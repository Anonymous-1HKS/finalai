"""
OOP/vehicle.py
Mô phỏng xe cộ với logic di chuyển đầy đủ.

FIX:
- _decide_target_speed(): luôn có fallback return float(limit) ở cuối
- ctx mặc định: nếu không có traffic_light / leader_vehicle thì xe chạy đúng tốc độ limit
- _apply_acceleration(): đơn vị m/s² đúng
- to_ws_dict() compact cho WebSocket push
"""

import uuid
import math
import random
from dataclasses import dataclass, field
from typing import Optional, Tuple, Dict, Any
from enum import Enum


class VehicleType(Enum):
    CAR         = "car"
    MOTORCYCLE  = "motorcycle"
    TRUCK       = "truck"
    BUS         = "bus"
    AMBULANCE   = "ambulance"
    POLICE      = "police"
    FIREFIGHTER = "firefighter"


class VehicleState(Enum):
    CRUISING    = "cruising"
    ACCELERATING = "accelerating"
    BRAKING     = "braking"
    STOPPED     = "stopped"
    YIELDING    = "yielding"
    OVERTAKING  = "overtaking"
    TURNING_L   = "turning_left"
    TURNING_R   = "turning_right"
    U_TURN      = "u_turn"
    EMERGENCY   = "emergency"
    PARKING     = "parking"
    COLLIDED    = "collided"
    JAMMED      = "jammed"


class LaneChange(Enum):
    NONE  = 0
    LEFT  = -1
    RIGHT = 1


VEHICLE_SPECS = {
    VehicleType.CAR:         {"max_speed": 120, "accel": 4.5, "decel": 7.0, "length": 4.5,  "width": 1.8},
    VehicleType.MOTORCYCLE:  {"max_speed": 100, "accel": 6.0, "decel": 8.0, "length": 2.2,  "width": 0.9},
    VehicleType.TRUCK:       {"max_speed":  80, "accel": 2.5, "decel": 5.0, "length": 12.0, "width": 2.5},
    VehicleType.BUS:         {"max_speed":  70, "accel": 2.0, "decel": 5.5, "length": 12.0, "width": 2.4},
    VehicleType.AMBULANCE:   {"max_speed": 130, "accel": 5.0, "decel": 8.0, "length": 5.5,  "width": 2.0},
    VehicleType.POLICE:      {"max_speed": 150, "accel": 6.0, "decel": 9.0, "length": 4.8,  "width": 1.9},
    VehicleType.FIREFIGHTER: {"max_speed": 100, "accel": 3.5, "decel": 7.0, "length": 9.0,  "width": 2.5},
}


@dataclass
class Vehicle:
    vehicle_type: VehicleType = VehicleType.CAR
    id:           str         = field(default_factory=lambda: str(uuid.uuid4())[:8])
    lat:          float       = 0.0
    lng:          float       = 0.0
    heading:      float       = 0.0
    speed:        float       = 0.0
    lane:         int         = 0
    road_id:      Optional[str] = None
    road_name:    str         = "Đường chưa rõ"
    destination:  Optional[Tuple[float, float]] = None
    plate:        str         = field(default_factory=lambda: f"51A-{random.randint(10000, 99999)}")

    state:       VehicleState = VehicleState.CRUISING
    prev_state:  VehicleState = VehicleState.CRUISING
    lane_change: LaneChange   = LaneChange.NONE

    acceleration:  float = 0.0
    angular_vel:   float = 0.0

    leader_id:      Optional[str] = None
    gap_to_leader:  float = 999.0
    blocked_ahead:  bool  = False
    emergency_mode: bool  = False
    siren_on:       bool  = False
    collision_id:   Optional[str] = None

    _overtake_cooldown: float = 0.0
    _turn_progress:     float = 0.0
    _stop_timer:        float = 0.0
    _decision_timer:    float = 0.0
    _jam_timer:         float = 0.0
    jam_severity:       float = 0.0
    _reaction_delay:    float = 0.0

    ml_controlled:   bool           = False
    ml_target_speed: Optional[float] = None

    def __post_init__(self):
        spec = VEHICLE_SPECS[self.vehicle_type]
        self.max_speed  = spec["max_speed"]
        self.accel_rate = spec["accel"]
        self.decel_rate = spec["decel"]
        self.length     = spec["length"]
        self.width      = spec["width"]
        if self.vehicle_type in (VehicleType.AMBULANCE, VehicleType.POLICE, VehicleType.FIREFIGHTER):
            self.emergency_mode = True
            self.siren_on       = True

    # ── CORE UPDATE ──────────────────────────────────────────────────────────
    def update(self, dt: float, context: dict):
        self._decision_timer    -= dt
        self._overtake_cooldown  = max(0.0, self._overtake_cooldown - dt)
        self._reaction_delay     = max(0.0, self._reaction_delay - dt)

        target_speed = self._decide_target_speed(context)
        if self.ml_controlled and self.ml_target_speed is not None:
            target_speed = self.ml_target_speed

        self._apply_acceleration(target_speed, dt)
        self._update_position(dt)
        self._update_state(context, dt)

        if self.emergency_mode and self.siren_on:
            self._handle_emergency_corridor(context)

    # ── TARGET SPEED ─────────────────────────────────────────────────────────
    def _decide_target_speed(self, ctx: dict) -> float:
        if self._reaction_delay > 0:
            return 0.0

        limit = float(ctx.get("road_speed_limit", 50))

        # Đèn giao thông
        tl = ctx.get("traffic_light")
        if tl:
            tl_phase = tl.get("phase") if isinstance(tl, dict) else getattr(tl, "phase", None)
            if hasattr(tl_phase, "value"):
                tl_phase = tl_phase.value

            tl_dist = (tl.get("distance_m", 999) if isinstance(tl, dict)
                       else getattr(tl, "distance_m", 999))
            tl_red_grace = (tl.get("red_grace", False) if isinstance(tl, dict)
                            else (tl.is_red_grace_period() if hasattr(tl, "is_red_grace_period") else False))
            tl_yellow_risky = (tl.get("yellow_risky", False) if isinstance(tl, dict)
                               else (tl.is_yellow_risky() if hasattr(tl, "is_yellow_risky") else False))

            if tl_dist < 40:
                if tl_phase == "yellow":
                    if tl_yellow_risky or tl_dist < 25:
                        return min(self.max_speed, limit * 1.2)
                    return 0.0
                elif tl_phase == "red":
                    if tl_red_grace and tl_dist < 12 and self.vehicle_type == VehicleType.MOTORCYCLE:
                        return float(limit)
                    if tl_dist < 20:
                        return 0.0

        # Xe phía trước (IDM)
        leader = ctx.get("leader_vehicle")
        if leader and self.gap_to_leader < 50:
            safe_speed = self._idm_speed(leader)
            if safe_speed < (limit * 0.3) and self._can_overtake(ctx):
                self._start_overtake()
                return min(limit, self.speed + 15)
            return safe_speed

        # Sự cố phía trước
        if ctx.get("incident_ahead"):
            return 0.0 if self.gap_to_leader < 25 else min(20.0, limit * 0.3)

        # Hành lang xe cứu thương
        if ctx.get("is_emergency_corridor") and not self.emergency_mode:
            return 0.0

        # FIX: luôn có return ở cuối
        if self.emergency_mode:
            return min(self.max_speed, limit * 1.5)
        return float(limit)

    def _idm_speed(self, leader) -> float:
        v   = self.speed / 3.6
        v0  = self.max_speed / 3.6
        dv  = v - (leader.speed / 3.6)
        s   = max(0.1, self.gap_to_leader)
        s_star = 2.0 + max(0.0, v * 1.5 + v * dv / (2 * math.sqrt(self.accel_rate * self.decel_rate)))
        accel  = self.accel_rate * (1 - (v / max(0.1, v0)) ** 4 - (s_star / s) ** 2)
        return max(0.0, v + accel * 0.1) * 3.6

    # ── OVERTAKE ─────────────────────────────────────────────────────────────
    def _can_overtake(self, ctx: dict) -> bool:
        if self._overtake_cooldown > 0:
            return False
        if self.vehicle_type in (VehicleType.TRUCK, VehicleType.BUS):
            return False
        if ctx.get("oncoming_vehicle"):
            return False
        if ctx.get("no_overtake_zone"):
            return False
        return True

    def _start_overtake(self):
        self.state              = VehicleState.OVERTAKING
        self.lane_change        = LaneChange.RIGHT
        self._overtake_cooldown = 8.0

    def start_turn(self, direction: str):
        if direction == "left":
            self.state       = VehicleState.TURNING_L
            self.lane_change = LaneChange.LEFT
        elif direction == "right":
            self.state       = VehicleState.TURNING_R
            self.lane_change = LaneChange.RIGHT
        elif direction == "u_turn":
            self.state = VehicleState.U_TURN
        self._turn_progress = 0.0

    # ── PHYSICS ──────────────────────────────────────────────────────────────
    def _apply_acceleration(self, target_kmh: float, dt: float):
        if dt <= 0:
            return
        delta_kmh = target_kmh - self.speed
        delta_ms  = delta_kmh / 3.6
        needed_accel = delta_ms / dt

        if delta_kmh > 0:
            self.acceleration = min(self.accel_rate, needed_accel)
        else:
            self.acceleration = max(-self.decel_rate, needed_accel)

        self.speed = max(0.0, self.speed + self.acceleration * dt * 3.6)

    def _update_position(self, dt: float):
        if self.speed < 0.01:
            return
        dist_m = (self.speed / 3.6) * dt
        rad    = math.radians(self.heading)
        dlat   = (dist_m / 111320) * math.cos(rad)
        dlng   = (dist_m / (111320 * math.cos(math.radians(self.lat)))) * math.sin(rad)
        self.lat += dlat
        self.lng += dlng

    # ── STATE MACHINE ────────────────────────────────────────────────────────
    def _update_state(self, ctx: dict, dt: float):
        if self.state == VehicleState.COLLIDED:
            return

        tl = ctx.get("traffic_light")
        waiting_for_light = False
        if tl:
            tl_phase = tl.get("phase") if isinstance(tl, dict) else getattr(tl, "phase", None)
            if hasattr(tl_phase, "value"):
                tl_phase = tl_phase.value
            tl_dist = (tl.get("distance_m", 999) if isinstance(tl, dict)
                       else getattr(tl, "distance_m", 999))
            waiting_for_light = (tl_phase == "red" and tl_dist < 20)

        if self.speed < 5.0 and self.gap_to_leader < 15.0 and not waiting_for_light:
            self._jam_timer += dt
        else:
            if self.state == VehicleState.JAMMED and self.speed > 0:
                self._reaction_delay = random.uniform(0.8, 2.5)
            self._jam_timer = max(0.0, self._jam_timer - (dt * 2.0))

        self.jam_severity = min(1.0, self._jam_timer / 10.0)

        if self.jam_severity > 0.5:
            self.state = VehicleState.JAMMED
        elif self.speed < 0.5:
            self.state = VehicleState.STOPPED
        elif self.acceleration > 1.0:
            self.state = VehicleState.ACCELERATING
        elif self.acceleration < -1.0:
            self.state = VehicleState.BRAKING
        elif self.state not in (VehicleState.OVERTAKING, VehicleState.TURNING_L,
                                VehicleState.TURNING_R, VehicleState.U_TURN,
                                VehicleState.YIELDING, VehicleState.EMERGENCY):
            self.state = VehicleState.CRUISING

        if self.emergency_mode and self.speed > 50:
            self.state = VehicleState.EMERGENCY

    # ── EMERGENCY ────────────────────────────────────────────────────────────
    def _handle_emergency_corridor(self, ctx: dict):
        for nearby in ctx.get("nearby_vehicles", []):
            if nearby.id != self.id:
                nearby._yield_to_emergency()

    def _yield_to_emergency(self):
        self.prev_state  = self.state
        self.state       = VehicleState.YIELDING
        self._stop_timer = 5.0

    def register_collision(self, other_id: str):
        self.state        = VehicleState.COLLIDED
        self.speed        = 0.0
        self.collision_id = other_id
        self.jam_severity = 1.0

    # ── SERIALIZATION ────────────────────────────────────────────────────────
    def to_dict(self) -> Dict[str, Any]:
        state_vn_map = {
            "cruising":     "Đang di chuyển ổn định",
            "accelerating": "Đang tăng tốc",
            "braking":      "Đang phanh giảm tốc",
            "stopped":      "Đang dừng chờ đèn đỏ",
            "yielding":     "Đang nhường đường xe ưu tiên",
            "overtaking":   "Đang lách vượt xe",
            "turning_left": "Đang rẽ trái giao lộ",
            "turning_right": "Đang rẽ phải giao lộ",
            "u_turn":       "Đang quay đầu xe",
            "emergency":    "Xe ưu tiên đang khẩn cấp",
            "parking":      "Đang đỗ bên đường",
            "collided":     "Bị TAI NẠN GIAO THÔNG",
            "jammed":       "ĐANG BỊ KẸT XE",
        }
        type_vn_map = {
            "car": "Ô tô con", "motorcycle": "Xe máy", "truck": "Xe tải nặng",
            "bus": "Xe buýt đô thị", "ambulance": "Xe cứu thương",
            "police": "Xe cảnh sát", "firefighter": "Xe cứu hỏa",
        }
        return {
            "id":           self.id,
            "type":         self.vehicle_type.value,
            "type_vn":      type_vn_map.get(self.vehicle_type.value, "Phương tiện"),
            "lat":          round(self.lat, 6),
            "lng":          round(self.lng, 6),
            "speed":        round(self.speed, 1),
            "heading":      round(self.heading, 1),
            "state":        self.state.value,
            "state_vn":     state_vn_map.get(self.state.value, "Bình thường"),
            "lane":         self.lane,
            "road_id":      self.road_id,
            "road_name":    self.road_name,
            "plate":        self.plate,
            "acceleration": round(self.acceleration, 2),
            "emergency":    self.emergency_mode,
            "siren":        self.siren_on,
            "size":         f"{self.length}m × {self.width}m",
            "ml_controlled": self.ml_controlled,
            "jam_severity": round(self.jam_severity, 2),
        }

    def to_ws_dict(self) -> Dict[str, Any]:
        """Compact dict cho WebSocket push."""
        return {
            "id":           self.id,
            "type":         self.vehicle_type.value,
            "lat":          round(self.lat, 6),
            "lng":          round(self.lng, 6),
            "speed":        round(self.speed, 1),
            "heading":      round(self.heading, 1),
            "state":        self.state.value,
            "emergency":    self.emergency_mode,
            "jam_severity": round(self.jam_severity, 2),
            "road_id":      self.road_id,
            "plate":        self.plate,
            "size":         f"{self.length}m × {self.width}m",
            "ml_controlled": self.ml_controlled,
            "acceleration": round(self.acceleration, 2),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Vehicle":
        v = cls(vehicle_type=VehicleType(d["type"]), id=d["id"])
        v.lat       = d["lat"]
        v.lng       = d["lng"]
        v.speed     = d["speed"]
        v.heading   = d["heading"]
        v.state     = VehicleState(d["state"])
        v.road_name = d.get("road_name", "Đường chưa rõ")
        return v