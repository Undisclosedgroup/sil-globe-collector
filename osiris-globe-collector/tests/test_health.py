"""Unit tests for health.assess_health.

Pure-function so all cases are deterministic (clock injected via `now`).
Mirrors test_predict.py conventions: top-of-file sys.path injection, plain
pytest functions, fixture builders inline.
"""
import pathlib, sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from health import assess_health, BASELINES


NOW = datetime(2026, 5, 23, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.isoformat()


def _entry(*, age_s=5, count=99999, error=None):
    """One manifest entry: `count` items, last updated `age_s` seconds ago."""
    e = {"updatedAt": _iso(NOW - timedelta(seconds=age_s)), "count": count}
    if error:
        e["error"] = error
    return e


def _all_healthy_manifest():
    """Every layer in BASELINES, fresh + comfortably above its floor."""
    layers = {}
    for lid, b in BASELINES.items():
        # +1000 guarantees we clear `>= expected_min_count` even for layers
        # whose floor is 0 or whose floor matches the boost.
        layers[lid] = _entry(count=b["expected_min_count"] + 1000)
    return {"layers": layers}


def test_all_healthy_manifest_is_healthy():
    out = assess_health(_all_healthy_manifest(), now=NOW)
    assert out["overall"] == "healthy"
    statuses = {l["status"] for l in out["layers"]}
    assert statuses == {"healthy"}, f"unexpected statuses: {statuses}"
    assert out["summary"].startswith(f"{len(BASELINES)} healthy")


def test_one_stale_layer_makes_overall_degraded():
    m = _all_healthy_manifest()
    # flights' max_age is 60s; backdate it past the threshold.
    m["layers"]["flights"] = _entry(age_s=600, count=20000)
    out = assess_health(m, now=NOW)
    assert out["overall"] == "degraded"
    flights = next(l for l in out["layers"] if l["id"] == "flights")
    assert flights["status"] == "stale"
    # All other layers must remain healthy — staleness on one feed shouldn't
    # contaminate the classifier for any sibling.
    others = [l for l in out["layers"] if l["id"] != "flights"]
    assert all(l["status"] == "healthy" for l in others)


def test_one_errored_layer_makes_overall_down():
    m = _all_healthy_manifest()
    m["layers"]["satellites"] = _entry(count=9000, error="HTTPError: 503 Service Unavailable")
    out = assess_health(m, now=NOW)
    assert out["overall"] == "down"
    sats = next(l for l in out["layers"] if l["id"] == "satellites")
    assert sats["status"] == "errored"
    assert sats["error"] == "HTTPError: 503 Service Unavailable"


def test_missing_layer_classified_silent():
    m = _all_healthy_manifest()
    del m["layers"]["military_air"]
    out = assess_health(m, now=NOW)
    assert out["overall"] == "degraded"
    ma = next(l for l in out["layers"] if l["id"] == "military_air")
    assert ma["status"] == "silent"
    assert ma["count"] == 0
    assert ma["lastUpdate"] is None
    assert ma["ageSeconds"] is None


def test_empty_count_below_floor_is_degraded():
    m = _all_healthy_manifest()
    # 100 < expected_min 5000 for flights; fresh timestamp + no error => empty.
    m["layers"]["flights"] = _entry(count=100)
    out = assess_health(m, now=NOW)
    assert out["overall"] == "degraded"
    flights = next(l for l in out["layers"] if l["id"] == "flights")
    assert flights["status"] == "empty"
    assert flights["count"] == 100
    assert flights["expectedMinCount"] == 5000


def test_static_layer_ignores_staleness():
    # chokepoints is "static" (manual reference data). Even an ancient
    # updatedAt must NOT mark it stale as long as the count is at the floor.
    m = _all_healthy_manifest()
    m["layers"]["chokepoints"] = _entry(age_s=86400 * 30, count=24)
    out = assess_health(m, now=NOW)
    cp = next(l for l in out["layers"] if l["id"] == "chokepoints")
    assert cp["status"] == "healthy"


def test_errored_beats_stale_in_classification():
    # If a layer is BOTH stale and errored, the error is the more actionable
    # signal — report "errored", which also rolls up to "down" overall.
    m = _all_healthy_manifest()
    m["layers"]["flights"] = _entry(age_s=600, count=0, error="ConnectError: tunnel timeout")
    out = assess_health(m, now=NOW)
    assert out["overall"] == "down"
    flights = next(l for l in out["layers"] if l["id"] == "flights")
    assert flights["status"] == "errored"
