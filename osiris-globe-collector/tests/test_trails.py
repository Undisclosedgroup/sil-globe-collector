"""Unit tests for trails.record_trails.

Follows project conventions: sys.path injection, inline fixture builders,
explicit `now` for determinism, no network, no clock."""
import pathlib, sys, json
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from trails import (   # noqa: E402
    record_trails,
    DEFAULT_MAX_POINTS,
    DEFAULT_WINDOW_HOURS,
)


NOW = datetime(2026, 5, 23, 12, 0, 0, tzinfo=timezone.utc)


def _layers(*, flights=(), military_air=(), boats=(), military_naval=()):
    """Build the {layer: payload} input the way collector passes it."""
    return {
        "flights":        {"items": list(flights)},
        "military_air":   {"items": list(military_air)},
        "boats":          {"items": list(boats)},
        "military_naval": {"items": list(military_naval)},
    }


def _plane(hex_, lat, lng, *, callsign="UAL12", country_code="US", altitude=35000):
    return {
        "id": hex_, "lat": lat, "lng": lng, "label": callsign,
        "callsign": callsign, "altitude": altitude, "speed": 450, "track": 90,
        "country_code": country_code,
    }


def _boat(mmsi, lat, lng, *, name="MV TEST", country_code="PA", speed=12.0,
          heading=45.0):
    return {
        "id": mmsi, "lat": lat, "lng": lng, "label": name, "name": name,
        "speed": speed, "heading": heading, "country_code": country_code,
    }


# ---------------------------------------------------------------------------
# 1. Cold start with no observations -> empty payload + empty state.
# ---------------------------------------------------------------------------
def test_cold_start_empty_payload():
    payload, state = record_trails(_layers(), None, now=NOW)
    assert payload["count"] == 0
    assert payload["entities"] == {}
    assert payload["windowHours"] == DEFAULT_WINDOW_HOURS
    assert state["tracks"] == {} or len(state["tracks"]) == 0


# ---------------------------------------------------------------------------
# 2. Single observation -> entity registered with 1 sample.
# ---------------------------------------------------------------------------
def test_single_observation_registers_entity():
    layers = _layers(
        flights=[_plane("a1b2c3", 40.0, -74.0)],
        boats=[_boat("441234567", 26.5, 56.2)],
    )
    payload, state = record_trails(layers, None, now=NOW)
    assert payload["count"] == 2
    assert "flights:a1b2c3" in payload["entities"]
    assert "boats:441234567" in payload["entities"]

    plane = payload["entities"]["flights:a1b2c3"]
    assert plane["label"] == "UAL12"
    assert plane["source"] == "flights"
    assert plane["kind"] == "flight"
    assert plane["flag"] == "US"
    assert len(plane["samples"]) == 1
    # ts_ms, lat*1e4, lng*1e4, alt
    s = plane["samples"][0]
    assert s[0] == int(NOW.timestamp() * 1000)
    assert s[1] == 400000
    assert s[2] == -740000
    assert s[3] == 35000

    boat = payload["entities"]["boats:441234567"]
    assert boat["kind"] == "vessel"
    # vessel sample carries speed*10 + heading*10
    bs = boat["samples"][0]
    assert bs[3] == 120 and bs[4] == 450


# ---------------------------------------------------------------------------
# 3. Multiple cycles -> samples accumulate in order.
# ---------------------------------------------------------------------------
def test_multiple_cycles_accumulate_in_order():
    state = None
    points = [(40.0, -74.0), (40.1, -74.1), (40.2, -74.2), (40.3, -74.3)]
    for i, (lat, lng) in enumerate(points):
        layers = _layers(flights=[_plane("a1b2c3", lat, lng)])
        payload, state = record_trails(
            layers, state, now=NOW + timedelta(seconds=90 * i))
    samples = payload["entities"]["flights:a1b2c3"]["samples"]
    assert len(samples) == len(points)
    # Strictly increasing ts.
    assert all(samples[i][0] < samples[i + 1][0]
               for i in range(len(samples) - 1))
    # First sample is the oldest, last is the newest.
    assert samples[0][1] == 400000
    assert samples[-1][1] == 403000


# ---------------------------------------------------------------------------
# 4. Cap exceeded -> oldest samples drop.
# ---------------------------------------------------------------------------
def test_per_entity_cap_drops_oldest():
    state = None
    cap = 5
    # Push 8 cycles, expect only the last 5 to survive.
    for i in range(8):
        layers = _layers(flights=[_plane("a1b2c3", 40.0 + i * 0.01, -74.0)])
        payload, state = record_trails(
            layers, state, now=NOW + timedelta(seconds=90 * i),
            max_points_per_entity=cap)
    samples = payload["entities"]["flights:a1b2c3"]["samples"]
    assert len(samples) == cap
    # Oldest surviving sample should be cycle 3 (i=3 -> lat 40.03).
    assert samples[0][1] == int(round((40.0 + 3 * 0.01) * 1e4))
    assert samples[-1][1] == int(round((40.0 + 7 * 0.01) * 1e4))


# ---------------------------------------------------------------------------
# 5. Stale entity (>window_hours, no observations) -> drops out.
# ---------------------------------------------------------------------------
def test_stale_entity_drops_after_window():
    # Cycle 1: observe both planes.
    layers1 = _layers(flights=[
        _plane("a1b2c3", 40.0, -74.0),
        _plane("d4e5f6", 50.0, -1.0),
    ])
    payload1, state = record_trails(layers1, None, now=NOW)
    assert payload1["count"] == 2

    # Cycle 2: 7h later, only ONE plane still observed. Other should drop.
    layers2 = _layers(flights=[_plane("d4e5f6", 50.1, -1.1)])
    payload2, state = record_trails(layers2, state,
                                    now=NOW + timedelta(hours=7))
    assert payload2["count"] == 1
    assert "flights:a1b2c3" not in payload2["entities"]
    assert "flights:d4e5f6" in payload2["entities"]


# ---------------------------------------------------------------------------
# 6. > max_entities active -> oldest-touched evicted.
# ---------------------------------------------------------------------------
def test_max_entities_evicts_oldest_touched():
    # Seed 4 entities at staggered times. Cap to 3 -> the oldest-touched drops.
    state = None
    for i in range(4):
        layers = _layers(flights=[_plane(f"hex{i}", 40.0 + i, -74.0)])
        payload, state = record_trails(
            layers, state, now=NOW + timedelta(minutes=i),
            max_entities=3)
    assert payload["count"] == 3
    # hex0 was inserted first and never re-touched -> evicted.
    assert "flights:hex0" not in payload["entities"]
    assert "flights:hex1" in payload["entities"]
    assert "flights:hex2" in payload["entities"]
    assert "flights:hex3" in payload["entities"]


# ---------------------------------------------------------------------------
# 7. Blob budget enforcement (sanity) — over-budget input gets trimmed.
# ---------------------------------------------------------------------------
def test_blob_budget_trims_when_oversized():
    # Build 50 entities × 100 samples then enforce a tiny 5KB budget.
    state = None
    for cycle in range(100):
        boats = [
            _boat(f"mmsi{i}", 0.0 + i * 0.01, 0.0 + cycle * 0.01)
            for i in range(50)
        ]
        layers = _layers(boats=boats)
        _, state = record_trails(
            layers, state, now=NOW + timedelta(seconds=90 * cycle),
            max_points_per_entity=200)
    # Final pass with a tiny budget.
    boats = [_boat(f"mmsi{i}", 0.0 + i * 0.01, 0.0) for i in range(50)]
    payload, _ = record_trails(
        _layers(boats=boats), state, now=NOW + timedelta(seconds=90 * 101),
        max_points_per_entity=200, blob_budget_bytes=5_000)
    encoded = json.dumps(payload, separators=(",", ":"))
    assert len(encoded) <= 5_000 or all(
        len(e["samples"]) == 1 for e in payload["entities"].values())


# ---------------------------------------------------------------------------
# 8. Round-tripping state via JSON works (collector seeds from API blob).
# ---------------------------------------------------------------------------
def test_state_survives_json_roundtrip():
    layers = _layers(flights=[_plane("a1b2c3", 40.0, -74.0)])
    payload1, state = record_trails(layers, None, now=NOW)

    # Simulate collector restart: state dict survives in memory, but tests
    # also exercise that the deque/OrderedDict coercion works on a fresh dict.
    roundtripped = {
        "tracks": {
            k: {"meta": v["meta"], "last_ts": v["last_ts"],
                "samples": list(v["samples"])}
            for k, v in state["tracks"].items()
        }
    }
    layers2 = _layers(flights=[_plane("a1b2c3", 40.1, -74.1)])
    payload2, _ = record_trails(layers2, roundtripped,
                                now=NOW + timedelta(seconds=90))
    assert payload2["entities"]["flights:a1b2c3"]["samples"][0][1] == 400000
    assert payload2["entities"]["flights:a1b2c3"]["samples"][-1][1] == 401000


# ---------------------------------------------------------------------------
# 9. Defaults pulled in from module are honored.
# ---------------------------------------------------------------------------
def test_default_caps_constant():
    # Smoke test: the constants used by the collector exist and are sane.
    assert DEFAULT_MAX_POINTS == 240
    assert DEFAULT_WINDOW_HOURS == 6
