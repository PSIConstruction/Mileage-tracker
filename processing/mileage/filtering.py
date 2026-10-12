"""Sanity filtering of raw GPS points before map matching.

Nothing here mutates the raw trip. The filter returns the indices of points it
kept plus a reason for every point it rejected, so a reviewer can see exactly
what was discarded and why.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .geo import haversine_m
from .trip import GpsPoint


@dataclass(frozen=True)
class FilterConfig:
    # Reject fixes whose reported horizontal accuracy is worse than this.
    max_accuracy_m: float = 50.0
    # Reject fixes that imply the vehicle moved faster than this (~134 mph)
    # from the last kept point: a GPS "teleport".
    max_implied_speed_mps: float = 60.0
    # Parked jitter. When the phone's Doppler speed says the vehicle is
    # stopped, fixes are noise unless they have moved well beyond the GPS
    # accuracy circle around the last kept fix (catches slow creeping).
    # Without a speed reading, a smaller radius is used.
    stationary_speed_mps: float = 1.0
    stopped_radius_m: float = 25.0
    stopped_radius_acc_factor: float = 3.0
    no_speed_radius_m: float = 10.0
    no_speed_radius_acc_factor: float = 1.5
    # Out-and-back spike: A -> B -> C where the detour through B is far
    # longer than going straight from A to C. A real vehicle can't do that
    # at speed (a U-turn at >30 mph inside 10 s); a single bad fix can.
    spike_min_leg_m: float = 75.0
    spike_min_excess_m: float = 150.0


@dataclass
class FilterResult:
    kept: list[int]
    rejected: dict[int, str] = field(default_factory=dict)

    def points(self, all_points: list[GpsPoint]) -> list[GpsPoint]:
        return [all_points[i] for i in self.kept]


def filter_points(points: list[GpsPoint], cfg: FilterConfig = FilterConfig()) -> FilterResult:
    rejected: dict[int, str] = {}
    kept: list[int] = []

    for i, p in enumerate(points):
        if p.acc is not None and p.acc > cfg.max_accuracy_m:
            rejected[i] = f"accuracy {p.acc:.0f} m > {cfg.max_accuracy_m:.0f} m"
            continue
        if not kept:
            kept.append(i)
            continue

        last = points[kept[-1]]
        dt = (p.t - last.t) / 1000.0
        if dt <= 0:
            rejected[i] = "duplicate or out-of-order timestamp"
            continue

        d = haversine_m(last.lat, last.lng, p.lat, p.lng)
        if d / dt > cfg.max_implied_speed_mps:
            # Either this fix is bad, or the last kept one was a bad fix that
            # slipped just under the limit. If this fix is plausible from the
            # one before last and the last kept fix is a detour between them,
            # blame the last kept fix instead.
            if (len(kept) >= 2 and _plausible(points[kept[-2]], p, cfg)
                    and _is_detour(points[kept[-2]], last, p, cfg)):
                rejected[kept.pop()] = "teleport: inconsistent with neighbours"
                last = points[kept[-1]]
                d = haversine_m(last.lat, last.lng, p.lat, p.lng)
            else:
                rejected[i] = f"implied speed {d / dt:.0f} m/s from previous fix"
                continue

        acc = p.acc or 0.0
        if p.speed is None:
            radius = max(cfg.no_speed_radius_m, cfg.no_speed_radius_acc_factor * acc)
        elif p.speed < cfg.stationary_speed_mps:
            radius = max(cfg.stopped_radius_m, cfg.stopped_radius_acc_factor * acc)
        else:
            radius = 0.0
        if d < radius:
            rejected[i] = "stationary jitter"
            continue

        kept.append(i)

    # Second pass: single-point out-and-back spikes.
    changed = True
    while changed and len(kept) >= 3:
        changed = False
        for j in range(1, len(kept) - 1):
            a, b, c = (points[kept[j - 1]], points[kept[j]], points[kept[j + 1]])
            if _is_detour(a, b, c, cfg):
                rejected[kept[j]] = "spike: out-and-back detour"
                del kept[j]
                changed = True
                break

    return FilterResult(kept=kept, rejected=dict(sorted(rejected.items())))


def _plausible(a: GpsPoint, b: GpsPoint, cfg: FilterConfig) -> bool:
    dt = (b.t - a.t) / 1000.0
    return dt > 0 and haversine_m(a.lat, a.lng, b.lat, b.lng) / dt <= cfg.max_implied_speed_mps


def _is_detour(a: GpsPoint, b: GpsPoint, c: GpsPoint, cfg: FilterConfig) -> bool:
    """True if going A -> B -> C is implausibly longer than A -> C."""
    ab = haversine_m(a.lat, a.lng, b.lat, b.lng)
    bc = haversine_m(b.lat, b.lng, c.lat, c.lng)
    ac = haversine_m(a.lat, a.lng, c.lat, c.lng)
    return (ab > cfg.spike_min_leg_m and bc > cfg.spike_min_leg_m
            and ab + bc > 2 * ac + cfg.spike_min_excess_m)
