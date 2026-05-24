"""Trajectory-projection alerts — forward-looking event log.

Distinct from `intel.derive_alerts` (which fires on present-state level crossings
and on the concurrent session's `kinetic_event` items) and from the concurrent
session's `kinetic_insights` work (which fires on detected kinetic activity
happening NOW). This module fires when the predictive forecast — the output of
`predict.predict_trajectories` — projects that a hotspot will cross a meaningful
future threshold:

  - eta_crisis_imminent (critical): ETA-to-crisis drops to <=6h after being >6h
    (or null) the previous cycle.
  - projection_crisis_24h (warning): 24h projection crosses up through 75 while
    the current score is still below 75.
  - projection_high_24h (warning): 24h projection crosses up through 50 while
    the current score is still below 50.
  - rapid_climb (warning): slope > +5/hr with confidence > 0.6.
  - rapid_drop (info):    slope < -5/hr with confidence > 0.6.

Pure / deterministic / stdlib-only. No clock except the explicit `now` arg.
Mirrors the EscalationAlert shape so the existing AlertsFeed UI surfaces these
without new plumbing.
"""
from datetime import datetime, timezone, timedelta


# Rolling-log sizing (per spec: 200 events / 7 days). Slightly tighter than
# intel.derive_alerts (250/7d) on purpose — projection events should be rarer.
ALERTS_WINDOW = timedelta(days=7)
ALERTS_MAX = 200

# Dedup cooldown: once a (hotspot, kind) fires, suppress re-fires for this long
# UNLESS the trigger condition first un-arms (e.g. ETA goes back above 6h, or
# projection drops below the threshold). The cooldown prevents a single quivering
# series from spamming the feed when it sits right at the edge.
DEDUP_COOLDOWN = timedelta(hours=24)

# Trigger thresholds.
ETA_CRISIS_HOURS = 6.0
PROJ_CRISIS_THRESHOLD = 75
PROJ_HIGH_THRESHOLD = 50
RAPID_SLOPE_ABS = 5.0
RAPID_MIN_CONFIDENCE = 0.6

# Alert kinds — exported so tests / parent integration can reference them.
KIND_ETA_CRISIS_IMMINENT = "eta_crisis_imminent"
KIND_PROJECTION_CRISIS_24H = "projection_crisis_24h"
KIND_PROJECTION_HIGH_24H = "projection_high_24h"
KIND_RAPID_CLIMB = "rapid_climb"
KIND_RAPID_DROP = "rapid_drop"

# Kinds that are "crossing" alerts — they need the previous cycle's state to
# decide whether to fire (require an un-armed -> armed transition). State alerts
# (rapid_climb / rapid_drop) only need the cooldown.
_CROSSING_KINDS = {
    KIND_ETA_CRISIS_IMMINENT,
    KIND_PROJECTION_CRISIS_24H,
    KIND_PROJECTION_HIGH_24H,
}


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat()


def _parse_t(s):
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def _fmt_eta(hours):
    """Compact human ETA string. Mirrors predict._fmt_eta for consistency."""
    if hours is None:
        return None
    if hours < 1:
        mins = max(1, int(round(hours * 60)))
        return f"~{mins}m"
    if hours < 10:
        return f"~{hours:.1f}h"
    return f"~{int(round(hours))}h"


def _last_fire_for(prev_alerts, hotspot_id, kind):
    """Most-recent timestamp this (hotspot, kind) fired in the rolling log, or
    None. Used for cooldown + crossing-re-arm decisions."""
    latest = None
    for e in prev_alerts:
        if e.get("hotspotId") != hotspot_id or e.get("kind") != kind:
            continue
        t = _parse_t(e.get("t"))
        if t is None:
            continue
        if latest is None or t > latest:
            latest = t
    return latest


def _in_cooldown(prev_alerts, hotspot_id, kind, now):
    last = _last_fire_for(prev_alerts, hotspot_id, kind)
    if last is None:
        return False
    return (now - last) < DEDUP_COOLDOWN


def _check_eta_crisis_imminent(traj, prev_traj):
    """Critical: ETA drops to <=6h after being >6h (or null/unset) last cycle.
    Re-armed when ETA goes back ABOVE 6h between cycles."""
    eta = traj.get("eta_to_crisis")
    if eta is None or eta > ETA_CRISIS_HOURS:
        return False
    # Need a previous cycle to confirm the crossing direction.
    if prev_traj is None:
        return False
    prev_eta = prev_traj.get("eta_to_crisis")
    # Fire when previous ETA was unset (no path to crisis) or above the threshold.
    return prev_eta is None or prev_eta > ETA_CRISIS_HOURS


def _check_projection_crisis_24h(traj, prev_traj):
    """Warning: 24h projection crosses up through 75 while currentScore < 75."""
    if traj.get("currentScore", 0) >= PROJ_CRISIS_THRESHOLD:
        return False
    if traj.get("projection_24h", 0) < PROJ_CRISIS_THRESHOLD:
        return False
    if prev_traj is None:
        return False
    # Re-arm when previous projection was below the threshold.
    return prev_traj.get("projection_24h", 0) < PROJ_CRISIS_THRESHOLD


def _check_projection_high_24h(traj, prev_traj):
    """Warning: 24h projection crosses up through 50 while currentScore < 50."""
    if traj.get("currentScore", 0) >= PROJ_HIGH_THRESHOLD:
        return False
    if traj.get("projection_24h", 0) < PROJ_HIGH_THRESHOLD:
        return False
    if prev_traj is None:
        return False
    return prev_traj.get("projection_24h", 0) < PROJ_HIGH_THRESHOLD


def _check_rapid_climb(traj):
    return (traj.get("slope", 0) > RAPID_SLOPE_ABS
            and traj.get("confidence", 0) > RAPID_MIN_CONFIDENCE)


def _check_rapid_drop(traj):
    return (traj.get("slope", 0) < -RAPID_SLOPE_ABS
            and traj.get("confidence", 0) > RAPID_MIN_CONFIDENCE)


def _make_event(traj, kind, severity, title, detail, now):
    """Build an alert event mirroring the EscalationAlert shape."""
    return {
        "id": f"proj-{traj['id']}-{kind}-{int(now.timestamp())}",
        "t": _iso(now),
        "severity": severity,
        "kind": kind,
        "hotspotId": traj["id"],
        "hotspot": traj.get("name"),
        "lat": traj.get("lat"),
        "lng": traj.get("lng"),
        "title": title,
        "detail": detail,
        "currentScore": int(traj.get("currentScore", 0)),
        "projection_6h": int(traj.get("projection_6h", 0)),
        "projection_24h": int(traj.get("projection_24h", 0)),
        "slope": round(float(traj.get("slope", 0.0)), 3),
        "eta_to_crisis": traj.get("eta_to_crisis"),
        "confidence": traj.get("confidence"),
    }


def _build_event(traj, prev_traj, kind, now):
    """Compose the title/detail/severity for `kind` and return the event dict."""
    name = traj.get("name") or traj.get("id")
    current = int(traj.get("currentScore", 0))
    proj_6h = int(traj.get("projection_6h", 0))
    proj_24h = int(traj.get("projection_24h", 0))
    slope = float(traj.get("slope", 0.0))
    eta = traj.get("eta_to_crisis")
    conf = float(traj.get("confidence", 0.0))

    if kind == KIND_ETA_CRISIS_IMMINENT:
        title = f"{name} projected to crisis ({_fmt_eta(eta)})"
        detail = (f"Slope {slope:+.1f}/hr, confidence {conf:.2f}, "
                  f"current {current}, projected {proj_6h} in 6h")
        return _make_event(traj, kind, "critical", title, detail, now)

    if kind == KIND_PROJECTION_CRISIS_24H:
        title = f"{name} 24h projection crosses crisis ({proj_24h})"
        detail = (f"Slope {slope:+.1f}/hr, confidence {conf:.2f}, "
                  f"current {current}, projected {proj_24h} in 24h")
        return _make_event(traj, kind, "warning", title, detail, now)

    if kind == KIND_PROJECTION_HIGH_24H:
        title = f"{name} 24h projection crosses high ({proj_24h})"
        detail = (f"Slope {slope:+.1f}/hr, confidence {conf:.2f}, "
                  f"current {current}, projected {proj_24h} in 24h")
        return _make_event(traj, kind, "warning", title, detail, now)

    if kind == KIND_RAPID_CLIMB:
        title = f"{name} climbing rapidly (slope {slope:+.1f}/hr)"
        detail = (f"Confidence {conf:.2f}, current {current}, "
                  f"projected {proj_6h} in 6h, {proj_24h} in 24h")
        return _make_event(traj, kind, "warning", title, detail, now)

    if kind == KIND_RAPID_DROP:
        title = f"{name} falling rapidly (slope {slope:+.1f}/hr)"
        detail = (f"Confidence {conf:.2f}, current {current}, "
                  f"projected {proj_6h} in 6h, {proj_24h} in 24h")
        return _make_event(traj, kind, "info", title, detail, now)

    raise ValueError(f"unknown projection alert kind: {kind}")


def _detect_for_hotspot(traj, prev_traj, prev_alerts, now):
    """Yield event dicts for every kind that fires on this hotspot this cycle.

    Crossing kinds need prev_traj to confirm an un-armed -> armed transition.
    All kinds respect the DEDUP_COOLDOWN against prev_alerts."""
    hid = traj["id"]
    events = []

    # --- ETA imminent ---------------------------------------------------------
    if _check_eta_crisis_imminent(traj, prev_traj):
        if not _in_cooldown(prev_alerts, hid, KIND_ETA_CRISIS_IMMINENT, now):
            events.append(_build_event(traj, prev_traj,
                                       KIND_ETA_CRISIS_IMMINENT, now))

    # --- Projection-crisis 24h ------------------------------------------------
    if _check_projection_crisis_24h(traj, prev_traj):
        if not _in_cooldown(prev_alerts, hid, KIND_PROJECTION_CRISIS_24H, now):
            events.append(_build_event(traj, prev_traj,
                                       KIND_PROJECTION_CRISIS_24H, now))

    # --- Projection-high 24h --------------------------------------------------
    if _check_projection_high_24h(traj, prev_traj):
        if not _in_cooldown(prev_alerts, hid, KIND_PROJECTION_HIGH_24H, now):
            events.append(_build_event(traj, prev_traj,
                                       KIND_PROJECTION_HIGH_24H, now))

    # --- Rapid climb (state alert, no prev_traj required) --------------------
    if _check_rapid_climb(traj):
        if not _in_cooldown(prev_alerts, hid, KIND_RAPID_CLIMB, now):
            events.append(_build_event(traj, prev_traj,
                                       KIND_RAPID_CLIMB, now))

    # --- Rapid drop (state alert) --------------------------------------------
    if _check_rapid_drop(traj):
        if not _in_cooldown(prev_alerts, hid, KIND_RAPID_DROP, now):
            events.append(_build_event(traj, prev_traj,
                                       KIND_RAPID_DROP, now))

    return events


def _prev_traj_index(prev_projection_alerts):
    """Reconstruct the previous cycle's trajectory state by reading the most
    recent event per hotspot from the rolling log.

    The rolling log doesn't actually contain a snapshot of every trajectory each
    cycle (that would balloon storage); it only contains FIRED events. For
    crossing dedup we instead use _in_cooldown + _last_fire_for, which is the
    semantically-correct way to enforce the re-arm rule via the rolling log.

    This index is therefore a thin shim: it returns None for everyone. The
    crossing-detection helpers treat `prev_traj is None` as "no baseline" and
    refuse to fire on the very first cycle — which is exactly the cold-start
    requirement in the spec. Subsequent cycles need a real prev_traj snapshot
    to confirm crossings; the parent passes that in via the `prev_predict`
    field on prev_projection_alerts (see derive_projection_alerts).
    """
    return {}


def derive_projection_alerts(predict, prev_projection_alerts, *, now=None):
    """Pure: append forward-looking alert events to a rolling log.

    predict: the latest payload from predict.predict_trajectories.
    prev_projection_alerts: the previous output of THIS function, or None on
        cold start. We use it for (a) the rolling event log (for dedup +
        retention) and (b) the embedded `prev_predict` snapshot (for crossing
        detection — we need last cycle's per-hotspot slope/ETA/projection to
        decide whether a threshold was actually crossed).
    now: optional override for testability.

    Returns a fresh payload (does NOT mutate prev_projection_alerts):
        {
          "updatedAt": iso,
          "generatedFrom": predict.updatedAt,
          "count": int,                  # total events in trimmed log
          "new": int,                    # events added this cycle
          "alerts": [ ... ],             # newest first, capped + windowed
          "prev_predict": { ... },       # snapshot to seed next cycle
        }
    """
    now = now or _now()

    trajectories = []
    if isinstance(predict, dict):
        raw = predict.get("trajectories")
        if isinstance(raw, list):
            trajectories = raw

    existing = []
    prev_predict = None
    if isinstance(prev_projection_alerts, dict):
        raw_alerts = prev_projection_alerts.get("alerts")
        if isinstance(raw_alerts, list):
            existing = list(raw_alerts)
        pp = prev_projection_alerts.get("prev_predict")
        if isinstance(pp, dict):
            prev_predict = pp

    # Build a {hotspot_id: trajectory} lookup for the previous cycle.
    prev_by_id = {}
    if prev_predict:
        for t in prev_predict.get("trajectories", []) or []:
            if isinstance(t, dict) and t.get("id"):
                prev_by_id[t["id"]] = t

    new_events = []
    for traj in trajectories:
        if not isinstance(traj, dict) or not traj.get("id"):
            continue
        prev_traj = prev_by_id.get(traj["id"])
        new_events.extend(_detect_for_hotspot(traj, prev_traj, existing, now))

    # Merge newest-first, trim to window + cap.
    merged = new_events + existing
    cutoff = now - ALERTS_WINDOW
    kept = []
    for e in merged:
        t = _parse_t(e.get("t"))
        if t is None or t < cutoff:
            continue
        kept.append(e)
        if len(kept) >= ALERTS_MAX:
            break

    # Snapshot the trajectories we just consumed so next cycle has a baseline
    # for crossing detection. We keep only the minimal fields the detectors
    # actually read — this keeps the rolling-log blob small.
    snapshot = {
        "updatedAt": predict.get("updatedAt") if isinstance(predict, dict) else None,
        "trajectories": [{
            "id": t["id"],
            "currentScore": t.get("currentScore"),
            "projection_6h": t.get("projection_6h"),
            "projection_24h": t.get("projection_24h"),
            "slope": t.get("slope"),
            "eta_to_crisis": t.get("eta_to_crisis"),
            "confidence": t.get("confidence"),
        } for t in trajectories if isinstance(t, dict) and t.get("id")],
    }

    return {
        "updatedAt": _iso(now),
        "generatedFrom": (predict or {}).get("updatedAt") if isinstance(predict, dict) else None,
        "count": len(kept),
        "new": len(new_events),
        "alerts": kept,
        "prev_predict": snapshot,
    }
