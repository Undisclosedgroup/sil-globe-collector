"""Unit tests for anomalies.detect_anomalies.

Follows the project test conventions (sys.path injection at top, plain pytest
functions, inline fixture builders, explicit `now` for determinism). Pure / no
network."""
import pathlib, sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from anomalies import (    # noqa: E402
    detect_anomalies,
    DROP_MISS_CYCLES, TRACKED_TTL_S,
)


NOW = datetime(2026, 5, 23, 12, 0, 0, tzinfo=timezone.utc)


def _layers(*, boats=(), flights=(), military_air=(), geo_zones=None):
    """Build the {layer: payload} input the way collector passes it."""
    out = {
        "boats":        {"layer": "boats",        "count": len(boats),        "items": list(boats)},
        "flights":      {"layer": "flights",      "count": len(flights),      "items": list(flights)},
        "military_air": {"layer": "military_air", "count": len(military_air), "items": list(military_air)},
    }
    if geo_zones is not None:
        out["geo_zones"] = {"layer": "geo_zones", "count": len(geo_zones),
                            "items": list(geo_zones)}
    return out


def _boat(mmsi, lat, lng, *, speed=10.0, heading=45.0, name="MV TEST",
          country_code="PA"):
    return {
        "id": mmsi, "lat": lat, "lng": lng, "label": name, "name": name,
        "speed": speed, "heading": heading,
        "flag": "Panama", "country_code": country_code,
    }


def _plane(hex_, lat, lng, *, callsign="UAL123", squawk=None, speed=420,
           track=270, country_code="US", source="flights"):
    return {
        "id": hex_, "lat": lat, "lng": lng, "label": callsign,
        "callsign": callsign, "squawk": squawk,
        "speed": speed, "track": track,
        "flag": "United States", "country_code": country_code,
    }


# ---------------------------------------------------------------------------
# 1. Cold start: no prev state -> no events, but state populated.
# ---------------------------------------------------------------------------
def test_cold_start_no_events_state_populated():
    layers = _layers(
        boats=[_boat("441234567", 26.5, 56.2)],
        flights=[_plane("a1b2c3", 40.0, -74.0)],
    )
    payload, state = detect_anomalies(layers, None, None, now=NOW)
    assert payload["count"] == 0
    assert payload["new"] == 0
    assert payload["events"] == []
    assert set(state["tracked"].keys()) == {
        "boats:441234567", "flights:a1b2c3",
    }
    for rec in state["tracked"].values():
        assert rec["miss_count"] == 0
        assert rec["dropped_fired"] is False
        assert rec["last_seen_ts"] == NOW.timestamp()


# ---------------------------------------------------------------------------
# 2. Vessel observed then disappears 3 cycles -> exactly one `dropped`,
#    suppressed thereafter.
# ---------------------------------------------------------------------------
def test_dropped_fires_once_after_threshold_then_suppressed():
    mmsi = "441234567"
    state = None
    anomalies = None

    # Cycle 0: observe.
    payload, state = detect_anomalies(
        _layers(boats=[_boat(mmsi, 26.5, 56.2)]),
        state, anomalies, now=NOW)
    anomalies = payload
    assert payload["new"] == 0

    # Cycles 1..3: no observation. miss_count 1, 2, 3 -> on miss 3 fire once.
    fired = []
    for i in range(1, DROP_MISS_CYCLES + 1):
        payload, state = detect_anomalies(
            _layers(), state, anomalies, now=NOW + timedelta(seconds=30 * i))
        anomalies = payload
        fired.append(payload["new"])

    # New events on miss 1 and 2 are zero; on miss 3 exactly one (dropped).
    assert fired == [0, 0, 1]
    last_evt = payload["events"][0]
    assert last_evt["kind"] == "dropped"
    assert last_evt["severity"] == "warning"
    assert last_evt["entity_key"] == f"boats:{mmsi}"
    assert "Stopped broadcasting" in last_evt["detail"]

    # Cycle 4+: still absent -> no additional dropped fires.
    payload, state = detect_anomalies(
        _layers(), state, anomalies,
        now=NOW + timedelta(seconds=30 * (DROP_MISS_CYCLES + 1)))
    assert payload["new"] == 0


# ---------------------------------------------------------------------------
# 3. mmsi swap at same location -> mmsi_swap event with swap_from.
# ---------------------------------------------------------------------------
def test_mmsi_swap_at_same_location():
    old, new = "441111111", "411222222"
    # Cycle 0: only OLD id present.
    _, state = detect_anomalies(
        _layers(boats=[_boat(old, 26.5, 56.2)]),
        None, None, now=NOW)
    # Cycle 1: OLD gone, NEW id at the same lat/lng.
    payload, state = detect_anomalies(
        _layers(boats=[_boat(new, 26.501, 56.199)]),
        state, None, now=NOW + timedelta(seconds=30))

    swap_events = [e for e in payload["events"] if e["kind"] == "mmsi_swap"]
    assert len(swap_events) == 1
    ev = swap_events[0]
    assert ev["entity_key"] == f"boats:{new}"
    assert ev["swap_from"] == old
    assert ev["severity"] == "warning"


# ---------------------------------------------------------------------------
# 4. Teleport (200nm in 1 cycle) -> teleport event.
# ---------------------------------------------------------------------------
def test_teleport_fires_when_speed_exceeds_bound():
    mmsi = "441333333"
    _, state = detect_anomalies(
        _layers(boats=[_boat(mmsi, 26.5, 56.2)]),
        None, None, now=NOW)
    # 30s later, the vessel is ~200nm north. 200nm/(30/3600)h = 24000 kt.
    payload, _ = detect_anomalies(
        _layers(boats=[_boat(mmsi, 26.5 + 200 / 60.0, 56.2)]),
        state, None, now=NOW + timedelta(seconds=30))
    tele = [e for e in payload["events"] if e["kind"] == "teleport"]
    assert len(tele) == 1
    assert tele[0]["entity_key"] == f"boats:{mmsi}"
    assert "Implied speed" in tele[0]["detail"]


# ---------------------------------------------------------------------------
# 5. Emergency squawk -> critical severity. Fires once per distinct squawk.
# ---------------------------------------------------------------------------
def test_emergency_squawk_critical_and_dedup():
    hexc = "abc123"
    # Cycle 0: normal squawk -> no event.
    _, state = detect_anomalies(
        _layers(flights=[_plane(hexc, 40.0, -74.0, squawk="1234")]),
        None, None, now=NOW)
    # Cycle 1: squawk 7500 -> one critical event.
    payload, state = detect_anomalies(
        _layers(flights=[_plane(hexc, 40.01, -74.01, squawk="7500")]),
        state, None, now=NOW + timedelta(seconds=15))
    sq = [e for e in payload["events"] if e["kind"] == "emergency_squawk"]
    assert len(sq) == 1
    assert sq[0]["severity"] == "critical"
    assert "7500" in sq[0]["detail"]
    # Cycle 2: still 7500 -> NO repeat fire.
    payload, _ = detect_anomalies(
        _layers(flights=[_plane(hexc, 40.02, -74.02, squawk="7500")]),
        state, payload, now=NOW + timedelta(seconds=30))
    new_sq = [e for e in payload["events"]
              if e["kind"] == "emergency_squawk"
              and e["t"] == (NOW + timedelta(seconds=30)).isoformat()]
    assert new_sq == []


# ---------------------------------------------------------------------------
# 6. Heading flip (12kt -> 0kt at same lat/lng) -> heading_flip event.
# ---------------------------------------------------------------------------
def test_heading_flip_emits_when_vessel_goes_static():
    mmsi = "441444444"
    _, state = detect_anomalies(
        _layers(boats=[_boat(mmsi, 26.5, 56.2, speed=12.0)]),
        None, None, now=NOW)
    payload, _ = detect_anomalies(
        _layers(boats=[_boat(mmsi, 26.501, 56.201, speed=0.0)]),
        state, None, now=NOW + timedelta(seconds=30))
    flip = [e for e in payload["events"] if e["kind"] == "heading_flip"]
    assert len(flip) == 1
    assert flip[0]["entity_key"] == f"boats:{mmsi}"
    assert flip[0]["severity"] == "warning"


# ---------------------------------------------------------------------------
# 7. Drop-then-return: after dropped fires + entity returns, the next dropout
#    fires fresh.
# ---------------------------------------------------------------------------
def test_drop_then_return_then_drop_fires_again():
    mmsi = "441555555"
    state = None
    anomalies = None
    # Observe once
    payload, state = detect_anomalies(
        _layers(boats=[_boat(mmsi, 26.5, 56.2)]),
        state, anomalies, now=NOW)
    anomalies = payload
    # Disappear for DROP_MISS_CYCLES cycles -> dropped fires
    for i in range(1, DROP_MISS_CYCLES + 1):
        payload, state = detect_anomalies(
            _layers(), state, anomalies, now=NOW + timedelta(seconds=30 * i))
        anomalies = payload
    assert any(e["kind"] == "dropped" for e in payload["events"])
    # Return
    payload, state = detect_anomalies(
        _layers(boats=[_boat(mmsi, 26.5, 56.2)]),
        state, anomalies, now=NOW + timedelta(seconds=30 * 4))
    anomalies = payload
    # After return, dropped_fired must have reset.
    assert state["tracked"][f"boats:{mmsi}"]["dropped_fired"] is False
    assert state["tracked"][f"boats:{mmsi}"]["miss_count"] == 0
    # Disappear again -> a FRESH dropped fires after threshold.
    pre_count = sum(1 for e in payload["events"] if e["kind"] == "dropped")
    for i in range(1, DROP_MISS_CYCLES + 1):
        payload, state = detect_anomalies(
            _layers(), state, anomalies,
            now=NOW + timedelta(seconds=30 * (4 + i)))
        anomalies = payload
    post_count = sum(1 for e in payload["events"] if e["kind"] == "dropped")
    assert post_count == pre_count + 1


# ---------------------------------------------------------------------------
# 8. TTL eviction: a long-dropped entity is forgotten and never fires dropped
#    twice after threshold from a stale prev.
# ---------------------------------------------------------------------------
def test_ttl_eviction_drops_stale_entity():
    mmsi = "441777777"
    _, state = detect_anomalies(
        _layers(boats=[_boat(mmsi, 0.0, 0.0)]),
        None, None, now=NOW)
    # Jump past TTL with no observation.
    payload, state = detect_anomalies(
        _layers(), state, None,
        now=NOW + timedelta(seconds=TRACKED_TTL_S + 60))
    # Entity is evicted (not present in tracked any more).
    assert f"boats:{mmsi}" not in state["tracked"]


# ---------------------------------------------------------------------------
# 9. geo_zones context enrichment: dropped event inside an EEZ bbox flags
#    `in_eez_of`.
# ---------------------------------------------------------------------------
def test_geo_zone_context_marks_eez_on_dropped():
    mmsi = "441888888"
    eez = {
        "type": "Feature",
        "properties": {"kind": "eez", "name": "Iran EEZ", "iso2": "IR"},
        "geometry": {"type": "Polygon",
                     "coordinates": [[[55.0, 25.0], [58.0, 25.0],
                                       [58.0, 28.0], [55.0, 28.0],
                                       [55.0, 25.0]]]},
    }
    state = None
    anomalies = None
    payload, state = detect_anomalies(
        _layers(boats=[_boat(mmsi, 26.5, 56.2)], geo_zones=[eez]),
        state, anomalies, now=NOW)
    anomalies = payload
    for i in range(1, DROP_MISS_CYCLES + 1):
        payload, state = detect_anomalies(
            _layers(geo_zones=[eez]), state, anomalies,
            now=NOW + timedelta(seconds=30 * i))
        anomalies = payload
    dropped = [e for e in payload["events"] if e["kind"] == "dropped"]
    assert len(dropped) == 1
    assert dropped[0]["context"]["in_eez_of"] == "IR"
    assert "IR EEZ" in dropped[0]["detail"]
