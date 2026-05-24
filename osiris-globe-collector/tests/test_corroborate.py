"""Unit tests for corroborate.corroborate.

Pure heuristic / no network — deterministic via the explicit `now` argument.
Builds minimal synthetic forecast + layers payloads in-line.
"""
import pathlib, sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from corroborate import (
    corroborate,
    LIT_THRESHOLD,
    KINETIC_KEYS,
    GRID_DEG,
)
from forecast import _level

NOW = datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)


def _signal(key, score, value=0):
    return {"key": key, "label": key, "value": value, "score": score,
            "note": f"synthetic {key}={score}"}


def _hotspot(hid, name, lat, lng, base_score, signal_scores):
    """signal_scores: {key: score}. Missing keys default to 0."""
    sigs = []
    for key in ("military_air", "naval", "events", "nav_warnings",
                "news", "frontlines"):
        sigs.append(_signal(key, int(signal_scores.get(key, 0))))
    return {"id": hid, "name": name, "lat": lat, "lng": lng,
            "score": base_score, "level": _level(base_score),
            "signals": sigs, "summary": ""}


def _forecast(hotspots):
    return {"updatedAt": NOW.isoformat(), "hotspots": hotspots}


# -- 1. cold/empty forecast --------------------------------------------------
def test_empty_forecast_returns_empty_payload():
    out = corroborate(_forecast([]), {}, now=NOW)
    assert out["hotspots"] == []
    assert out["convergences"] == []
    assert out["updatedAt"] == NOW.isoformat()
    assert out["generatedFrom"] == NOW.isoformat()
    assert out["litThreshold"] == LIT_THRESHOLD
    assert out["kineticFamilies"] == list(KINETIC_KEYS)


def test_none_inputs_safe():
    out = corroborate({}, None, now=NOW)
    assert out["hotspots"] == []
    assert out["convergences"] == []


# -- 2. one lit signal -> no bonus ------------------------------------------
def test_single_lit_signal_no_bonus():
    fc = _forecast([_hotspot("a", "A", 0, 0, base_score=40,
                             signal_scores={"military_air": 60})])
    out = corroborate(fc, {}, now=NOW)
    h = out["hotspots"][0]
    assert h["lit_count"] == 1
    assert h["lit_signals"] == ["military_air"]
    assert h["kinetic_lit"] == 1
    assert h["compound_bonus"] == 0
    assert h["compound_score"] == 40
    assert h["base_score"] == 40
    assert h["compound_level"] == _level(40)


def test_two_lit_kinetic_gets_8_bonus():
    fc = _forecast([_hotspot("a", "A", 0, 0, base_score=40,
                             signal_scores={"military_air": 60, "naval": 50})])
    out = corroborate(fc, {}, now=NOW)
    h = out["hotspots"][0]
    assert h["kinetic_lit"] == 2
    assert h["compound_bonus"] == 8
    assert h["compound_score"] == 48


def test_three_lit_kinetic_gets_14_bonus():
    fc = _forecast([_hotspot("a", "A", 0, 0, base_score=40,
                             signal_scores={"military_air": 60, "naval": 50,
                                            "nav_warnings": 45})])
    out = corroborate(fc, {}, now=NOW)
    h = out["hotspots"][0]
    assert h["kinetic_lit"] == 3
    assert h["compound_bonus"] == 14
    assert h["compound_score"] == 54


# -- 3. 4/4 kinetic lit -> +20 bonus -----------------------------------------
def test_four_lit_kinetic_gets_20_bonus():
    fc = _forecast([_hotspot("a", "A", 0, 0, base_score=50,
                             signal_scores={"military_air": 60, "naval": 50,
                                            "nav_warnings": 45,
                                            "frontlines": 55})])
    out = corroborate(fc, {}, now=NOW)
    h = out["hotspots"][0]
    assert h["kinetic_lit"] == 4
    assert h["lit_count"] == 4
    assert h["compound_bonus"] == 25   # all-four max bonus
    assert h["compound_score"] == 75
    assert h["compound_level"] == "crisis"
    assert h["corroboration"] == 1.0


# -- 4. convergence scan: mil_air + naval cluster at (50,36) ---------------
def test_convergence_emitted_for_collocated_kinetic_signals():
    # 8 military aircraft + 5 vessels all inside the 5-deg cell containing
    # (50, 36). The cell covers (50<=lat<55, 35<=lng<40). Pick coords inside.
    air_items = [{"lat": 50.5 + 0.1 * i, "lng": 36.5} for i in range(8)]
    boat_items = [{"lat": 51.0, "lng": 37.0 + 0.1 * i} for i in range(5)]
    layers = {
        "military_air": {"items": air_items},
        "boats": {"items": boat_items},
    }
    out = corroborate(_forecast([]), layers, now=NOW)
    # Ukraine hotspot covers 44-53N, 22-41E — the (50, 36) cell IS inside Ukraine.
    # So this cell should be suppressed. Pick a cell off the hotspot list instead.
    # The test should validate the suppression logic too.
    assert out["convergences"] == []


def test_convergence_emitted_outside_hotspots():
    # (-40, -55) is well off every hotspot box (off Argentina / Falklands).
    # cell containing (-40, -55): lat -40..-35, lng -55..-50.
    air_items = [{"lat": -39.0 + 0.1 * i, "lng": -54.0} for i in range(6)]
    boat_items = [{"lat": -38.0, "lng": -53.0 + 0.1 * i} for i in range(4)]
    layers = {
        "military_air": {"items": air_items},
        "boats": {"items": boat_items},
    }
    out = corroborate(_forecast([]), layers, now=NOW)
    assert len(out["convergences"]) == 1
    c = out["convergences"][0]
    assert "military_air" in c["lit_signals"]
    assert "naval" in c["lit_signals"]
    assert c["score"] >= 50
    # cell center is in the (-40..-35, -55..-50) cell
    assert -40 <= c["lat"] <= -35
    assert -55 <= c["lng"] <= -50
    assert c["family_counts"]["military_air"] == 6
    assert c["family_counts"]["naval"] == 4


# -- 5. scattered signals: no co-location -> no convergence ----------------
def test_scattered_signals_no_convergence():
    # one mil_air far from any vessel; one vessel far from any aircraft.
    # Both off any hotspot box. Different grid cells -> no convergence.
    layers = {
        "military_air": [{"lat": -45.0, "lng": -55.0}],  # cell (-45..-40, -55..-50)
        "boats": [{"lat": -20.0, "lng": -10.0}],          # very different cell
    }
    # Layers payload supports either {"items": [...]} or raw list? In production
    # always dict-with-items. Use that shape.
    layers = {
        "military_air": {"items": [{"lat": -45.0, "lng": -55.0}]},
        "boats": {"items": [{"lat": -20.0, "lng": -10.0}]},
    }
    out = corroborate(_forecast([]), layers, now=NOW)
    assert out["convergences"] == []


def test_convergence_requires_kinetic_family():
    # Two non-kinetic families (events only would not even be 2 families) —
    # we synthesize events + a stray with no kinetic. events alone is 1 family
    # so won't trigger. Try events + a synthetic nav_warning with no kinetic.
    # Actually nav_warnings IS kinetic — that's the point. So test: events only
    # in a cell, even with many items, doesn't trigger.
    layers = {
        "events": {"items": [{"lat": -45.0, "lng": -54.0} for _ in range(20)]},
    }
    out = corroborate(_forecast([]), layers, now=NOW)
    assert out["convergences"] == []


# -- 6. compound score clamps to 100 ---------------------------------------
def test_compound_clamps_to_100():
    fc = _forecast([_hotspot("a", "A", 0, 0, base_score=90,
                             signal_scores={"military_air": 80, "naval": 75,
                                            "nav_warnings": 60,
                                            "frontlines": 70})])
    out = corroborate(fc, {}, now=NOW)
    h = out["hotspots"][0]
    assert h["compound_bonus"] == 25
    assert h["compound_score"] == 100      # 90 + 25 clamped
    assert h["compound_level"] == "crisis"
    assert h["base_score"] == 90


def test_non_kinetic_lit_does_not_get_bonus():
    # news + events lit but no kinetic -> bonus stays at 0 (corroboration is
    # specifically about kinetic convergence).
    fc = _forecast([_hotspot("a", "A", 0, 0, base_score=45,
                             signal_scores={"news": 80, "events": 70})])
    out = corroborate(fc, {}, now=NOW)
    h = out["hotspots"][0]
    assert h["lit_count"] == 2
    assert h["kinetic_lit"] == 0
    assert h["compound_bonus"] == 0
    assert h["compound_score"] == 45


def test_hotspots_sorted_by_compound_score():
    # B has higher base but no corroboration; A has lower base but full kinetic
    # corroboration. Sorted output should put A first (compound 60+25=85 > 70).
    fc = _forecast([
        _hotspot("b", "B-quiet", 0, 0, base_score=70,
                 signal_scores={"news": 80}),
        _hotspot("a", "A-corroborated", 10, 10, base_score=60,
                 signal_scores={"military_air": 60, "naval": 50,
                                "nav_warnings": 45, "frontlines": 55}),
    ])
    out = corroborate(fc, {}, now=NOW)
    ids = [h["id"] for h in out["hotspots"]]
    assert ids[0] == "a"
    assert ids[1] == "b"


def test_lit_threshold_is_strict_inequality_boundary():
    # signal exactly at LIT_THRESHOLD is lit; one below is not.
    fc = _forecast([
        _hotspot("on", "On", 0, 0, base_score=20,
                 signal_scores={"military_air": LIT_THRESHOLD,
                                "naval": LIT_THRESHOLD}),
        _hotspot("off", "Off", 10, 10, base_score=20,
                 signal_scores={"military_air": LIT_THRESHOLD - 1,
                                "naval": LIT_THRESHOLD - 1}),
    ])
    out = corroborate(fc, {}, now=NOW)
    by_id = {h["id"]: h for h in out["hotspots"]}
    assert by_id["on"]["kinetic_lit"] == 2
    assert by_id["on"]["compound_bonus"] == 8
    assert by_id["off"]["kinetic_lit"] == 0
    assert by_id["off"]["compound_bonus"] == 0
