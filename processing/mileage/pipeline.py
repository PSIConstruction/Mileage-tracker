"""raw GPS -> sanity filtering -> map matching -> road geometry -> mileage."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

from .filtering import FilterConfig, filter_points
from .geo import haversine_m, miles, path_length_m
from .roads import SnapClient, snap_path
from .trip import GpsPoint, Trip

PIPELINE_VERSION = "1"


@dataclass(frozen=True)
class PipelineConfig:
    filter: FilterConfig = FilterConfig()
    # Split the trip where consecutive kept fixes are this far apart in
    # distance or time (lost signal, phone killed the app, etc.). Snapping
    # across such a hole is guesswork, so each side is matched separately and
    # the hole is bridged with a straight line and flagged.
    gap_split_m: float = 1000.0
    gap_split_s: float = 300.0
    # Google recommends consecutive points within ~300 m.
    spacing_warn_m: float = 300.0
    batch_overlap: int = 2


@dataclass
class Segment:
    first_index: int  # index into trip.points
    last_index: int
    filtered_m: float
    matched_m: float | None
    snapped: list[list[float]] = field(default_factory=list)  # [lat, lng]


@dataclass
class TripResult:
    trip_id: str
    pipeline_version: str
    processed_at: int
    point_count: int
    kept_count: int
    rejected: dict[int, str]
    raw_m: float
    filtered_m: float
    matched_m: float | None
    gap_m: float  # straight-line bridges between segments
    end_connector_m: float  # trip start/end -> nearest snapped road point
    segments: list[Segment]
    warnings: list[str]
    roads_requests: int
    odometer_miles: float | None

    @property
    def raw_miles(self) -> float:
        return miles(self.raw_m)

    @property
    def filtered_miles(self) -> float:
        return miles(self.filtered_m + self.gap_m)

    @property
    def matched_miles(self) -> float | None:
        """Reimbursable mileage: matched road distance plus flagged gaps."""
        if self.matched_m is None:
            return None
        return miles(self.matched_m + self.gap_m)

    @property
    def matched_plus_ends_miles(self) -> float | None:
        """Matched mileage plus the off-road legs at each end (parking lots,
        driveways). Kept separate until odometer tests show which is right."""
        if self.matched_m is None:
            return None
        return miles(self.matched_m + self.gap_m + self.end_connector_m)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["rejected"] = {str(k): v for k, v in self.rejected.items()}
        d["summary_miles"] = {
            "raw": round(self.raw_miles, 3),
            "filtered": round(self.filtered_miles, 3),
            "matched": None if self.matched_miles is None else round(self.matched_miles, 3),
            "matched_plus_ends": (None if self.matched_plus_ends_miles is None
                                  else round(self.matched_plus_ends_miles, 3)),
            "odometer": self.odometer_miles,
        }
        return d


def _split_segments(points: list[GpsPoint], kept: list[int], cfg: PipelineConfig,
                    warnings: list[str]) -> tuple[list[list[int]], float]:
    segments: list[list[int]] = []
    gap_m = 0.0
    cur: list[int] = []
    for i in kept:
        if cur:
            a, b = points[cur[-1]], points[i]
            d = haversine_m(a.lat, a.lng, b.lat, b.lng)
            dt = (b.t - a.t) / 1000.0
            if d > cfg.gap_split_m or dt > cfg.gap_split_s:
                warnings.append(
                    f"gap between points {cur[-1]} and {i}: {d:.0f} m in {dt:.0f} s; "
                    f"counted as straight line (may under-count)")
                gap_m += d
                segments.append(cur)
                cur = []
            elif d > cfg.spacing_warn_m:
                warnings.append(
                    f"points {cur[-1]} and {i} are {d:.0f} m apart "
                    f"(> {cfg.spacing_warn_m:.0f} m recommended)")
        cur.append(i)
    if cur:
        segments.append(cur)
    return segments, gap_m


def process_trip(trip: Trip, client: SnapClient | None,
                 cfg: PipelineConfig = PipelineConfig()) -> TripResult:
    """Run the full pipeline. With ``client=None`` only raw and filtered
    distances are computed (offline mode, no API calls)."""
    pts = trip.points
    warnings: list[str] = []
    raw_m = path_length_m((p.lat, p.lng) for p in pts)

    fr = filter_points(pts, cfg.filter)
    seg_indices, gap_m = _split_segments(pts, fr.kept, cfg, warnings)

    segments: list[Segment] = []
    requests = 0
    matched_total: float | None = 0.0 if client is not None else None
    end_connector_m = 0.0

    for idxs in seg_indices:
        coords = [(pts[i].lat, pts[i].lng) for i in idxs]
        seg = Segment(first_index=idxs[0], last_index=idxs[-1],
                      filtered_m=path_length_m(coords), matched_m=None)
        if client is not None and len(coords) >= 2:
            run = snap_path(coords, client, overlap=cfg.batch_overlap)
            requests += run.requests
            warnings.extend(run.warnings)
            if run.points:
                seg.snapped = [[sp.lat, sp.lng] for sp in run.points]
                seg.matched_m = path_length_m(seg.snapped)
            else:
                warnings.append(
                    f"segment {idxs[0]}-{idxs[-1]}: no road match; "
                    f"using filtered GPS distance")
                seg.matched_m = seg.filtered_m
            matched_total += seg.matched_m
        elif client is not None:
            seg.matched_m = 0.0
        segments.append(seg)

    if client is not None and segments:
        first, last = segments[0], segments[-1]
        if first.snapped:
            p = pts[first.first_index]
            end_connector_m += haversine_m(p.lat, p.lng, *first.snapped[0])
        if last.snapped:
            p = pts[last.last_index]
            end_connector_m += haversine_m(p.lat, p.lng, *last.snapped[-1])

    return TripResult(
        trip_id=trip.trip_id,
        pipeline_version=PIPELINE_VERSION,
        processed_at=int(time.time() * 1000),
        point_count=len(pts),
        kept_count=len(fr.kept),
        rejected=fr.rejected,
        raw_m=raw_m,
        filtered_m=sum(s.filtered_m for s in segments),
        matched_m=matched_total,
        gap_m=gap_m,
        end_connector_m=end_connector_m,
        segments=segments,
        warnings=warnings,
        roads_requests=requests,
        odometer_miles=trip.odometer_miles,
    )
