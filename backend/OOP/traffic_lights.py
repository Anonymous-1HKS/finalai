"""
OOP/traffic_lights.py
Đèn giao thông với 3 pha (đỏ/vàng/xanh), đếm ngược, ML override chu kỳ,
ưu tiên xe cứu thương, thống kê hàng đợi.

FIX:
- Không random tên trong __post_init__ — tên set từ ngoài hoặc dùng ID
- Thêm last_phase_change timestamp
- to_ws_dict() compact cho WebSocket
"""

import uuid
import time
import math
import random
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
from enum import Enum


class LightPhase(Enum):
    RED    = "red"
    YELLOW = "yellow"
    GREEN  = "green"


class LocationType(Enum):
    INTERSECTION_3WAY  = "intersection_3way"
    INTERSECTION_4WAY  = "intersection_4way"
    INTERSECTION_MULTI = "intersection_multi"
    ROUNDABOUT         = "roundabout"
    PEDESTRIAN_ONLY    = "pedestrian_only"


@dataclass
class TrafficLightCycle:
    red_duration:    float = 30.0
    yellow_duration: float = 4.0
    green_duration:  float = 25.0

    def total(self) -> float:
        return self.red_duration + self.yellow_duration + self.green_duration


@dataclass
class TrafficLight:
    id:      str   = field(default_factory=lambda: f"TL-{str(uuid.uuid4())[:6]}")
    lat:     float = 0.0
    lng:     float = 0.0
    road_id: str   = ""
    name:    str   = ""

    location_type: LocationType = LocationType.INTERSECTION_4WAY
    junction_id:   Optional[str] = None
    heading:       float = 0.0

    phase:     LightPhase        = LightPhase.RED
    countdown: float             = 30.0
    cycle:     TrafficLightCycle = field(default_factory=TrafficLightCycle)

    queue_length: int  = 0
    queue_ids:    List = field(default_factory=list)

    ml_adjusted:       bool            = False
    ml_red_override:   Optional[float] = None
    ml_green_override: Optional[float] = None

    preempted:     bool  = False
    preempt_timer: float = 0.0

    total_cycles:    int = 0
    vehicles_served: int = 0

    red_grace_period:   float = 2.5
    _time_in_red:       float = field(default=0.0, repr=False)
    _last_phase_change: float = field(default_factory=time.time, repr=False)

    def __post_init__(self):
        if not self.name:
            type_label = {
                LocationType.INTERSECTION_3WAY:  "Ngã ba",
                LocationType.INTERSECTION_4WAY:  "Ngã tư",
                LocationType.INTERSECTION_MULTI: "Nút giao đa hướng",
                LocationType.ROUNDABOUT:         "Vòng xuyến",
                LocationType.PEDESTRIAN_ONLY:    "Điểm sang đường",
            }
            self.name = f"{type_label.get(self.location_type, 'Đèn')} {self.id}"
        self._last_phase_change = time.time()

    @property
    def display_countdown(self) -> int:
        return max(0, math.ceil(self.countdown))

    def update(self, dt: float):
        if self.preempted:
            self.phase     = LightPhase.GREEN
            self.countdown = max(0.0, self.countdown - dt)
            self.preempt_timer -= dt
            if self.preempt_timer <= 0:
                self.preempted = False
                self.countdown = self._get_green_duration()
            return

        if self.phase == LightPhase.RED:
            self._time_in_red += dt
        else:
            self._time_in_red = 0.0

        self.countdown -= dt
        if self.countdown <= 0:
            self._advance_phase()

    def _advance_phase(self):
        self._last_phase_change = time.time()
        if self.phase == LightPhase.RED:
            self.phase        = LightPhase.GREEN
            self.countdown    = self._get_green_duration()
            self._time_in_red = 0.0
        elif self.phase == LightPhase.GREEN:
            self.phase     = LightPhase.YELLOW
            self.countdown = self.cycle.yellow_duration
        elif self.phase == LightPhase.YELLOW:
            self.phase     = LightPhase.RED
            self.countdown = self._get_red_duration()
            self.total_cycles    += 1
            self.vehicles_served += self.queue_length
            self._time_in_red     = 0.0

    def _get_red_duration(self) -> float:
        return self.ml_red_override if self.ml_red_override else self.cycle.red_duration

    def _get_green_duration(self) -> float:
        return self.ml_green_override if self.ml_green_override else self.cycle.green_duration

    def is_red_grace_period(self) -> bool:
        return self.phase == LightPhase.RED and self._time_in_red < self.red_grace_period

    def is_yellow_risky(self) -> bool:
        return self.phase == LightPhase.YELLOW and self.countdown < 1.5

    def applies_to_vehicle(self, vehicle_heading: float) -> bool:
        diff = abs((vehicle_heading - self.heading + 360) % 360)
        return 135 <= diff <= 225

    def ml_set_cycle(self, red: Optional[float] = None, green: Optional[float] = None):
        if red   is not None:
            self.ml_red_override   = max(5.0, red)
        if green is not None:
            self.ml_green_override = max(5.0, green)
        self.ml_adjusted = True

    def ml_force_green(self, duration: float = 15.0):
        self.phase       = LightPhase.GREEN
        self.countdown   = duration
        self.ml_adjusted = True

    def ml_extend_green(self, extra: float):
        if self.phase == LightPhase.GREEN:
            self.countdown  += extra
            self.ml_adjusted = True

    def preempt(self, duration: float = 20.0):
        self.preempted          = True
        self.phase              = LightPhase.GREEN
        self.preempt_timer      = duration
        self.countdown          = duration
        self._last_phase_change = time.time()

    def register_vehicle(self, vehicle_id: str):
        if vehicle_id not in self.queue_ids:
            self.queue_ids.append(vehicle_id)
            self.queue_length = len(self.queue_ids)

    def deregister_vehicle(self, vehicle_id: str):
        self.queue_ids    = [v for v in self.queue_ids if v != vehicle_id]
        self.queue_length = len(self.queue_ids)

    def to_dict(self) -> Dict[str, Any]:
        phase_vn_map = {"red": "Đèn Đỏ", "yellow": "Đèn Vàng", "green": "Đèn Xanh"}
        type_vn_map = {
            "intersection_3way":  "Ngã ba đường",
            "intersection_4way":  "Ngã tư đường",
            "intersection_multi": "Nút giao đa hướng (Ngã 5+)",
            "roundabout":         "Vòng xuyến / Bùng binh",
            "pedestrian_only":    "Điểm sang đường độc lập",
        }
        return {
            "id":               self.id,
            "lat":              self.lat,
            "lng":              self.lng,
            "road_id":          self.road_id,
            "junction_id":      self.junction_id,
            "location_type":    self.location_type.value,
            "location_type_vn": type_vn_map.get(self.location_type.value, "Nút giao"),
            "name":             self.name,
            "phase":            self.phase.value,
            "phase_vn":         phase_vn_map.get(self.phase.value, "Không rõ"),
            "countdown_exact":  round(self.countdown, 2),
            "display_seconds":  self.display_countdown,
            "cycle": {
                "red":    self._get_red_duration(),
                "yellow": self.cycle.yellow_duration,
                "green":  self._get_green_duration(),
            },
            "queue_length":      self.queue_length,
            "ml_adjusted":       self.ml_adjusted,
            "preempted":         self.preempted,
            "red_grace":         self.is_red_grace_period(),
            "yellow_risky":      self.is_yellow_risky(),
            "total_cycles":      self.total_cycles,
            "vehicles_served":   self.vehicles_served,
            "last_phase_change": self._last_phase_change,
        }

    def to_ws_dict(self) -> Dict[str, Any]:
        """Compact dict cho WebSocket push."""
        return {
            "id":              self.id,
            "lat":             self.lat,
            "lng":             self.lng,
            "phase":           self.phase.value,
            "display_seconds": self.display_countdown,
            "countdown_exact": round(self.countdown, 2),
            "queue_length":    self.queue_length,
            "ml_adjusted":     self.ml_adjusted,
            "preempted":       self.preempted,
            "red_grace":       self.is_red_grace_period(),
            "yellow_risky":    self.is_yellow_risky(),
            "name":            self.name,
            "location_type":   self.location_type.value,
            "cycle": {
                "red":    self._get_red_duration(),
                "yellow": self.cycle.yellow_duration,
                "green":  self._get_green_duration(),
            },
        }