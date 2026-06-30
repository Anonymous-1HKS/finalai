"""
backend/main.py
"""

import asyncio
import json
import math
import os
import random
import sys
import time
import uuid
from typing import Dict, List, Optional, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from OOP.traffic_lights import TrafficLight, TrafficLightCycle, LightPhase, LocationType
from OOP.vehicle import Vehicle, VehicleType, VehicleState
from OOP.sign_roads_pedestrian import (
    TrafficSign, SignType, Pedestrian, PedestrianState, RoadMetadata
)
from database.db_manager import db, _haversine_m
from reinforcement import RLTrafficController
from supervised import MLSupervisor

HCM_CENTER            = (10.7769, 106.7009)
TICK_INTERVAL         = 0.5
MAX_TOTAL_VEHICLES    = 300
MAX_TOTAL_PEDESTRIANS = 80
COLLISION_RADIUS_M    = 5.0

app = FastAPI(title="TrafficSim HCM")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

SIM: dict = {
    "tick":        0,
    "running":     True,
    "speed":       1.0,
    "vehicles":    {},
    "lights":      {},
    "signs":       {},
    "pedestrians": {},
    "incidents":   {},
    "viewports":   {},
}

rl_controller = RLTrafficController()
ml_supervisor = MLSupervisor()
WS_CLIENTS: Dict[str, WebSocket] = {}


def _rnd_near(center: tuple, spread: float = 0.04) -> tuple:
    return (center[0] + random.uniform(-spread, spread),
            center[1] + random.uniform(-spread, spread))


def _rnd_on_bbox_edge(bbox: dict) -> tuple:
    side = random.choice(["n", "s", "e", "w"])
    s, w, n, e = bbox["s"], bbox["w"], bbox["n"], bbox["e"]
    if side == "n":   return n, random.uniform(w, e), random.uniform(160, 200)
    elif side == "s": return s, random.uniform(w, e), random.uniform(-20, 20)
    elif side == "e": return random.uniform(s, n), e, random.uniform(250, 290)
    else:             return random.uniform(s, n), w, random.uniform(70, 110)


def _active_bbox_union() -> List[dict]:
    return list(SIM["viewports"].values())


def _point_in_any_bbox(lat, lng, bboxes, buffer=0.01):
    for b in bboxes:
        if (b["s"]-buffer <= lat <= b["n"]+buffer and
                b["w"]-buffer <= lng <= b["e"]+buffer):
            return True
    return False


def _build_ctx_for_vehicle(v: Vehicle) -> dict:
    ctx: dict = {"road_speed_limit": 50}
    nearest_tl, nearest_dist = None, 999.0
    for tl in SIM["lights"].values():
        dist = _haversine_m(v.lat, v.lng, tl.lat, tl.lng)
        if dist < 80 and tl.applies_to_vehicle(v.heading) and dist < nearest_dist:
            nearest_dist, nearest_tl = dist, tl
    if nearest_tl:
        tl_d = nearest_tl.to_dict()
        tl_d["distance_m"] = nearest_dist
        ctx["traffic_light"] = tl_d
        if nearest_tl.phase == LightPhase.RED and nearest_dist < 30:
            nearest_tl.register_vehicle(v.id)
        else:
            nearest_tl.deregister_vehicle(v.id)
    best_leader, best_gap = None, 999.0
    for other in SIM["vehicles"].values():
        if other.id == v.id: continue
        dist = _haversine_m(v.lat, v.lng, other.lat, other.lng)
        if dist < 60:
            hdiff = abs((v.heading - other.heading + 360) % 360)
            if (hdiff < 45 or hdiff > 315) and dist < best_gap:
                best_gap, best_leader = dist, other
    if best_leader:
        v.leader_id, v.gap_to_leader = best_leader.id, best_gap
        ctx["leader_vehicle"] = best_leader
    else:
        v.leader_id, v.gap_to_leader = None, 999.0
    ctx["nearby_vehicles"] = [
        o for o in SIM["vehicles"].values()
        if o.id != v.id and _haversine_m(v.lat, v.lng, o.lat, o.lng) < 100
    ]
    return ctx


def _seed_simulation():
    vtypes = ([VehicleType.CAR]*12 + [VehicleType.MOTORCYCLE]*16 +
              [VehicleType.TRUCK]*4 + [VehicleType.BUS]*4 +
              [VehicleType.AMBULANCE]*2 + [VehicleType.POLICE]*2)
    for _ in range(120):
        pos = _rnd_near(HCM_CENTER, 0.05)
        v = Vehicle(vehicle_type=random.choice(vtypes), lat=pos[0], lng=pos[1],
                    heading=random.uniform(0,360), speed=random.uniform(15,65))
        SIM["vehicles"][v.id] = v

    jtypes = [LocationType.INTERSECTION_3WAY, LocationType.INTERSECTION_4WAY,
              LocationType.INTERSECTION_4WAY, LocationType.INTERSECTION_4WAY,
              LocationType.INTERSECTION_MULTI, LocationType.ROUNDABOUT, LocationType.PEDESTRIAN_ONLY]
    for _ in range(40):
        pos = _rnd_near(HCM_CENTER, 0.055)
        tl = TrafficLight(lat=pos[0], lng=pos[1], location_type=random.choice(jtypes),
                          phase=random.choice(list(LightPhase)), countdown=random.uniform(3,40))
        tl.cycle = TrafficLightCycle(red_duration=random.uniform(20,40),
                                     yellow_duration=4, green_duration=random.uniform(18,35))
        SIM["lights"][tl.id] = tl

    for _ in range(60):
        pos = _rnd_near(HCM_CENTER, 0.055)
        s = TrafficSign(sign_type=random.choice(list(SignType)), lat=pos[0], lng=pos[1],
                        value=float(random.choice([30,40,50,60,80])))
        SIM["signs"][s.id] = s

    for _ in range(40):
        pos = _rnd_near(HCM_CENTER, 0.02)
        p = Pedestrian(lat=pos[0], lng=pos[1], heading=random.uniform(0,360))
        SIM["pedestrians"][p.id] = p


def _detect_collisions():
    vehicles = [v for v in SIM["vehicles"].values()
                if v.state not in (VehicleState.COLLIDED, VehicleState.STOPPED, VehicleState.PARKING)]
    new_incidents = []
    vlist = list(vehicles)
    for i in range(len(vlist)):
        v1 = vlist[i]
        if v1.state == VehicleState.COLLIDED: continue
        for j in range(i+1, len(vlist)):
            v2 = vlist[j]
            if v2.state == VehicleState.COLLIDED: continue
            if _haversine_m(v1.lat, v1.lng, v2.lat, v2.lng) < COLLISION_RADIUS_M:
                v1.register_collision(v2.id)
                v2.register_collision(v1.id)
                inc_id = f"INC-{str(uuid.uuid4())[:6]}"
                inc = {
                    "id": inc_id, "type": "Tai nạn giao thông",
                    "severity": "high" if (v1.speed+v2.speed)>60 else "medium",
                    "lat": (v1.lat+v2.lat)/2, "lng": (v1.lng+v2.lng)/2,
                    "description": f"Va chạm: {v1.vehicle_type.value} và {v2.vehicle_type.value}",
                    "blocked": True, "policeDispatched": False,
                    "time": time.strftime("%H:%M:%S"), "created_at": time.time(),
                }
                SIM["incidents"][inc_id] = inc
                db.save_incident(inc)
                new_incidents.append(inc)
                async def _clear(vid1, vid2, iid, delay=30):
                    await asyncio.sleep(delay)
                    for vid in (vid1, vid2):
                        if vid in SIM["vehicles"]:
                            SIM["vehicles"][vid].state = VehicleState.STOPPED
                            SIM["vehicles"][vid].collision_id = None
                    if iid in SIM["incidents"]:
                        del SIM["incidents"][iid]
                    db.resolve_incident(iid)
                asyncio.create_task(_clear(v1.id, v2.id, inc_id))
    return new_incidents


def _sync_population(dt):
    bboxes = _active_bbox_union()
    if not bboxes: return
    for vid in list(SIM["vehicles"].keys()):
        v = SIM["vehicles"][vid]
        if not _point_in_any_bbox(v.lat, v.lng, bboxes):
            del SIM["vehicles"][vid]
    for pid in list(SIM["pedestrians"].keys()):
        p = SIM["pedestrians"][pid]
        if not _point_in_any_bbox(p.lat, p.lng, bboxes):
            del SIM["pedestrians"][pid]
    vtypes = [VehicleType.CAR]*6 + [VehicleType.MOTORCYCLE]*8 + [VehicleType.TRUCK] + [VehicleType.BUS]
    for bbox in bboxes:
        in_bbox = sum(1 for v in SIM["vehicles"].values()
                      if bbox["s"] <= v.lat <= bbox["n"] and bbox["w"] <= v.lng <= bbox["e"])
        for _ in range(min(max(0, 35-in_bbox), 4)):
            if len(SIM["vehicles"]) >= MAX_TOTAL_VEHICLES: break
            lat, lng, heading = _rnd_on_bbox_edge(bbox)
            v = Vehicle(vehicle_type=random.choice(vtypes), lat=lat, lng=lng,
                        heading=heading, speed=random.uniform(20,55))
            SIM["vehicles"][v.id] = v
        if len(SIM["pedestrians"]) < MAX_TOTAL_PEDESTRIANS and random.random() < 0.15:
            lat, lng, heading = _rnd_on_bbox_edge(bbox)
            SIM["pedestrians"][(p:=Pedestrian(lat=lat, lng=lng, heading=(heading+180)%360)).id] = p


def _build_viewport_payload(bbox: dict = None) -> dict:
    def in_bbox(lat, lng):
        if not bbox: return True
        buf = 0.005
        return (bbox["s"]-buf <= lat <= bbox["n"]+buf and bbox["w"]-buf <= lng <= bbox["e"]+buf)
    vehicles    = [v.to_ws_dict() for v in SIM["vehicles"].values()     if in_bbox(v.lat, v.lng)]
    lights      = [tl.to_ws_dict() for tl in SIM["lights"].values()     if in_bbox(tl.lat, tl.lng)]
    signs       = [s.to_dict()     for s in SIM["signs"].values()       if in_bbox(s.lat, s.lng)]
    pedestrians = [p.to_dict()     for p in SIM["pedestrians"].values() if in_bbox(p.lat, p.lng)]
    incidents   = [inc for inc in SIM["incidents"].values()
                   if in_bbox(inc.get("lat",0), inc.get("lng",0))]
    speeds   = [v["speed"] for v in vehicles]
    jam_vals = [v["jam_severity"] for v in vehicles]
    avg_spd  = round(sum(speeds)/max(1,len(speeds)), 1)
    avg_jam  = sum(jam_vals)/max(1,len(jam_vals)) if jam_vals else 0
    jam_clusters = []
    if bbox:
        try: jam_clusters = db.get_jam_clusters(bbox)
        except: pass
    return {
        "vehicles": vehicles, "lights": lights, "signs": signs,
        "pedestrians": pedestrians, "incidents": incidents, "jamClusters": jam_clusters,
        "stats": {
            "total": len(vehicles),
            "avgSpeed": avg_spd,
            "density": min(100, round(len(vehicles)/max(1,300)*100)),
            "incidents": len(incidents),
            "avgJam": round(avg_jam*100),
        },
    }


def _avg_speed_near(lat, lng, radius_m=100.0):
    speeds = [v.speed for v in SIM["vehicles"].values()
              if _haversine_m(lat, lng, v.lat, v.lng) < radius_m]
    return sum(speeds)/max(1,len(speeds))


def _avg_jam_near(lat, lng, radius_m=100.0):
    jams = [v.jam_severity for v in SIM["vehicles"].values()
            if _haversine_m(lat, lng, v.lat, v.lng) < radius_m]
    return sum(jams)/max(1,len(jams))


def _spawn_police_near(lat, lng):
    if len(SIM["vehicles"]) >= MAX_TOTAL_VEHICLES: return
    v = Vehicle(vehicle_type=VehicleType.POLICE,
                lat=lat+random.uniform(-0.002,0.002),
                lng=lng+random.uniform(-0.002,0.002),
                heading=random.uniform(0,360), speed=80.0)
    SIM["vehicles"][v.id] = v


async def _broadcast_to_client(cid: str, ws: WebSocket, msg: dict):
    """Gửi msg đến 1 client, xóa nếu lỗi."""
    try:
        await ws.send_text(json.dumps(msg))
        return True
    except Exception:
        WS_CLIENTS.pop(cid, None)
        SIM["viewports"].pop(cid, None)
        return False



async def _async_db_flush_vehicles(states: List[dict]):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, db.save_oop_batch, states, "vehicle")


async def _async_db_flush_lights(states: List[dict]):
    loop = asyncio.get_event_loop()
    for s in states:
        await loop.run_in_executor(None, db.save_light_state, s)


async def _async_db_trim():
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, db.auto_trim, 1.0)


async def _simulation_loop():
    ml_log_queue: List[dict] = []
    while True:
        if not SIM["running"]:
            await asyncio.sleep(TICK_INTERVAL)
            continue
        tick_start = time.time()
        dt = TICK_INTERVAL * SIM["speed"]
        SIM["tick"] += 1
        tick = SIM["tick"]

        for v in list(SIM["vehicles"].values()):
            if v.state == VehicleState.COLLIDED: continue
            v.update(dt, _build_ctx_for_vehicle(v))
        for tl in SIM["lights"].values(): tl.update(dt)
        for p in SIM["pedestrians"].values(): p.update(dt, {})

        if tick % 10 == 0:  # collision + sync mỗi 5s
            for inc in _detect_collisions():
                ml_log_queue.append({
                    "type": "ml_log",
                    "text": f"🚨 Tai nạn tại ({inc['lat']:.4f},{inc['lng']:.4f}) — {inc['description']}",
                    "level": "warn",
                })
            _sync_population(dt)
            # DB flush xe: chỉ ghi mỗi 60 tick (30s) để giảm I/O
            if tick % 60 == 0:
                asyncio.ensure_future(_async_db_flush_vehicles(
                    [v.to_ws_dict() for v in SIM["vehicles"].values()]
                ))
            for tl_id, tl in list(SIM["lights"].items()):
                road_state = {
                    "avg_speed": _avg_speed_near(tl.lat, tl.lng),
                    "jam_severity": _avg_jam_near(tl.lat, tl.lng),
                    "has_incident": any(
                        _haversine_m(tl.lat, tl.lng, i.get("lat",0), i.get("lng",0)) < 200
                        for i in SIM["incidents"].values()
                    ),
                }
                try:
                    decision = rl_controller.decide(tl_id, tl.to_ws_dict(), road_state)
                    result = rl_controller.apply_action(decision["action"], tl)
                    if result:
                        ml_log_queue.append({
                            "type": "ml_log",
                            "text": f"🤖 RL [{tl.name}]: {result} — {decision['explanation']}",
                            "level": "action",
                        })
                        if result == "DISPATCH_POLICE":
                            _spawn_police_near(tl.lat, tl.lng)
                except Exception:
                    pass

        if tick % 30 == 0:
            asyncio.ensure_future(_async_db_flush_lights(
                [tl.to_ws_dict() for tl in SIM["lights"].values()]
            ))
            try:
                # Chạy ML supervisor trong thread riêng để không block event loop
                loop = asyncio.get_event_loop()
                ml_actions = await loop.run_in_executor(
                    None, ml_supervisor.tick, _build_viewport_payload(), SIM["lights"], {}
                )
                for action in ml_actions:
                    if action["type"] == "adjust_light":
                        tl = SIM["lights"].get(action["light_id"])
                        if tl:
                            tl.ml_set_cycle(red=action.get("red"), green=action.get("green"))
                            ml_log_queue.append({
                                "type": "ml_log",
                                "text": f"📊 ML điều chỉnh {tl.name}: đỏ={action.get('red')}s xanh={action.get('green')}s",
                                "level": "info",
                            })
                    elif action["type"] == "incident_detected":
                        ml_log_queue.append({
                            "type": "ml_log",
                            "text": f"⚠️ ML phát hiện sự cố đèn {action.get('light_id')}",
                            "level": "warn",
                        })
            except Exception:
                pass

        # FIX CHÍNH: Push riêng từng client theo bbox của họ
        if tick % 6 == 0 and WS_CLIENTS:
            logs_to_send = list(ml_log_queue)
            ml_log_queue.clear()
            dead = []
            for cid, ws in list(WS_CLIENTS.items()):
                bbox = SIM["viewports"].get(cid)  # bbox riêng của client này
                payload = _build_viewport_payload(bbox)  # lọc theo bbox → ít data hơn nhiều
                try:
                    await ws.send_text(json.dumps({"type": "oop_update", "data": payload}))
                except Exception:
                    dead.append(cid)
            for cid in dead:
                WS_CLIENTS.pop(cid, None)
                SIM["viewports"].pop(cid, None)
            # Gửi ML log
            for log_msg in logs_to_send:
                for cid, ws in list(WS_CLIENTS.items()):
                    try: await ws.send_text(json.dumps(log_msg))
                    except: pass

        if tick % 600 == 0:
            asyncio.ensure_future(_async_db_trim())

        elapsed = time.time() - tick_start
        await asyncio.sleep(max(0.0, TICK_INTERVAL - elapsed))


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    client_id = str(uuid.uuid4())[:8]
    WS_CLIENTS[client_id] = websocket
    try:
        # Gửi snapshot ban đầu — chưa có bbox nên gửi tất cả
        await websocket.send_text(json.dumps({
            "type": "oop_update", "data": _build_viewport_payload()
        }))
    except Exception:
        pass
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
                if msg.get("type") == "set_viewport":
                    bbox = msg.get("bbox", {})
                    if all(k in bbox for k in ("s","w","n","e")):
                        SIM["viewports"][client_id] = bbox
                        # Gửi ngay payload lọc theo bbox mới
                        await websocket.send_text(json.dumps({
                            "type": "oop_update",
                            "data": _build_viewport_payload(bbox)
                        }))
                elif msg.get("type") == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))
            except json.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        WS_CLIENTS.pop(client_id, None)
        SIM["viewports"].pop(client_id, None)


@app.get("/viewport")
async def get_viewport(s:float=0,w:float=0,n:float=0,e:float=0,
                       zoom:int=15,limit:int=400,client_id:str="anon"):
    bbox = {"s":s,"w":w,"n":n,"e":e}
    SIM["viewports"][client_id] = bbox
    return JSONResponse(_build_viewport_payload(bbox))


@app.get("/road/{road_id}")
async def get_road(road_id:str, lat:float=None, lng:float=None):
    stats = db.get_road_stats(road_id=road_id if road_id!="undefined" else None, lat=lat, lng=lng)
    stats["mlNote"] = (f"⚠️ ML phát hiện tắc nghẽn mức {stats['congestion']} — đang điều chỉnh đèn."
                       if stats.get("congestion") in ("heavy","gridlock")
                       else f"✅ Lưu lượng {stats.get('congestion','free')} — AI đang giám sát.")
    return JSONResponse(stats)


@app.post("/sim/toggle")
async def sim_toggle():
    SIM["running"] = not SIM["running"]
    return {"running": SIM["running"]}


@app.post("/sim/speed")
async def sim_speed(body:dict):
    SIM["speed"] = float(body.get("speed", 1.0))
    return {"speed": SIM["speed"]}


@app.post("/db/viewport/snapshot")
async def db_viewport_snapshot(body:dict):
    try:
        db.save_viewport_snapshot(bbox=body.get("bbox",{}),zoom=body.get("zoom",15),data=body.get("data",{}))
    except: pass
    return {"ok": True}


@app.get("/stats")
async def get_stats():
    vehicles = list(SIM["vehicles"].values())
    speeds = [v.speed for v in vehicles]
    return {
        "total_vehicles": len(vehicles), "total_lights": len(SIM["lights"]),
        "total_pedestrians": len(SIM["pedestrians"]), "total_incidents": len(SIM["incidents"]),
        "avg_speed": round(sum(speeds)/max(1,len(speeds)),1),
        "tick": SIM["tick"], "running": SIM["running"], "ws_clients": len(WS_CLIENTS),
    }


@app.on_event("startup")
async def startup():
    _seed_simulation()
    asyncio.create_task(_simulation_loop())
    print(f"[TrafficSim] Started — {len(SIM['vehicles'])} xe, {len(SIM['lights'])} đèn, {len(SIM['signs'])} biển")


# ── STATIC FILES ──────────────────────────────────────────────────────────────
_frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.isdir(_frontend_dir):
    app.mount("/", StaticFiles(directory=_frontend_dir, html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False, workers=1)