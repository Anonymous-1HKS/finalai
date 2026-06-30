"""
database/db_manager.py
SQLite database manager cho TrafficSim.

FIX:
- PRAGMA journal_mode=WAL + PRAGMA wal_autocheckpoint=100 để WAL tự checkpoint
- PRAGMA cache_size=-8000 giảm memory pressure
- Thread-safe với check_same_thread=False + write lock riêng
- auto_trim() giữ 30 phút thay vì 1 giờ để DB không phình
- get_road_stats() / get_jam_clusters() giữ nguyên logic
"""

import sqlite3
import json
import time
import math
import threading
import os
from typing import List, Dict, Any, Optional


DB_PATH = os.path.join(os.path.dirname(__file__), "traffic_sim.db")


class DBManager:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA wal_autocheckpoint=100")
        conn.execute("PRAGMA cache_size=-8000")
        conn.execute("PRAGMA temp_store=MEMORY")
        return conn

    def _init_db(self):
        with self._lock:
            conn = self._conn()
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS oop_history (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    obj_type    TEXT NOT NULL,
                    obj_id      TEXT NOT NULL,
                    road_id     TEXT,
                    state_json  TEXT NOT NULL,
                    lat         REAL,
                    lng         REAL,
                    speed       REAL DEFAULT 0,
                    jam_severity REAL DEFAULT 0,
                    created_at  REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_oop_road    ON oop_history(road_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_oop_obj     ON oop_history(obj_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_oop_type    ON oop_history(obj_type, created_at);
                CREATE INDEX IF NOT EXISTS idx_oop_lat_lng ON oop_history(lat, lng);

                CREATE TABLE IF NOT EXISTS light_history (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    light_id    TEXT NOT NULL,
                    phase       TEXT NOT NULL,
                    countdown   REAL,
                    queue_len   INTEGER DEFAULT 0,
                    ml_adjusted INTEGER DEFAULT 0,
                    created_at  REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_light_id ON light_history(light_id, created_at);

                CREATE TABLE IF NOT EXISTS ml_samples (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    source      TEXT NOT NULL,
                    episode_id  TEXT,
                    features    TEXT NOT NULL,
                    labels      TEXT NOT NULL,
                    reward      REAL DEFAULT 0,
                    created_at  REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_ml_source ON ml_samples(source, created_at);

                CREATE TABLE IF NOT EXISTS incidents (
                    id          TEXT PRIMARY KEY,
                    inc_type    TEXT NOT NULL,
                    severity    TEXT NOT NULL,
                    lat         REAL,
                    lng         REAL,
                    road_id     TEXT,
                    description TEXT,
                    blocked     INTEGER DEFAULT 0,
                    police_dispatched INTEGER DEFAULT 0,
                    resolved    INTEGER DEFAULT 0,
                    created_at  REAL NOT NULL,
                    resolved_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_inc_resolved ON incidents(resolved, created_at);
            """)
            conn.commit()
            conn.close()

    # ── OOP STATE ────────────────────────────────────────────────────────────
    def save_oop_state(self, state: dict, obj_type: str = "vehicle"):
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("""
                    INSERT INTO oop_history
                        (obj_type, obj_id, road_id, state_json, lat, lng, speed, jam_severity, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    obj_type,
                    state.get("id", ""),
                    state.get("road_id"),
                    json.dumps(state),
                    state.get("lat"),
                    state.get("lng"),
                    state.get("speed", 0),
                    state.get("jam_severity", 0),
                    time.time(),
                ))
                conn.commit()
            finally:
                conn.close()

    def save_oop_batch(self, states: List[dict], obj_type: str):
        """Lưu nhiều object trong 1 transaction."""
        if not states:
            return
        now = time.time()
        rows = [
            (obj_type, s.get("id", ""), s.get("road_id"), json.dumps(s),
             s.get("lat"), s.get("lng"), s.get("speed", 0), s.get("jam_severity", 0), now)
            for s in states
        ]
        with self._lock:
            conn = self._conn()
            try:
                conn.executemany("""
                    INSERT INTO oop_history
                        (obj_type, obj_id, road_id, state_json, lat, lng, speed, jam_severity, created_at)
                    VALUES (?,?,?,?,?,?,?,?,?)
                """, rows)
                conn.commit()
            finally:
                conn.close()

    def get_oop_history(self, road_id: str = None, obj_type: str = None,
                        since: float = None, limit: int = 100) -> List[dict]:
        if since is None:
            since = time.time() - 600
        conn = self._conn()
        try:
            conditions = ["created_at > ?"]
            params: list = [since]
            if road_id:
                conditions.append("road_id = ?")
                params.append(road_id)
            if obj_type:
                conditions.append("obj_type = ?")
                params.append(obj_type)
            sql = f"""
                SELECT * FROM oop_history
                WHERE {' AND '.join(conditions)}
                ORDER BY created_at DESC LIMIT ?
            """
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ── LIGHT HISTORY ────────────────────────────────────────────────────────
    def save_light_state(self, light_dict: dict):
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("""
                    INSERT INTO light_history
                        (light_id, phase, countdown, queue_len, ml_adjusted, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    light_dict.get("id"),
                    light_dict.get("phase", "red"),
                    light_dict.get("countdown_exact", 0),
                    light_dict.get("queue_length", 0),
                    1 if light_dict.get("ml_adjusted") else 0,
                    time.time(),
                ))
                conn.commit()
            finally:
                conn.close()

    def get_light_history(self, light_id: str, hours: float = 1.0) -> List[dict]:
        since = time.time() - hours * 3600
        conn = self._conn()
        try:
            rows = conn.execute("""
                SELECT * FROM light_history
                WHERE light_id = ? AND created_at > ?
                ORDER BY created_at DESC LIMIT 200
            """, (light_id, since)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ── ML SAMPLES ───────────────────────────────────────────────────────────
    def save_ml_sample(self, features: dict, labels: dict, source: str,
                       episode_id: str = None, reward: float = 0.0):
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("""
                    INSERT INTO ml_samples
                        (source, episode_id, features, labels, reward, created_at)
                    VALUES (?,?,?,?,?,?)
                """, (source, episode_id, json.dumps(features), json.dumps(labels),
                      reward, time.time()))
                conn.commit()
            finally:
                conn.close()

    def get_ml_training_data(self, source: str = None, limit: int = 1000) -> List[dict]:
        conn = self._conn()
        try:
            if source:
                rows = conn.execute("""
                    SELECT * FROM ml_samples WHERE source=?
                    ORDER BY created_at DESC LIMIT ?
                """, (source, limit)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM ml_samples
                    ORDER BY created_at DESC LIMIT ?
                """, (limit,)).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                try:
                    d["features"] = json.loads(d["features"])
                except Exception:
                    pass
                try:
                    d["labels"] = json.loads(d["labels"])
                except Exception:
                    pass
                result.append(d)
            return result
        finally:
            conn.close()

    # ── INCIDENTS ────────────────────────────────────────────────────────────
    def save_incident(self, incident: dict):
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("""
                    INSERT OR REPLACE INTO incidents
                        (id, inc_type, severity, lat, lng, road_id,
                         description, blocked, police_dispatched, resolved, created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    incident.get("id"),
                    incident.get("type", "unknown"),
                    incident.get("severity", "low"),
                    incident.get("lat"),
                    incident.get("lng"),
                    incident.get("road_id"),
                    incident.get("description", ""),
                    1 if incident.get("blocked") else 0,
                    1 if incident.get("policeDispatched") else 0,
                    0,
                    incident.get("created_at", time.time()),
                ))
                conn.commit()
            finally:
                conn.close()

    def get_active_incidents(self) -> List[dict]:
        conn = self._conn()
        try:
            rows = conn.execute("""
                SELECT * FROM incidents WHERE resolved = 0
                ORDER BY created_at DESC LIMIT 50
            """).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def resolve_incident(self, incident_id: str):
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("""
                    UPDATE incidents SET resolved=1, resolved_at=? WHERE id=?
                """, (time.time(), incident_id))
                conn.commit()
            finally:
                conn.close()

    # ── ROAD STATS ───────────────────────────────────────────────────────────
    def get_road_stats(self, road_id: str = None, lat: float = None,
                       lng: float = None, radius_m: float = 70.0) -> dict:
        conn = self._conn()
        try:
            since = time.time() - 120
            if road_id:
                rows = conn.execute("""
                    SELECT speed, lat, lng, jam_severity FROM oop_history
                    WHERE obj_type='vehicle' AND road_id=? AND created_at>?
                    ORDER BY created_at DESC LIMIT 100
                """, (road_id, since)).fetchall()
            elif lat is not None and lng is not None:
                delta = radius_m / 111320
                rows = conn.execute("""
                    SELECT speed, lat, lng, jam_severity FROM oop_history
                    WHERE obj_type='vehicle' AND created_at>?
                      AND lat BETWEEN ? AND ? AND lng BETWEEN ? AND ?
                    ORDER BY created_at DESC LIMIT 200
                """, (since, lat - delta, lat + delta,
                      lng - delta, lng + delta)).fetchall()
                rows = [r for r in rows
                        if _haversine_m(lat, lng, r["lat"], r["lng"]) <= radius_m]
            else:
                return {"vehicles": 0, "avgSpeed": 0, "congestion": "free"}

            speeds   = [r["speed"] for r in rows if r["speed"] is not None]
            jam_vals = [r["jam_severity"] for r in rows if r["jam_severity"] is not None]
            count    = len(speeds)
            avg_spd  = round(sum(speeds) / max(1, count), 1)
            avg_jam  = sum(jam_vals) / max(1, len(jam_vals))

            if avg_jam > 0.7 or (count > 10 and avg_spd < 10):
                congestion = "gridlock"
            elif avg_jam > 0.4 or (count > 6 and avg_spd < 20):
                congestion = "heavy"
            elif count > 3 and avg_spd < 35:
                congestion = "moderate"
            else:
                congestion = "free"

            return {
                "vehicles":   count,
                "avgSpeed":   avg_spd,
                "congestion": congestion,
                "hasIssue":   congestion in ("heavy", "gridlock"),
            }
        finally:
            conn.close()

    def get_jam_clusters(self, bbox: dict, min_cluster: int = 3) -> List[dict]:
        conn = self._conn()
        try:
            since = time.time() - 60
            rows = conn.execute("""
                SELECT lat, lng, jam_severity FROM oop_history
                WHERE obj_type='vehicle' AND created_at > ?
                  AND jam_severity > 0.4
                  AND lat BETWEEN ? AND ? AND lng BETWEEN ? AND ?
                ORDER BY created_at DESC LIMIT 500
            """, (since,
                  bbox.get("s", 0), bbox.get("n", 90),
                  bbox.get("w", 0), bbox.get("e", 180))).fetchall()

            if not rows:
                return []

            cells: Dict[tuple, list] = {}
            for r in rows:
                cell_lat = round(r["lat"] * 1000)
                cell_lng = round(r["lng"] * 1000)
                key = (cell_lat, cell_lng)
                if key not in cells:
                    cells[key] = []
                cells[key].append(dict(r))

            clusters = []
            used_keys = set()
            for key, members in cells.items():
                if key in used_keys:
                    continue
                all_members = list(members)
                for dk in [(-1, 0), (1, 0), (0, -1), (0, 1),
                           (-1, -1), (1, 1), (-1, 1), (1, -1)]:
                    neighbor = (key[0] + dk[0], key[1] + dk[1])
                    if neighbor in cells and neighbor not in used_keys:
                        all_members.extend(cells[neighbor])
                        used_keys.add(neighbor)
                used_keys.add(key)

                if len(all_members) >= min_cluster:
                    avg_lat = sum(m["lat"] for m in all_members) / len(all_members)
                    avg_lng = sum(m["lng"] for m in all_members) / len(all_members)
                    avg_jam = sum(m["jam_severity"] for m in all_members) / len(all_members)
                    clusters.append({
                        "id":       f"JAM-{key[0]}-{key[1]}",
                        "lat":      round(avg_lat, 6),
                        "lng":      round(avg_lng, 6),
                        "count":    len(all_members),
                        "severity": round(avg_jam, 2),
                        "label":    f"Kẹt xe: {len(all_members)} xe, mức {round(avg_jam * 100)}%",
                    })

            return sorted(clusters, key=lambda c: c["count"], reverse=True)[:20]
        finally:
            conn.close()

    # ── AUTO TRIM ────────────────────────────────────────────────────────────
    def auto_trim(self, keep_hours: float = 0.5):
        """
        FIX: Giữ 30 phút (0.5h) thay vì 1 giờ để DB không phình.
        Gọi mỗi 300 tick (2.5 phút).
        """
        cutoff = time.time() - keep_hours * 3600
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("DELETE FROM oop_history  WHERE created_at < ?", (cutoff,))
                conn.execute("DELETE FROM light_history WHERE created_at < ?", (cutoff,))
                ml_cutoff = time.time() - 7 * 86400
                conn.execute("DELETE FROM ml_samples WHERE created_at < ?", (ml_cutoff,))
                inc_cutoff = time.time() - 86400
                conn.execute("""
                    DELETE FROM incidents WHERE resolved=1 AND resolved_at < ?
                """, (inc_cutoff,))
                conn.commit()
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            finally:
                conn.close()

    # ── VIEWPORT SNAPSHOT ────────────────────────────────────────────────────
    def save_viewport_snapshot(self, bbox: dict, zoom: int, data: dict):
        with self._lock:
            conn = self._conn()
            try:
                vehicle_count = len(data.get("vehicles", []))
                conn.execute("""
                    INSERT INTO ml_samples (source, features, labels, created_at)
                    VALUES ('viewport_snapshot', ?, ?, ?)
                """, (
                    json.dumps({"bbox": bbox, "zoom": zoom}),
                    json.dumps({"vehicle_count": vehicle_count}),
                    time.time(),
                ))
                conn.commit()
            finally:
                conn.close()


# ── HELPER ──────────────────────────────────────────────────────────────────
def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# Singleton
db = DBManager()