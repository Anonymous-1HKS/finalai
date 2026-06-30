"""
supervised.py  (đặt ở root, cùng cấp reinforcement.py)
"""

import json, time, math, random
from typing import List, Dict, Optional, Tuple

# KHÔNG tự thêm sys.path — main.py đã thiết lập ROOT_DIR trước khi import
from database.db_manager import db


def extract_road_features(road_data: dict, light_history: List[dict], oop_history: List[dict]) -> dict:
    vehicles = [h for h in oop_history if h.get("obj_type") == "vehicle"]
    speeds, stopped_count = [], 0
    for v in vehicles:
        try:
            s = json.loads(v["state_json"])
            spd = s.get("speed", 0)
            speeds.append(spd)
            if spd < 5: stopped_count += 1
        except: pass
    avg_speed = sum(speeds)/max(1,len(speeds))
    queue_lens = [h.get("queue_len",0) for h in light_history[-10:]]
    hour_of_day = time.localtime().tm_hour
    speeds_list = speeds
    speed_trend = (speeds_list[-1]-speeds_list[0]) if len(speeds_list)>=2 else 0.0
    return {
        "vehicle_count":    len(vehicles),
        "stopped_vehicles": stopped_count,
        "avg_speed":        avg_speed,
        "avg_queue":        sum(queue_lens)/max(1,len(queue_lens)),
        "speed_trend":      speed_trend,
        "jam_severity":     road_data.get("jam_severity", 0.0),
        "hour_of_day":      hour_of_day,
        "day_of_week":      time.localtime().tm_wday,
        "speed_limit":      road_data.get("speed_limit", 60),
        "road_type_enc":    {"motorway":5,"primary":4,"secondary":3,"tertiary":2,"residential":1}.get(road_data.get("type","residential"),0),
        "is_peak":          int(hour_of_day in (7,8,9,17,18,19)),
    }


class CongestionClassifier:
    LABELS = ["free","moderate","heavy","gridlock"]
    def __init__(self):
        self._priors = {l:0.25 for l in self.LABELS}
        self._weights = {
            "vehicle_count": {"free":0,"moderate":5,"heavy":15,"gridlock":30},
            "avg_speed":     {"free":50,"moderate":30,"heavy":15,"gridlock":5},
            "avg_queue":     {"free":0,"moderate":3,"heavy":8,"gridlock":15},
            "jam_severity":  {"free":0.0,"moderate":0.3,"heavy":0.6,"gridlock":0.9},
        }
        self._trained_samples = 0

    def predict(self, features: dict) -> Tuple[str, Dict[str,float]]:
        scores = {l: math.log(self._priors[l]+1e-9) for l in self.LABELS}
        vc, spd, q, jam = (features.get(k,d) for k,d in
                           [("vehicle_count",0),("avg_speed",60),("avg_queue",0),("jam_severity",0.0)])
        for label,t in self._weights["vehicle_count"].items(): scores[label] -= abs(vc-t)*0.05
        for label,t in self._weights["avg_speed"].items():     scores[label] -= abs(spd-t)*0.03
        for label,t in self._weights["avg_queue"].items():     scores[label] -= abs(q-t)*0.1
        for label,t in self._weights["jam_severity"].items():  scores[label] -= abs(jam-t)*2.0
        if features.get("is_peak"):
            scores["heavy"] += 0.3; scores["gridlock"] += 0.2
        max_s = max(scores.values())
        exps  = {l: math.exp(scores[l]-max_s) for l in self.LABELS}
        total = sum(exps.values())
        probs = {l: exps[l]/total for l in self.LABELS}
        return max(probs, key=lambda l: probs[l]), probs

    def train_from_db(self, n_samples=1000):
        samples = db.get_ml_training_data(source="supervised", limit=n_samples)
        if not samples: return
        counts = {l:0 for l in self.LABELS}
        for s in samples:
            lbl = s["labels"].get("congestion")
            if lbl in counts: counts[lbl] += 1
        total = sum(counts.values())
        if total > 0:
            self._priors = {l:(counts[l]+1)/(total+len(self.LABELS)) for l in self.LABELS}
            self._trained_samples = total


class LightCycleOptimizer:
    def recommend(self, light_id, current_queue, hour, is_gridlock=False) -> dict:
        hist = db.get_light_history(light_id, hours=2.0)
        if not hist:
            is_peak = hour in (7,8,9,17,18,19)
            return {"red": 25 if is_peak else 35, "green": 35 if is_peak else 25}
        avg_r = sum(h["queue_len"] for h in hist if h["phase"]=="red")/max(1,sum(1 for h in hist if h["phase"]=="red"))
        avg_g = sum(h["queue_len"] for h in hist if h["phase"]=="green")/max(1,sum(1 for h in hist if h["phase"]=="green"))
        ratio = avg_r/max(1,avg_g)
        if is_gridlock or ratio>2.0:
            return {"red": round(max(10,30/ratio*0.8),1), "green": round(min(60,25*ratio*0.8),1)}
        elif ratio<0.5:
            return {"red": round(min(60,30*1.2),1), "green": round(max(10,25*0.7),1)}
        return {"red":30.0,"green":25.0}


class IncidentClassifier:
    TYPES = {
        "collision":  {"min_speed_drop":20,"min_vehicles_stopped":2},
        "breakdown":  {"min_speed_drop":15,"min_vehicles_stopped":1},
        "congestion": {"min_speed_drop":5, "min_vehicles_stopped":5},
        "road_block": {"min_speed_drop":30,"min_vehicles_stopped":3},
    }
    def classify(self, features, prev_features) -> Optional[dict]:
        drop = prev_features.get("avg_speed",0) - features.get("avg_speed",0)
        stopped = features.get("stopped_vehicles",0)
        for t, thresh in self.TYPES.items():
            if drop>=thresh["min_speed_drop"] and stopped>=thresh["min_vehicles_stopped"]:
                return {"type":t,"severity":"high" if drop>30 else ("medium" if drop>15 else "low"),
                        "confidence":min(0.99,drop/50+stopped/20)}
        return None


class MLSupervisor:
    def __init__(self):
        self.congestion_clf  = CongestionClassifier()
        self.light_optimizer = LightCycleOptimizer()
        self.incident_clf    = IncidentClassifier()
        self._prev_features: Dict[str,dict] = {}
        self._last_train = 0.0

    def tick(self, viewport_data:dict, traffic_lights:dict, roads:dict) -> List[dict]:
        actions = []
        now = time.time()
        if now-self._last_train > 300:
            self.congestion_clf.train_from_db()
            self._last_train = now
        for light_id, light in traffic_lights.items():
            road_data = {}
            hist_oop   = db.get_oop_history(road_id=light.road_id, since=now-600, limit=50)
            hist_light = db.get_light_history(light_id, hours=0.5)
            features   = extract_road_features(road_data, hist_light, hist_oop)
            label, probs = self.congestion_clf.predict(features)
            prev = self._prev_features.get(light_id, features)
            incident = self.incident_clf.classify(features, prev)
            self._prev_features[light_id] = features
            if label in ("heavy","gridlock"):
                rec = self.light_optimizer.recommend(light_id, light.queue_length,
                                                     features["hour_of_day"], label=="gridlock")
                actions.append({"type":"adjust_light","light_id":light_id,
                                 "red":rec["red"],"green":rec["green"],
                                 "reason":f"Congestion={label} ({probs[label]:.0%})"})
            if incident:
                actions.append({"type":"incident_detected","light_id":light_id,
                                 "road_id":light.road_id,"incident":incident})
            try:
                db.save_ml_sample(features=features,
                                  labels={"congestion":label,"incident":incident["type"] if incident else None},
                                  source="supervised")
            except: pass
        return actions

    def get_road_analysis(self, road_id:str) -> dict:
        now = time.time()
        hist = db.get_oop_history(road_id=road_id, since=now-600, limit=100)
        features = extract_road_features({}, [], hist)
        label, probs = self.congestion_clf.predict(features)
        return {"road_id":road_id,"congestion":label,
                "probs":{k:round(v,3) for k,v in probs.items()},
                "features":features}