"""Synthetic drives along a known road, plus a fake Snap to Roads client."""

from __future__ import annotations

import math
import random

from mileage.geo import haversine_m, path_length_m
from mileage.roads import SnappedPoint
from mileage.trip import GpsPoint, Trip

LAT0, LNG0 = 40.30, -74.70
M_PER_DEG_LAT = 111_320.0
M_PER_DEG_LNG = 111_320.0 * math.cos(math.radians(LAT0))


def to_ll(x: float, y: float) -> tuple[float, float]:
    return LAT0 + y / M_PER_DEG_LAT, LNG0 + x / M_PER_DEG_LNG


def to_xy(lat: float, lng: float) -> tuple[float, float]:
    return (lng - LNG0) * M_PER_DEG_LNG, (lat - LAT0) * M_PER_DEG_LAT


def make_road() -> list[tuple[float, float]]:
    """~6 km road: straight east, a 90-degree curve, north, then a turn west."""
    xy = [(0.0, 0.0), (2000.0, 0.0)]
    cx, cy, r = 2000.0, 300.0, 300.0
    for k in range(1, 19):
        a = -math.pi / 2 + k * (math.pi / 2) / 18
        xy.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    xy += [(2300.0, 2500.0), (1200.0, 2500.0)]
    return [to_ll(x, y) for x, y in xy]


def _point_along(road_xy, s):
    for (x1, y1), (x2, y2) in zip(road_xy, road_xy[1:]):
        seg = math.hypot(x2 - x1, y2 - y1)
        if s <= seg:
            f = s / seg
            return x1 + f * (x2 - x1), y1 + f * (y2 - y1)
        s -= seg
    return road_xy[-1]


def make_trip(seed: int = 1, noise_m: float = 6.0, speed_mps: float = 15.0,
              interval_s: float = 5.0, parked_s: float = 180.0,
              spikes: int = 3) -> tuple[Trip, float]:
    """Return (trip, true road distance in metres)."""
    rng = random.Random(seed)
    road = make_road()
    road_xy = [to_xy(*p) for p in road]
    length = path_length_m(road)
    t0 = 1_760_000_000_000
    pts: list[GpsPoint] = []
    t = 0.0

    # Android reports accuracy as the 68% radius, ~1.5x the per-axis sigma.
    def fix(x, y, speed, acc=1.5 * noise_m):
        lat, lng = to_ll(x + rng.gauss(0, noise_m), y + rng.gauss(0, noise_m))
        pts.append(GpsPoint(t=t0 + int(t * 1000), lat=lat, lng=lng, acc=acc, speed=speed))

    # Parked at the start, phone jittering around.
    while t < parked_s:
        fix(*road_xy[0], speed=0.0)
        t += interval_s
    s = 0.0
    while s < length:
        fix(*_point_along(road_xy, s), speed=speed_mps)
        s += speed_mps * interval_s
        t += interval_s
    end = t + parked_s
    while t < end:
        fix(*road_xy[-1], speed=0.0)
        t += interval_s

    # A few wild single-fix outliers, plus a low-accuracy fix.
    for k in range(spikes):
        i = len(pts) // 2 + 7 * k
        p = pts[i]
        x, y = to_xy(p.lat, p.lng)
        lat, lng = to_ll(x + 250, y - 250)
        pts[i] = GpsPoint(p.t, lat, lng, acc=8.0, speed=p.speed)
    if len(pts) > 60:
        p = pts[60]
        pts[60] = GpsPoint(p.t, p.lat + 0.002, p.lng, acc=120.0, speed=p.speed)

    trip = Trip(trip_id=f"synthetic-{seed}", points=pts,
                odometer_start=1000.0, odometer_end=1000.0 + round(length / 1609.344, 1))
    return trip, length


class FakeRoadsClient:
    """Projects points onto the known road, interpolating road vertices in
    between, like Snap to Roads with interpolate=true."""

    def __init__(self, road: list[tuple[float, float]] | None = None):
        self.road_xy = [to_xy(*p) for p in (road or make_road())]
        self.calls: list[int] = []

    def _project(self, lat, lng):
        px, py = to_xy(lat, lng)
        best = (float("inf"), 0, (0.0, 0.0))
        for k, ((x1, y1), (x2, y2)) in enumerate(zip(self.road_xy, self.road_xy[1:])):
            dx, dy = x2 - x1, y2 - y1
            f = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)))
            qx, qy = x1 + f * dx, y1 + f * dy
            d = math.hypot(px - qx, py - qy)
            if d < best[0]:
                best = (d, k + f, (qx, qy))
        return best[1], best[2]

    def snap(self, path):
        assert 1 <= len(path) <= 100
        self.calls.append(len(path))
        out: list[SnappedPoint] = []
        prev_pos = None
        for i, (lat, lng) in enumerate(path):
            pos, (qx, qy) = self._project(lat, lng)
            if prev_pos is not None and pos > prev_pos:
                for v in range(math.floor(prev_pos) + 1, math.ceil(pos)):
                    out.append(SnappedPoint(*to_ll(*self.road_xy[v]), None))
            out.append(SnappedPoint(*to_ll(qx, qy), i))
            prev_pos = pos
        return out, None


def straight_line_m(trip: Trip) -> float:
    a, b = trip.points[0], trip.points[-1]
    return haversine_m(a.lat, a.lng, b.lat, b.lng)
