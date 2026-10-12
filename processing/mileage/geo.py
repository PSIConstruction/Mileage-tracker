"""Small geodesy helpers."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

EARTH_RADIUS_M = 6_371_008.8
METERS_PER_MILE = 1609.344


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def path_length_m(coords: Iterable[Sequence[float]]) -> float:
    """Sum of segment lengths along a (lat, lng) polyline."""
    total = 0.0
    prev = None
    for c in coords:
        if prev is not None:
            total += haversine_m(prev[0], prev[1], c[0], c[1])
        prev = c
    return total


def miles(meters: float) -> float:
    return meters / METERS_PER_MILE
