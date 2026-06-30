"""
OOP/sign_roads_pedestrian.py
Quản lý Biển báo giao thông, Vạch kẻ đường cho người đi bộ,
và cấu trúc dữ liệu liên kết thông tin đường bộ.
"""

import uuid, math, random
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Dict, Any


class SignType(Enum):
    SPEED_LIMIT   = "speed_limit"
    NO_OVERTAKING = "no_overtaking"
    NO_U_TURN     = "no_u_turn"
    BRIDGE_AHEAD  = "bridge_ahead"
    ROUNDABOUT    = "roundabout"
    DANGER_ZONE   = "danger_zone"


class JunctionType(Enum):
    INTERSECTION_3WAY  = "intersection_3way"
    INTERSECTION_4WAY  = "intersection_4way"
    INTERSECTION_MULTI = "intersection_multi"
    ROUNDABOUT         = "roundabout"
    BRIDGE             = "bridge"
    PEDESTRIAN_ONLY    = "pedestrian_only"


class RoadType(Enum):
    MOTORWAY    = "motorway"
    PRIMARY     = "primary"
    SECONDARY   = "secondary"
    TERTIARY    = "tertiary"
    RESIDENTIAL = "residential"
    SERVICE     = "service"
    PATH        = "path"


@dataclass
class RoadMetadata:
    road_id:     str
    osm_id:      str
    name:        str
    road_type:   str
    lane_count:  int   = 2
    one_way:     bool  = False
    speed_limit: float = 60.0
    current_flow_density: float = 0.0
    status:      str   = "Bình thường"
    vehicles_present: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "road_id":          self.road_id,
            "osm_id":           self.osm_id,
            "name":             self.name if self.name else f"Đường #{self.road_id}",
            "road_type":        self.road_type,
            "lane_count":       self.lane_count if self.lane_count else 2,
            "one_way":          "Có" if self.one_way else "Không",
            "speed_limit":      self.speed_limit,
            "flow_density_pct": round(self.current_flow_density * 100, 1),
            "status":           self.status,
            "vehicles_present": self.vehicles_present,
        }


@dataclass
class TrafficSign:
    sign_type:   SignType
    value:       float = 0.0
    id:          str   = field(default_factory=lambda: f"SIGN-{str(uuid.uuid4())[:6]}")
    lat:         float = 0.0
    lng:         float = 0.0
    road_id:     Optional[str] = None
    road_name:   str = "Không rõ tên đường"
    description: str = ""

    def __post_init__(self):
        if not self.description:
            if self.sign_type == SignType.SPEED_LIMIT:
                self.description = f"Biển giới hạn tốc độ {int(self.value)} km/h"
            elif self.sign_type == SignType.BRIDGE_AHEAD:
                self.description = "Sắp đến đoạn lên cầu - Chú ý giảm tốc"
            elif self.sign_type == SignType.ROUNDABOUT:
                self.description = "Giao lộ vòng xuyến - Nhường đường xe bên trái"
            elif self.sign_type == SignType.NO_U_TURN:
                self.description = "Cấm các phương tiện quay đầu xe tại đoạn này"
            elif self.sign_type == SignType.NO_OVERTAKING:
                self.description = "Đoạn đường khuất tầm nhìn - Cấm vượt xe"
            else:
                self.description = f"Biển báo {self.sign_type.value}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id":          self.id,
            "type":        self.sign_type.value,
            "value":       self.value,
            "lat":         self.lat,
            "lng":         self.lng,
            "road_id":     self.road_id,
            "road_name":   self.road_name,
            "description": self.description,
        }


@dataclass
class PedestrianCrosswalk:
    id:          str   = field(default_factory=lambda: f"CROSS-{str(uuid.uuid4())[:6]}")
    lat:         float = 0.0
    lng:         float = 0.0
    road_id:     Optional[str] = None
    junction_id: Optional[str] = None
    is_active:   bool  = False
    pedestrian_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id":               self.id,
            "lat":              self.lat,
            "lng":              self.lng,
            "road_id":          self.road_id,
            "junction_id":      self.junction_id,
            "is_active":        self.is_active,
            "pedestrian_count": self.pedestrian_count,
            "label":            f"Vạch đi bộ ({self.pedestrian_count} người)",
        }


class PedestrianState(Enum):
    WALKING  = "walking"
    WAITING  = "waiting"
    CROSSING = "crossing"


@dataclass
class Pedestrian:
    id:      str   = field(default_factory=lambda: f"PED-{str(uuid.uuid4())[:6]}")
    lat:     float = 0.0
    lng:     float = 0.0
    heading: float = 0.0
    speed:   float = 1.2
    state:   PedestrianState = PedestrianState.WALKING
    _wait_timer: float = 0.0

    def update(self, dt: float, ctx: dict):
        if self.state == PedestrianState.WAITING:
            self._wait_timer -= dt
            if self._wait_timer <= 0:
                self.state = PedestrianState.CROSSING
            return

        if random.random() < 0.05:
            self.heading = (self.heading + random.uniform(-40, 40)) % 360

        if self.state == PedestrianState.WALKING and random.random() < 0.01:
            self.state = PedestrianState.WAITING
            self._wait_timer = random.uniform(3, 10)
            return

        if self.state == PedestrianState.CROSSING and random.random() < 0.05:
            self.state = PedestrianState.WALKING

        dist_m = self.speed * dt
        rad    = math.radians(self.heading)
        dlat   = (dist_m / 111320) * math.cos(rad)
        dlng   = (dist_m / (111320 * math.cos(math.radians(self.lat)))) * math.sin(rad)
        self.lat += dlat
        self.lng += dlng

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id":      self.id,
            "lat":     round(self.lat, 6),
            "lng":     round(self.lng, 6),
            "heading": round(self.heading, 1),
            "speed":   round(self.speed, 2),
            "state":   self.state.value,
        }

    def to_ws_dict(self) -> Dict[str, Any]:
        return self.to_dict()


@dataclass
class ComplexJunction:
    id:              str         = field(default_factory=lambda: f"JNC-{str(uuid.uuid4())[:6]}")
    name:            str         = "Nút giao thông"
    junction_type:   JunctionType = JunctionType.INTERSECTION_4WAY
    lat:             float       = 0.0
    lng:             float       = 0.0
    connected_roads: List[str]   = field(default_factory=list)
    traffic_light_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id":              self.id,
            "name":            self.name,
            "type":            self.junction_type.value,
            "lat":             self.lat,
            "lng":             self.lng,
            "connected_roads": self.connected_roads,
            "traffic_lights":  self.traffic_light_ids,
            "total_branches":  len(self.connected_roads),
        }


# Alias để tương thích
Road = RoadMetadata