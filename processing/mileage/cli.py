"""Command line entry point.

    mileage process TRIP.json [--offline]      # one trip, prints summary
    mileage validate TRIPS_DIR [--offline]     # odometer comparison report
    mileage gpx TRIP.json                      # raw vs matched track for a map viewer
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from .pipeline import PIPELINE_VERSION, TripResult, process_trip
from .roads import GoogleRoadsClient
from .trip import Trip


def _client(args):
    if args.offline:
        return None
    return GoogleRoadsClient(max_requests=args.max_requests)


def _processed_path(trip_path: Path, trip: Trip) -> Path:
    return trip_path.parent / "processed" / f"{trip.trip_id}.json"


def _process_cached(trip_path: Path, client, force: bool) -> tuple[Trip, dict]:
    """Process a trip, reusing a saved result unless forced. Results are
    stored next to (never on top of) the raw trip file."""
    trip = Trip.load(trip_path)
    out = _processed_path(trip_path, trip)
    if out.exists() and not force:
        saved = json.loads(out.read_text())
        has_match = saved.get("matched_m") is not None
        if saved.get("pipeline_version") == PIPELINE_VERSION and (has_match or client is None):
            return trip, saved
    result = process_trip(trip, client).to_dict()
    if client is not None:
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps(result, indent=1))
    return trip, result


def _fmt(v, nd=2):
    return "-" if v is None else f"{v:.{nd}f}"


def _err(v, ref):
    if v is None or ref is None:
        return None
    return v - ref


def _pct(v, ref):
    if v is None or not ref:
        return None
    return 100.0 * (v - ref) / ref


def cmd_process(args) -> int:
    client = _client(args)
    path = Path(args.trip)
    trip, r = _process_cached(path, client, args.force)
    s = r["summary_miles"]
    print(f"Trip {trip.trip_id}: {r['point_count']} points, {r['kept_count']} kept, "
          f"{r['roads_requests']} Roads API request(s)")
    print(f"  raw GPS:            {_fmt(s['raw'])} mi")
    print(f"  filtered GPS:       {_fmt(s['filtered'])} mi")
    print(f"  road-matched:       {_fmt(s['matched'])} mi")
    print(f"  matched + ends:     {_fmt(s['matched_plus_ends'])} mi")
    if s["odometer"] is not None:
        print(f"  odometer:           {_fmt(s['odometer'], 1)} mi")
    if args.verbose:
        for idx, why in r["rejected"].items():
            print(f"  rejected #{idx}: {why}")
    for w in r["warnings"]:
        print(f"  warning: {w}")
    return 0


COLUMNS = ["trip_id", "employee", "vehicle", "notes", "points", "rejected", "odometer_mi",
           "raw_mi", "filtered_mi", "matched_mi", "matched_plus_ends_mi",
           "raw_err_mi", "filtered_err_mi", "matched_err_mi", "matched_plus_ends_err_mi",
           "matched_err_pct", "warnings"]


def cmd_validate(args) -> int:
    client = _client(args)
    trip_dir = Path(args.trips_dir)
    rows = []
    for path in sorted(trip_dir.glob("*.json")):
        try:
            trip, r = _process_cached(path, client, args.force)
        except (ValueError, KeyError) as e:
            print(f"skipping {path.name}: {e}", file=sys.stderr)
            continue
        s = r["summary_miles"]
        odo = s["odometer"]
        rows.append({
            "trip_id": trip.trip_id, "employee": trip.employee or "",
            "vehicle": trip.vehicle or "", "notes": trip.notes or "",
            "points": r["point_count"], "rejected": len(r["rejected"]),
            "odometer_mi": odo, "raw_mi": s["raw"], "filtered_mi": s["filtered"],
            "matched_mi": s["matched"], "matched_plus_ends_mi": s["matched_plus_ends"],
            "raw_err_mi": _err(s["raw"], odo), "filtered_err_mi": _err(s["filtered"], odo),
            "matched_err_mi": _err(s["matched"], odo),
            "matched_plus_ends_err_mi": _err(s["matched_plus_ends"], odo),
            "matched_err_pct": _pct(s["matched"], odo),
            "warnings": len(r["warnings"]),
        })

    if not rows:
        print("no trips found", file=sys.stderr)
        return 1

    print(f"{'trip':<28} {'odo':>6} {'raw':>7} {'filt':>7} {'match':>7} {'m+ends':>7} "
          f"{'err':>6} {'err%':>6}")
    for row in rows:
        print(f"{row['trip_id'][:28]:<28} {_fmt(row['odometer_mi'], 1):>6} "
              f"{_fmt(row['raw_mi']):>7} {_fmt(row['filtered_mi']):>7} "
              f"{_fmt(row['matched_mi']):>7} {_fmt(row['matched_plus_ends_mi']):>7} "
              f"{_fmt(row['matched_err_mi']):>6} {_fmt(row['matched_err_pct'], 1):>6}")

    print("\nMean absolute error vs odometer (trips with odometer readings):")
    for key in ("raw", "filtered", "matched", "matched_plus_ends"):
        errs = [abs(r[f"{key}_err_mi"]) for r in rows if r[f"{key}_err_mi"] is not None]
        if errs:
            print(f"  {key:<18} {sum(errs) / len(errs):.2f} mi  "
                  f"(worst {max(errs):.2f} mi, n={len(errs)})")
    print("Note: odometers read to 0.1 mi, so +/-0.1 mi is within measurement error.")

    if args.csv:
        with open(args.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=COLUMNS)
            w.writeheader()
            w.writerows(rows)
        print(f"\nwrote {args.csv}")
    return 0


def cmd_gpx(args) -> int:
    client = _client(args)
    path = Path(args.trip)
    trip, r = _process_cached(path, client, args.force)
    rejected = {int(k) for k in r["rejected"]}

    def trk(name, coords):
        pts = "\n".join(f'      <trkpt lat="{la:.7f}" lon="{lo:.7f}"/>' for la, lo in coords)
        return f"  <trk><name>{name}</name><trkseg>\n{pts}\n    </trkseg></trk>"

    raw = [(p.lat, p.lng) for p in trip.points]
    kept = [(p.lat, p.lng) for i, p in enumerate(trip.points) if i not in rejected]
    parts = [trk("raw GPS", raw), trk("filtered GPS", kept)]
    for n, seg in enumerate(r["segments"]):
        if seg["snapped"]:
            parts.append(trk(f"road-matched {n + 1}", seg["snapped"]))
    wpts = "\n".join(
        f'  <wpt lat="{trip.points[i].lat:.7f}" lon="{trip.points[i].lng:.7f}">'
        f"<name>rejected #{i}</name></wpt>" for i in sorted(rejected))
    gpx = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<gpx version="1.1" creator="psi-mileage" xmlns="http://www.topografix.com/GPX/1/1">\n'
           f"{wpts}\n" + "\n".join(parts) + "\n</gpx>\n")
    out = Path(args.out) if args.out else path.with_suffix(".gpx")
    out.write_text(gpx)
    print(f"wrote {out}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mileage", description="PSI mileage processing")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--offline", action="store_true",
                       help="skip Google map matching (raw + filtered only)")
        p.add_argument("--force", action="store_true", help="ignore saved results")
        p.add_argument("--max-requests", type=int, default=500,
                       help="hard cap on Roads API requests per run (default 500)")

    p = sub.add_parser("process", help="process one trip file")
    p.add_argument("trip")
    p.add_argument("-v", "--verbose", action="store_true", help="list rejected points")
    common(p)
    p.set_defaults(func=cmd_process)

    p = sub.add_parser("validate", help="compare every trip in a folder to its odometer")
    p.add_argument("trips_dir")
    p.add_argument("--csv", help="also write the comparison to this CSV file")
    common(p)
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("gpx", help="write raw/filtered/matched tracks as GPX")
    p.add_argument("trip")
    p.add_argument("-o", "--out")
    common(p)
    p.set_defaults(func=cmd_gpx)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
