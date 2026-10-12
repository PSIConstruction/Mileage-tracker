import json

import pytest

from mileage.cli import main
from mileage.filtering import filter_points
from mileage.geo import haversine_m, miles
from mileage.pipeline import process_trip
from mileage.roads import SnappedPoint, parse_snap_response, snap_path
from mileage.trip import FORMAT, GpsPoint, Trip

from synthetic import FakeRoadsClient, make_trip


def test_haversine_one_degree_latitude():
    assert haversine_m(40.0, -74.0, 41.0, -74.0) == pytest.approx(111_195, rel=1e-3)


def test_raw_gps_overcounts_and_matching_fixes_it():
    trip, true_m = make_trip(seed=3)
    r = process_trip(trip, FakeRoadsClient())
    # Noise + parked jitter + spikes inflate the naive sum well past the truth.
    assert r.raw_m > true_m * 1.15
    # Road matching recovers the real distance to within 1%.
    assert r.matched_m == pytest.approx(true_m, rel=0.01)
    assert r.gap_m == 0
    assert not r.warnings


@pytest.mark.parametrize("seed", range(10))
def test_matched_distance_stable_across_noise(seed):
    trip, true_m = make_trip(seed=seed, noise_m=8.0)
    r = process_trip(trip, FakeRoadsClient())
    assert abs(miles(r.matched_m) - miles(true_m)) < 0.05


def test_filter_rejects_spikes_bad_accuracy_and_parked_jitter():
    trip, _ = make_trip(seed=5, spikes=3)
    fr = filter_points(trip.points)
    # The three injected spikes, and only them, are rejected mid-drive; the
    # good fixes around them survive even when a spike is just under the
    # speed limit.
    moving = {i for i, p in enumerate(trip.points) if p.speed}
    assert {74, 81, 88} == {i for i in fr.rejected if i in moving and i != 60}
    assert fr.rejected[60].startswith("accuracy")
    assert sum(r == "stationary jitter" for r in fr.rejected.values()) > 60


def test_parked_phone_produces_no_distance():
    pts = [GpsPoint(t=i * 5000, lat=40.0 + (i % 3) * 3e-5, lng=-74.0, acc=8.0, speed=0.0)
           for i in range(200)]
    fr = filter_points(pts)
    assert fr.kept == [0]


def test_raw_trip_is_not_modified():
    trip, _ = make_trip(seed=2)
    before = list(trip.points)
    process_trip(trip, FakeRoadsClient())
    assert trip.points == before


def test_batches_respect_limit_and_overlap():
    trip, true_m = make_trip(seed=4, speed_mps=5.0)  # slow -> many points
    client = FakeRoadsClient()
    r = process_trip(trip, client)
    assert len(client.calls) > 2
    assert max(client.calls) <= 100
    assert r.roads_requests == len(client.calls)
    assert r.matched_m == pytest.approx(true_m, rel=0.01)
    # Original indices must come out in order with nothing repeated at seams.
    path = [(p.lat, p.lng) for p in trip.points]
    run = snap_path(path, FakeRoadsClient())
    orig = [sp.original_index for sp in run.points if sp.original_index is not None]
    assert orig == list(range(len(path)))


def test_large_gap_splits_and_is_flagged():
    trip, true_m = make_trip(seed=6, parked_s=0, spikes=0)
    pts = trip.points
    mid = len(pts) // 2
    # Lose 2 minutes of signal mid-drive (~1.8 km at 15 m/s).
    trip.points = pts[:mid] + pts[mid + 24:]
    r = process_trip(trip, FakeRoadsClient())
    assert len(r.segments) == 2
    assert r.gap_m > 1000
    assert any("gap" in w for w in r.warnings)


def test_offline_mode_makes_no_requests():
    trip, _ = make_trip(seed=7)
    r = process_trip(trip, None)
    assert r.matched_m is None and r.roads_requests == 0
    assert r.filtered_m < r.raw_m


def test_parse_snap_response():
    body = {"snappedPoints": [
        {"location": {"latitude": 1.5, "longitude": 2.5}, "originalIndex": 0, "placeId": "a"},
        {"location": {"latitude": 1.6, "longitude": 2.6}, "placeId": "a"},
    ]}
    assert parse_snap_response(body) == [SnappedPoint(1.5, 2.5, 0, "a"),
                                         SnappedPoint(1.6, 2.6, None, "a")]


def _write_trip(path, trip):
    path.write_text(json.dumps({
        "format": FORMAT, "tripId": trip.trip_id,
        "odometerStart": trip.odometer_start, "odometerEnd": trip.odometer_end,
        "points": [{"t": p.t, "lat": p.lat, "lng": p.lng, "acc": p.acc, "speed": p.speed}
                   for p in trip.points],
    }))


def test_cli_validate_offline(tmp_path, capsys):
    for seed in (1, 2):
        trip, _ = make_trip(seed=seed)
        _write_trip(tmp_path / f"{trip.trip_id}.json", trip)
    csv_path = tmp_path / "report.csv"
    assert main(["validate", str(tmp_path), "--offline", "--csv", str(csv_path)]) == 0
    out = capsys.readouterr().out
    assert "synthetic-1" in out and "synthetic-2" in out
    assert csv_path.read_text().count("\n") == 3
    # Offline runs must not write processed results.
    assert not (tmp_path / "processed").exists()


def test_cli_gpx_offline(tmp_path):
    trip, _ = make_trip(seed=1)
    p = tmp_path / "t.json"
    _write_trip(p, trip)
    assert main(["gpx", str(p), "--offline"]) == 0
    assert "<trk>" in (tmp_path / "t.gpx").read_text()


def test_trip_rejects_unknown_format():
    with pytest.raises(ValueError):
        Trip.from_dict({"format": "nope", "tripId": "x", "points": []})


def test_google_client_request_and_budget(monkeypatch):
    import io
    import urllib.parse
    import urllib.request

    from mileage.roads import GoogleRoadsClient, RequestBudgetExceeded

    seen = []

    def fake_urlopen(req, timeout):
        seen.append(req.full_url)
        body = {"snappedPoints": [
            {"location": {"latitude": 40.0, "longitude": -74.0}, "originalIndex": 0}]}
        return io.BytesIO(json.dumps(body).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = GoogleRoadsClient("KEY", max_requests=1)
    pts, warning = client.snap([(40.0, -74.0), (40.001, -74.0)])
    assert pts == [SnappedPoint(40.0, -74.0, 0, None)] and warning is None
    q = urllib.parse.parse_qs(urllib.parse.urlparse(seen[0]).query)
    assert q["path"] == ["40.000000,-74.000000|40.001000,-74.000000"]
    assert q["interpolate"] == ["true"] and q["key"] == ["KEY"]
    with pytest.raises(RequestBudgetExceeded):
        client.snap([(40.0, -74.0)])
