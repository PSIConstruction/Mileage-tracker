"""Google Roads API "Snap to Roads" client and batching.

Google accepts at most 100 points per request and works best when consecutive
points are within ~300 m. Long trips are sent in overlapping batches and the
results stitched back together using each snapped point's ``originalIndex``.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Protocol

SNAP_URL = "https://roads.googleapis.com/v1/snapToRoads"
MAX_POINTS_PER_REQUEST = 100


@dataclass(frozen=True)
class SnappedPoint:
    lat: float
    lng: float
    original_index: int | None  # None for points Google interpolated
    place_id: str | None = None


class SnapClient(Protocol):
    def snap(self, path: list[tuple[float, float]]) -> tuple[list[SnappedPoint], str | None]:
        """Snap up to 100 (lat, lng) points. Returns (points, warning message)."""


class RequestBudgetExceeded(RuntimeError):
    pass


class GoogleRoadsClient:
    """Minimal Snap to Roads client.

    ``max_requests`` is a local safety net on top of the quota you should also
    set in Google Cloud Console, so a bug cannot run up a bill.
    """

    def __init__(self, api_key: str | None = None, *, interpolate: bool = True,
                 max_requests: int = 500, timeout_s: float = 30.0):
        self.api_key = api_key or os.environ.get("GOOGLE_MAPS_API_KEY")
        if not self.api_key:
            raise ValueError("Google API key required (set GOOGLE_MAPS_API_KEY)")
        self.interpolate = interpolate
        self.max_requests = max_requests
        self.timeout_s = timeout_s
        self.requests_made = 0

    def snap(self, path):
        if not 1 <= len(path) <= MAX_POINTS_PER_REQUEST:
            raise ValueError(f"path must have 1..{MAX_POINTS_PER_REQUEST} points")
        if self.requests_made >= self.max_requests:
            raise RequestBudgetExceeded(
                f"refusing to exceed {self.max_requests} Roads API requests in one run")
        query = urllib.parse.urlencode({
            "path": "|".join(f"{lat:.6f},{lng:.6f}" for lat, lng in path),
            "interpolate": "true" if self.interpolate else "false",
            "key": self.api_key,
        })
        self.requests_made += 1
        req = urllib.request.Request(f"{SNAP_URL}?{query}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                body = json.load(resp)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            raise RuntimeError(f"Roads API HTTP {e.code}: {detail}") from None
        return parse_snap_response(body), body.get("warningMessage")


def parse_snap_response(body: dict) -> list[SnappedPoint]:
    out = []
    for sp in body.get("snappedPoints", []):
        loc = sp["location"]
        out.append(SnappedPoint(
            lat=float(loc["latitude"]),
            lng=float(loc["longitude"]),
            original_index=sp.get("originalIndex"),
            place_id=sp.get("placeId"),
        ))
    return out


@dataclass
class SnapRun:
    points: list[SnappedPoint]   # original_index refers to the full input path
    requests: int
    warnings: list[str]


def snap_path(path: list[tuple[float, float]], client: SnapClient, *,
              batch_size: int = MAX_POINTS_PER_REQUEST, overlap: int = 2) -> SnapRun:
    """Snap an arbitrarily long path using overlapping batches.

    Each batch after the first re-sends the last ``overlap`` points of the
    previous batch so Google has context at the seam. The stitched result
    keeps the previous batch's output for those overlap points and drops the
    new batch's output up to and including its last overlap point.
    """
    if not 0 <= overlap < batch_size:
        raise ValueError("overlap must be in [0, batch_size)")
    out: list[SnappedPoint] = []
    warnings: list[str] = []
    requests = 0
    start = 0
    n = len(path)
    while start < n:
        end = min(start + batch_size, n)
        snapped, warning = client.snap(path[start:end])
        requests += 1
        if warning:
            warnings.append(f"points {start}-{end - 1}: {warning}")

        if start > 0 and overlap:
            # Drop everything up to the last snapped point that belongs to the
            # overlap region; the previous batch already covered it.
            cut = -1
            for k, sp in enumerate(snapped):
                if sp.original_index is not None and sp.original_index < overlap:
                    cut = k
            snapped = snapped[cut + 1:]

        for sp in snapped:
            idx = None if sp.original_index is None else sp.original_index + start
            out.append(SnappedPoint(sp.lat, sp.lng, idx, sp.place_id))

        if end == n:
            break
        start = end - overlap
    return SnapRun(points=out, requests=requests, warnings=warnings)
