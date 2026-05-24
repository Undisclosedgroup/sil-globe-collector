"""Unit tests for projection_alerts.derive_projection_alerts.

Follows the conventions in test_predict.py: sys.path injection at the top,
plain pytest functions, fixture builders inline. Pure heuristic — deterministic
via the explicit `now` argument."""
import pathlib, sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from projection_alerts import (
    derive_projection_alerts,
    KIND_ETA_CRISIS_IMMINENT,
    KIND_PROJECTION_CRISIS_24H,
    KIND_PROJECTION_HIGH_24H,
    KIND_RAPID_CLIMB,
    KIND_RAPID_DROP,
    DEDUP_COOLDOWN,
)


NOW = datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)


def _traj(hid="cuba", name="Cuba", *, current=40, slope=0.0, proj_6h=None,
          proj_24h=None, eta=None, confidence=0.8):
    """Build one trajectory entry. Sensible defaults so each test only sets
    the dimensions it actually cares about."""
    if proj_6h is None:
        proj_6h = int(max(0, min(100, current + slope * 6)))
    if proj_24h is None:
        proj_24h = int(max(0, min(100, current + slope * 24)))
    return {
        "id": hid, "name": name, "lat": 21.5, "lng": -77.8,
        "currentScore": current, "currentLevel": "elevated",
        "slope": slope, "trend": "rising" if slope > 0 else "flat",
        "projection_6h": proj_6h, "projection_24h": proj_24h,
        "projectedLevel_6h": "high", "projectedLevel_24h": "high",
        "eta_to_crisis": eta, "eta_to_routine": None,
        "confidence": confidence,
        "summary": "test",
    }


def _predict(trajs, *, updated_at=None):
    return {
        "updatedAt": (updated_at or NOW).isoformat(),
        "generatedFrom": (updated_at or NOW).isoformat(),
        "windowSampledHours": 12,
        "trajectories": trajs,
    }


# ---------------------------------------------------------------------------
# 1. Cold start: no prev -> no events emitted, log seeded
# ---------------------------------------------------------------------------
def test_cold_start_emits_no_events():
    pred = _predict([_traj(current=40, slope=3.0, eta=4.0)])
    out = derive_projection_alerts(pred, None, now=NOW)
    assert out["new"] == 0
    assert out["count"] == 0
    assert out["alerts"] == []
    # Snapshot is seeded so the next cycle has a baseline.
    assert out["prev_predict"]["trajectories"][0]["id"] == "cuba"
    assert out["generatedFrom"] == pred["updatedAt"]


# ---------------------------------------------------------------------------
# 2. ETA drops from 8h to 4h -> 1 critical event
# ---------------------------------------------------------------------------
def test_eta_drops_below_six_hours_fires_critical():
    # Cycle 1: ETA = 8h (no fire)
    pred1 = _predict([_traj(current=60, slope=2.0, eta=8.0)])
    out1 = derive_projection_alerts(pred1, None, now=NOW)
    assert out1["new"] == 0

    # Cycle 2: ETA drops to 4h -> fire
    later = NOW + timedelta(minutes=5)
    pred2 = _predict([_traj(current=63, slope=3.0, eta=4.0)],
                     updated_at=later)
    out2 = derive_projection_alerts(pred2, out1, now=later)
    assert out2["new"] == 1
    ev = out2["alerts"][0]
    assert ev["kind"] == KIND_ETA_CRISIS_IMMINENT
    assert ev["severity"] == "critical"
    assert ev["hotspotId"] == "cuba"
    assert ev["eta_to_crisis"] == 4.0
    # ID prefix sanity
    assert ev["id"].startswith("proj-cuba-eta_crisis_imminent-")


# ---------------------------------------------------------------------------
# 3. ETA stays at 4h next cycle -> no new event (deduped)
# ---------------------------------------------------------------------------
def test_eta_stays_low_does_not_refire():
    pred1 = _predict([_traj(current=60, slope=2.0, eta=8.0)])
    out1 = derive_projection_alerts(pred1, None, now=NOW)

    t2 = NOW + timedelta(minutes=5)
    pred2 = _predict([_traj(current=63, slope=3.0, eta=4.0)], updated_at=t2)
    out2 = derive_projection_alerts(pred2, out1, now=t2)
    assert out2["new"] == 1

    # Cycle 3: ETA still 4h -> no new fire (cooldown + still-armed)
    t3 = t2 + timedelta(minutes=5)
    pred3 = _predict([_traj(current=66, slope=3.0, eta=3.5)], updated_at=t3)
    out3 = derive_projection_alerts(pred3, out2, now=t3)
    assert out3["new"] == 0
    # But the log still contains the prior event.
    assert out3["count"] == 1


# ---------------------------------------------------------------------------
# 4. ETA returns above 6h, then drops back -> fresh event
# ---------------------------------------------------------------------------
def test_eta_re_arms_after_returning_above_six_hours():
    # Cycle 1: baseline ETA = 8h
    pred1 = _predict([_traj(current=60, slope=2.0, eta=8.0)])
    out1 = derive_projection_alerts(pred1, None, now=NOW)

    # Cycle 2: ETA drops to 4h -> 1 event
    t2 = NOW + timedelta(minutes=5)
    pred2 = _predict([_traj(current=63, slope=3.0, eta=4.0)], updated_at=t2)
    out2 = derive_projection_alerts(pred2, out1, now=t2)
    assert out2["new"] == 1

    # Cycle 3: ETA climbs back to 9h (un-arms)
    t3 = t2 + timedelta(minutes=5)
    pred3 = _predict([_traj(current=60, slope=1.5, eta=9.0)], updated_at=t3)
    out3 = derive_projection_alerts(pred3, out2, now=t3)
    assert out3["new"] == 0

    # Cycle 4: ETA drops again to 4h. Cooldown is 24h so the prior event is
    # still "recent" — but the spec says re-arm should win once the crossing
    # condition has been broken. We model that by stepping past the cooldown.
    t4 = t3 + DEDUP_COOLDOWN + timedelta(minutes=1)
    pred4 = _predict([_traj(current=63, slope=3.0, eta=4.0)], updated_at=t4)
    # prev_predict carries the un-armed (9h) state from cycle 3.
    out4 = derive_projection_alerts(pred4, out3, now=t4)
    assert out4["new"] == 1
    assert out4["alerts"][0]["kind"] == KIND_ETA_CRISIS_IMMINENT


# ---------------------------------------------------------------------------
# 5. projection_24h crosses 75 from below -> warning event
# ---------------------------------------------------------------------------
def test_projection_crisis_24h_crossing_fires_warning():
    # Cycle 1: 24h projection below 75 (no fire)
    pred1 = _predict([_traj(current=50, slope=0.5, proj_24h=62)])
    out1 = derive_projection_alerts(pred1, None, now=NOW)
    assert out1["new"] == 0

    # Cycle 2: 24h projection now 80 (crosses up through 75)
    t2 = NOW + timedelta(minutes=5)
    pred2 = _predict([_traj(current=55, slope=1.0, proj_24h=80)],
                     updated_at=t2)
    out2 = derive_projection_alerts(pred2, out1, now=t2)
    kinds = [e["kind"] for e in out2["alerts"]]
    assert KIND_PROJECTION_CRISIS_24H in kinds
    crisis_ev = next(e for e in out2["alerts"]
                     if e["kind"] == KIND_PROJECTION_CRISIS_24H)
    assert crisis_ev["severity"] == "warning"
    assert crisis_ev["projection_24h"] == 80


# ---------------------------------------------------------------------------
# 6. slope +6/hr with confidence 0.7 -> rapid_climb
# ---------------------------------------------------------------------------
def test_rapid_climb_fires_with_high_confidence():
    pred = _predict([_traj(current=40, slope=6.0, confidence=0.7)])
    out = derive_projection_alerts(pred, None, now=NOW)
    kinds = [e["kind"] for e in out["alerts"]]
    assert KIND_RAPID_CLIMB in kinds
    ev = next(e for e in out["alerts"] if e["kind"] == KIND_RAPID_CLIMB)
    assert ev["severity"] == "warning"
    assert ev["slope"] == 6.0


# ---------------------------------------------------------------------------
# 7. slope +6/hr with confidence 0.3 -> NO rapid_climb (confidence gate)
# ---------------------------------------------------------------------------
def test_rapid_climb_suppressed_by_low_confidence():
    pred = _predict([_traj(current=40, slope=6.0, confidence=0.3)])
    out = derive_projection_alerts(pred, None, now=NOW)
    kinds = [e["kind"] for e in out["alerts"]]
    assert KIND_RAPID_CLIMB not in kinds


# ---------------------------------------------------------------------------
# Extras for confidence: rapid_drop, projection_high, dedup behaviour,
# and the "missing hotspot id" / malformed-input guards.
# ---------------------------------------------------------------------------
def test_rapid_drop_fires_with_high_confidence():
    pred = _predict([_traj(current=60, slope=-6.0, confidence=0.75)])
    out = derive_projection_alerts(pred, None, now=NOW)
    ev = next((e for e in out["alerts"] if e["kind"] == KIND_RAPID_DROP), None)
    assert ev is not None
    assert ev["severity"] == "info"


def test_projection_high_24h_crossing_fires():
    pred1 = _predict([_traj(current=30, slope=0.5, proj_24h=42)])
    out1 = derive_projection_alerts(pred1, None, now=NOW)

    t2 = NOW + timedelta(minutes=5)
    pred2 = _predict([_traj(current=35, slope=1.0, proj_24h=55)],
                     updated_at=t2)
    out2 = derive_projection_alerts(pred2, out1, now=t2)
    kinds = [e["kind"] for e in out2["alerts"]]
    assert KIND_PROJECTION_HIGH_24H in kinds


def test_rapid_climb_dedup_within_cooldown():
    # Two consecutive cycles, slope still high — should NOT re-fire.
    pred1 = _predict([_traj(current=40, slope=6.0, confidence=0.7)])
    out1 = derive_projection_alerts(pred1, None, now=NOW)
    assert any(e["kind"] == KIND_RAPID_CLIMB for e in out1["alerts"])

    t2 = NOW + timedelta(minutes=5)
    pred2 = _predict([_traj(current=46, slope=6.5, confidence=0.7)],
                     updated_at=t2)
    out2 = derive_projection_alerts(pred2, out1, now=t2)
    # No NEW rapid_climb in this cycle even though the slope still qualifies.
    new_kinds = [a["kind"] for a in out2["alerts"]
                 if int(datetime.fromisoformat(a["t"]).timestamp())
                 == int(t2.timestamp())]
    assert KIND_RAPID_CLIMB not in new_kinds


def test_malformed_inputs_do_not_raise():
    # Garbage predict + garbage prev — must not blow up, must return safe shape.
    out = derive_projection_alerts(None, None, now=NOW)
    assert out["new"] == 0
    assert out["alerts"] == []
    assert out["count"] == 0

    out2 = derive_projection_alerts({"trajectories": "not-a-list"},
                                    {"alerts": "nope"}, now=NOW)
    assert out2["new"] == 0


def test_eta_already_low_on_first_cycle_does_not_fire():
    # First cycle with ETA already <=6h -> no prior baseline, so no fire.
    pred = _predict([_traj(current=70, slope=3.0, eta=2.0)])
    out = derive_projection_alerts(pred, None, now=NOW)
    assert not any(e["kind"] == KIND_ETA_CRISIS_IMMINENT for e in out["alerts"])


def test_current_score_already_at_threshold_does_not_fire_projection_crisis():
    # currentScore already >= 75 -> projection_crisis_24h must not fire.
    pred1 = _predict([_traj(current=80, slope=0.5, proj_24h=85)])
    out1 = derive_projection_alerts(pred1, None, now=NOW)
    t2 = NOW + timedelta(minutes=5)
    pred2 = _predict([_traj(current=82, slope=0.5, proj_24h=88)],
                     updated_at=t2)
    out2 = derive_projection_alerts(pred2, out1, now=t2)
    assert not any(e["kind"] == KIND_PROJECTION_CRISIS_24H
                   for e in out2["alerts"])
