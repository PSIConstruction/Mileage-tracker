"""Trip file format shared between the phone app and the processing pipeline.

A trip file is the *raw evidence* for a drive. The pipeline only ever reads
it; processed results are written to a separate file so the raw GPS can
always be re-processed with a better algorithm later.

Format ``psi-mileage-trip/1``::

    {
      "format": "psi-mileage-trip/1",
      "tripId": "2026-10-12T08-14-03_ab12",
      "employee": "Saadya",            # optional
      "vehicle": "F-150 #2",           # optional
      "startedAt": 1760271243000,      # epoch ms
      "endedAt": 1760272323000,        # epoch ms
      "odometerStart": 51284.2,        # miles, optional (validation drives)
      "odometerEnd": 51297.8,          # miles, optional
      "notes": "Shop -> Newman",       # optional
      "points": [
        {"t": 1760271243000,          # epoch ms (GPS fix time)
         "lat": 40.123456, "lng": -74.123456,
         "acc": 4.5,                  # horizontal accuracy, metres (68%)
         "speed": 12.3,               # m/s, optional
         "bearing": 87.0,             # degrees, optional
         "alt": 31.2,                 # metres, optional
         "provider": "fused"}         # optional
      ]
    }
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

FORMAT = "psi-mileage-trip/1"


@dataclass(frozen=True)
class GpsPoint:
    t: int  # epoch milliseconds
    lat: float
    lng: float
    acc: float | None = None
    speed: float | None = None
    bearing: float | None = None
    alt: float | None = None
    provider: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "GpsPoint":
        return cls(
            t=int(d["t"]),
            lat=float(d["lat"]),
            lng=float(d["lng"]),
            acc=_opt_float(d.get("acc")),
            speed=_opt_float(d.get("speed")),
            bearing=_opt_float(d.get("bearing")),
            alt=_opt_float(d.get("alt")),
            provider=d.get("provider"),
        )


@dataclass
class Trip:
    trip_id: str
    points: list[GpsPoint]
    started_at: int | None = None
    ended_at: int | None = None
    employee: str | None = None
    vehicle: str | None = None
    odometer_start: float | None = None
    odometer_end: float | None = None
    notes: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def odometer_miles(self) -> float | None:
        if self.odometer_start is None or self.odometer_end is None:
            return None
        return round(self.odometer_end - self.odometer_start, 1)

    @classmethod
    def from_dict(cls, d: dict) -> "Trip":
        fmt = d.get("format")
        if fmt != FORMAT:
            raise ValueError(f"unsupported trip format {fmt!r}, expected {FORMAT!r}")
        known = {"format", "tripId", "points", "startedAt", "endedAt", "employee",
                 "vehicle", "odometerStart", "odometerEnd", "notes"}
        return cls(
            trip_id=str(d["tripId"]),
            points=[GpsPoint.from_dict(p) for p in d.get("points", [])],
            started_at=d.get("startedAt"),
            ended_at=d.get("endedAt"),
            employee=d.get("employee"),
            vehicle=d.get("vehicle"),
            odometer_start=_opt_float(d.get("odometerStart")),
            odometer_end=_opt_float(d.get("odometerEnd")),
            notes=d.get("notes"),
            extra={k: v for k, v in d.items() if k not in known},
        )

    @classmethod
    def load(cls, path: str | Path) -> "Trip":
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(json.load(f))


def _opt_float(v) -> float | None:
    return None if v is None else float(v)
