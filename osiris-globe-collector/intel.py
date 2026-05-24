"""Derived intelligence products built from a single forecast pass.

The collector builds the forecast once per cycle (forecast.build_forecast_full),
then derives three further blobs from that same in-memory result — no extra API
reads:

    brief    a narrative "intel brief": BLUF, a ranked watchboard, which kinetic
             escalation tells are lit right now, and a short read per top hotspot.
    alerts   a rolling event log: an entry is appended whenever a hotspot crosses
             an escalation LEVEL or its score jumps/drops sharply between cycles.
    history  a rolling time-series (compact score-per-hotspot + feed counts) the
             playback scrubber replays so you can watch a hotspot's score climb.

Every function here is PURE (no network, no clock except the explicit `now`),
so the collector stays the single orchestrator and these stay unit-testable.
All three reuse forecast.py's level thresholds and weights for consistency.
"""
from datetime import datetime, timezone, timedelta

from forecast import WEIGHTS, _level

# The four kinetic, hard-to-fake signal families. Their presence is the strongest
# escalation tell — these are what "lights up" on the brief board.
_KINETIC_TELLS = [
    ("military_air", "Military air activity",
     "Fighters, tankers, ISR or strategic lift operating in-theatre"),
    ("naval", "Naval surge",
     "Warships / carrier presence or unusual vessel movement in-box"),
    ("nav_warnings", "Declared danger zones",
     "NAVAREA maritime warnings — live-fire, missile or exercise areas"),
    ("frontlines", "Active frontlines",
     "Ground-combat contact lines inside the box"),
]

# Feeds we summarise on the brief + record in the history time-series.
METRIC_LAYERS = ["flights", "military_air", "military_naval", "boats",
                 "nav_warnings", "events"]


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat()


def _signal(hotspot, key):
    """Return the signal dict for `key` on a forecast hotspot, or None."""
    for s in hotspot.get("signals", []):
        if s.get("key") == key:
            return s
    return None


# ===========================================================================
# Intel brief
# ===========================================================================
def build_brief(forecast, counts, *, now=None):
    """Narrative intelligence brief derived from the scored forecast.

    counts: {layer_id: int} live feed counts (for the "feeds" line).
    """
    now = now or _now()
    hotspots = sorted(forecast.get("hotspots", []),
                      key=lambda h: h["score"], reverse=True)
    top = hotspots[0] if hotspots else None

    # --- Kinetic tells: lit if ANY hotspot shows real signal for that family ---
    tells = []
    for key, label, detail in _KINETIC_TELLS:
        lit_in = []
        peak = 0
        for h in hotspots:
            sig = _signal(h, key)
            if sig and sig.get("score", 0) >= 35:   # meaningful, not trace noise
                lit_in.append((h["name"], sig["score"]))
                peak = max(peak, sig["score"])
        lit_in.sort(key=lambda x: x[1], reverse=True)
        where = ", ".join(n for n, _ in lit_in[:4])
        tells.append({
            "key": key,
            "label": label,
            "lit": bool(lit_in),
            "peak": peak,
            "detail": (f"{detail}. Strongest: {where}." if lit_in
                       else f"{detail}. Not lit anywhere right now."),
        })

    # --- Watchboard: ranked hotspots with their level + one-line read ----------
    watchboard = [{
        "id": h["id"], "name": h["name"], "lat": h["lat"], "lng": h["lng"],
        "score": h["score"], "level": h["level"], "summary": h.get("summary", ""),
    } for h in hotspots]

    # --- BLUF -----------------------------------------------------------------
    n_crisis = sum(1 for h in hotspots if h["level"] == "crisis")
    n_high = sum(1 for h in hotspots if h["level"] == "high")
    lit_tells = [t["label"] for t in tells if t["lit"]]
    if top:
        bluf = (
            f"Top hotspot is {top['name']} at {top['score']}/100 "
            f"({top['level']}). "
            + (f"{n_crisis} region(s) at crisis and {n_high} at high. "
               if (n_crisis or n_high) else "No region at high or crisis. ")
            + ("Kinetic indicators lit: " + ", ".join(lit_tells) + "."
               if lit_tells else "No kinetic indicators currently lit.")
        )
        headline = (f"{top['name']} leads at {top['score']}/100 "
                    f"({top['level'].upper()})")
    else:
        bluf = "No hotspot data available this cycle."
        headline = "No data"

    # --- Per-top-hotspot sections (top 5, score>0) ----------------------------
    sections = []
    for h in hotspots[:5]:
        if h["score"] <= 0:
            continue
        drivers = sorted(
            (s for s in h.get("signals", []) if s.get("score", 0) > 0),
            key=lambda s: s["score"] * WEIGHTS.get(s["key"], 0), reverse=True)
        bullets = [f"{s['label']}: {s['note']}" for s in drivers[:4]]
        sections.append({
            "id": h["id"], "name": h["name"], "score": h["score"],
            "level": h["level"], "summary": h.get("summary", ""),
            "drivers": bullets,
        })

    feeds = {lid: int(counts.get(lid, 0)) for lid in METRIC_LAYERS}

    return {
        "updatedAt": _iso(now),
        "generatedFrom": forecast.get("updatedAt"),
        "headline": headline,
        "bluf": bluf,
        "tells": tells,
        "watchboard": watchboard,
        "sections": sections,
        "feeds": feeds,
    }


# ===========================================================================
# Alerts — rolling event log on level-crossings and sharp score moves
# ===========================================================================
ALERTS_WINDOW = timedelta(days=7)
ALERTS_MAX = 250
SPIKE_DELTA = 12          # score jump within one cycle that warrants an alert
_LEVEL_RANK = {"routine": 0, "elevated": 1, "high": 2, "crisis": 3}


def _alert_severity(kind, level):
    if kind == "level_up":
        return "critical" if level == "crisis" else "warning"
    if kind == "spike":
        return "warning"
    return "info"          # level_down / drop


def derive_alerts(forecast, prev_forecast, prev_alerts, *, now=None,
                  kinetic_insights=None, prev_kinetic_insights=None):
    """Append alert events for level-crossings / sharp moves vs the prior cycle.

    prev_forecast / prev_alerts may be None on cold start (then we only seed the
    rolling log, emitting nothing — we need two cycles to detect a transition).

    kinetic_insights (optional): the latest payload from
    `kinetic_detector.detect_kinetic_insights`. When provided, NEW insight items
    (id not seen in prev_kinetic_insights) with severity in {critical, warning}
    get folded into the alert stream with kind="kinetic_event" so the existing
    LiveAlerts UI surfaces them without any new plumbing."""
    now = now or _now()
    existing = list((prev_alerts or {}).get("alerts", []))

    prev_by_id = {h["id"]: h
                  for h in (prev_forecast or {}).get("hotspots", [])}
    new_events = []
    if prev_forecast:                      # need a baseline to compare against
        for h in forecast.get("hotspots", []):
            ph = prev_by_id.get(h["id"])
            if not ph:
                continue
            cur_s, prev_s = h["score"], ph["score"]
            cur_l, prev_l = h["level"], ph["level"]
            kind = None
            if _LEVEL_RANK.get(cur_l, 0) > _LEVEL_RANK.get(prev_l, 0):
                kind, verb = "level_up", "escalated to"
            elif _LEVEL_RANK.get(cur_l, 0) < _LEVEL_RANK.get(prev_l, 0):
                kind, verb = "level_down", "de-escalated to"
            elif cur_s - prev_s >= SPIKE_DELTA:
                kind, verb = "spike", "spiked to"
            elif prev_s - cur_s >= SPIKE_DELTA:
                kind, verb = "drop", "dropped to"
            if not kind:
                continue
            sev = _alert_severity(kind, cur_l)
            title = f"{h['name']} {verb} {cur_l.upper()} ({cur_s}/100)"
            top_sig = max(
                (s for s in h.get("signals", []) if s.get("score", 0) > 0),
                key=lambda s: s["score"] * WEIGHTS.get(s["key"], 0),
                default=None)
            detail = (top_sig["note"] if top_sig
                      else "no single dominant driver this cycle")
            new_events.append({
                "id": f"{h['id']}-{int(now.timestamp())}",
                "t": _iso(now),
                "severity": sev,
                "kind": kind,
                "hotspotId": h["id"],
                "hotspot": h["name"],
                "lat": h["lat"], "lng": h["lng"],
                "title": title,
                "detail": detail,
                "score": cur_s, "prevScore": prev_s,
                "level": cur_l, "prevLevel": prev_l,
            })

    # Kinetic-event insights — fold NEW critical/warning items into the alert
    # stream so the existing LiveAlerts UI renders them. The detector already
    # applies a per-(cell, signature) cooldown; here we just dedupe by id
    # against the previous cycle.
    if kinetic_insights:
        prev_ids = set()
        if prev_kinetic_insights:
            prev_ids = {i.get("id") for i in (prev_kinetic_insights.get("items") or [])
                        if isinstance(i, dict)}
        for ki in (kinetic_insights.get("items") or []):
            if not isinstance(ki, dict):
                continue
            if ki.get("severity") not in ("critical", "warning"):
                continue
            if ki.get("id") in prev_ids:
                continue
            new_events.append({
                "id": ki["id"],
                "t": ki.get("t") or _iso(now),
                "severity": ki["severity"],
                "kind": "kinetic_event",
                "signature": ki.get("signature"),
                "hotspotId": ki.get("cell_key"),
                "hotspot": ki.get("country") or ki.get("title"),
                "lat": ki.get("lat"), "lng": ki.get("lng"),
                "title": ki.get("title") or "Kinetic event likely",
                "detail": ki.get("summary") or "",
                "confidence": ki.get("confidence"),
            })

    # newest first, trimmed to window + cap
    merged = new_events + existing
    cutoff = now - ALERTS_WINDOW
    kept = []
    truncated = False
    for e in merged:
        try:
            t = datetime.fromisoformat(e["t"])
        except (ValueError, KeyError, TypeError):
            continue
        if t >= cutoff:
            kept.append(e)
        if len(kept) >= ALERTS_MAX:
            # Cap hit before exhausting the in-window history. This is the
            # mass-escalation scenario — operators should know we truncated.
            if len(merged) > ALERTS_MAX:
                truncated = True
            break

    if truncated:
        # Print rather than log (intel.py is import-safe / no logger dependency).
        # The collector logs via its own log.warning; this surfaces in stderr.
        import sys as _sys
        _sys.stderr.write(
            f"WARN intel.derive_alerts: capped at {ALERTS_MAX} events "
            f"(saw {len(merged)} in window), older history dropped\n")

    return {
        "updatedAt": _iso(now),
        "generatedFrom": forecast.get("updatedAt"),
        "count": len(kept),
        "new": len(new_events),
        "alerts": kept,
    }


# ===========================================================================
# History — rolling compact time-series for playback
# ===========================================================================
HISTORY_WINDOW = timedelta(hours=72)
HISTORY_MAX = 1200            # ~ every 6 min over 72h, generous headroom


def append_history(prev_history, forecast, counts, *, now=None):
    """Append one compact sample (per-hotspot score + feed counts), trim to 72h."""
    now = now or _now()
    samples = list((prev_history or {}).get("samples", []))

    sample = {
        "t": _iso(now),
        "scores": {h["id"]: h["score"] for h in forecast.get("hotspots", [])},
        "metrics": {lid: int(counts.get(lid, 0)) for lid in METRIC_LAYERS},
    }
    samples.append(sample)

    cutoff = now - HISTORY_WINDOW
    trimmed = []
    for s in samples:
        try:
            t = datetime.fromisoformat(s["t"])
        except (ValueError, KeyError, TypeError):
            continue
        if t >= cutoff:
            trimmed.append(s)
    trimmed = trimmed[-HISTORY_MAX:]

    # Stable hotspot label/coord index so the scrubber can render without the
    # forecast blob (scores reference these ids).
    index = [{"id": h["id"], "name": h["name"], "lat": h["lat"], "lng": h["lng"]}
             for h in forecast.get("hotspots", [])]

    return {
        "updatedAt": _iso(now),
        "windowHours": int(HISTORY_WINDOW.total_seconds() // 3600),
        "hotspots": index,
        "metricLayers": METRIC_LAYERS,
        "count": len(trimmed),
        "samples": trimmed,
    }
