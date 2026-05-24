"""Per-entity anomaly detector — tracks vessels/aircraft across cycles and emits
events when an entity exhibits a sanctions-evasion / pre-kinetic tell that the
layer/event feeds miss.

Five detection kinds:

  dropped           Entity was being tracked then disappeared from the feed for
                    >=3 consecutive cycles. Classic "dark vessel" / "dark plane"
                    AIS-transponder-off tell. Emitted ONCE per dropout; suppressed
                    until the entity returns. Bonus context: nearby hotspot + EEZ
                    membership (from the optional geo_zones layer).
  mmsi_swap         Same lat/lng (±0.05°) seen with different id between
                    consecutive cycles — classic AIS-spoofing identity swap.
  teleport          Position delta implies > 120kt for boats or > 700kt for
                    flights between consecutive samples — signal corruption or
                    spoof; both are worth surfacing.
  heading_flip      A vessel doing > 5kt previously now reports <= 0.5kt at
                    roughly the same position — common AIS-spoof-of-static tell.
  emergency_squawk  Aircraft transponder squawk in {7500, 7600, 7700}.

Function contract:

    detect_anomalies(layers, prev_state, prev_anomalies, *, now=None)
      -> (payload, new_state)

`layers` is the dict {layer_id: payload} that `_build_intel_cycle` already has
in scope (forecast.build_forecast_full returns it). We only read the boats,
flights, military_air, and (optional) geo_zones entries — every other layer is
ignored, so this is cheap.

The function is pure (no network, no clock except the explicit `now` argument)
so it unit-tests deterministically against synthetic fixtures.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone, timedelta
from typing import Any

# ---------------------------------------------------------------------------
# State / output bounds
# ---------------------------------------------------------------------------
TRACKED_TTL_S = 7200          # 2h: entities unobserved this long drop out
TRACKED_CAP = 10000           # hard cap on per-cycle tracked set
ANOMALIES_WINDOW = timedelta(days=7)
ANOMALIES_MAX = 250

# ---------------------------------------------------------------------------
# Detection thresholds (documented so reviewers can tune)
# ---------------------------------------------------------------------------
# Boats snapshot every ~30s, flights/military_air every ~15s. 3 misses ~= 45-90s
# silent — short enough to be timely, long enough to ignore single dropped
# packets / proxy hiccups.
DROP_MISS_CYCLES = 3

# Same lat/lng tolerance for the mmsi_swap match. ~0.05° ≈ 5.5km at the equator,
# tight enough that two different vessels at the same point is improbable noise.
SWAP_POS_TOL_DEG = 0.05

# Max plausible cycle-over-cycle speed before we call it a teleport (knots).
# Top vessel speeds are ~30-40kt; >120kt is unphysical. Civil jets cruise
# ~400-500kt; >700kt is faster than practically any tracked airframe in level
# flight at normal cycle cadence and signals a spoof or signal-source swap.
TELEPORT_BOAT_KT = 120.0
TELEPORT_PLANE_KT = 700.0

# heading_flip: vessel previously moving meaningfully now reporting static.
HEADING_FLIP_PREV_KT = 5.0
HEADING_FLIP_NOW_KT = 0.5

EMERGENCY_SQUAWKS = {"7500", "7600", "7700"}

# Severities by kind — drives the LED color in the UI.
_SEVERITY = {
    "dropped":          "warning",
    "mmsi_swap":        "warning",
    "teleport":         "info",       # often noise; surfaced but de-emphasized
    "heading_flip":     "warning",
    "emergency_squawk": "critical",
}

# Earth radius in nautical miles (for great-circle distance in kt math).
_R_NM = 3440.065

# Layer sources we ingest. Keys must stay STABLE — they prefix entity_key so
# cross-source id collisions can't happen (a boat MMSI vs a flight hex).
_BOAT_SOURCE = "boats"
_FLIGHT_SOURCES = ("flights", "military_air")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now():
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _haversine_nm(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in nautical miles."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lng2 - lng1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2)
    return 2 * _R_NM * math.asin(min(1.0, math.sqrt(a)))


def _items(layer_payload):
    if not isinstance(layer_payload, dict):
        return []
    items = layer_payload.get("items")
    return items if isinstance(items, list) else []


def _entity_key(source: str, eid) -> str | None:
    if eid is None:
        return None
    return f"{source}:{eid}"


def _flag_for_boat(item) -> str | None:
    return item.get("country_code") or None


def _flag_for_plane(item) -> str | None:
    return item.get("country_code") or None


def _label_for(item) -> str:
    return (item.get("label") or item.get("name") or item.get("callsign")
            or str(item.get("id") or "")).strip() or "unknown"


# ---------------------------------------------------------------------------
# Geo enrichment (optional). We accept the geo_zones blob (LAYERS id
# "geo_zones") and do cheap bbox-of-polygon containment checks. Real point-in-
# polygon would need shapely; bbox is good enough for surfacing context.
# ---------------------------------------------------------------------------
def _coords_bbox(coords):
    """Compute (min_lng, min_lat, max_lng, max_lat) over a nested GeoJSON
    coordinate array. Returns None for an empty/invalid input."""
    min_lng = min_lat = float("inf")
    max_lng = max_lat = float("-inf")
    found = False

    def _walk(c):
        nonlocal min_lng, min_lat, max_lng, max_lat, found
        if isinstance(c, (list, tuple)):
            if c and isinstance(c[0], (int, float)) and len(c) >= 2:
                lng, lat = c[0], c[1]
                if (isinstance(lng, (int, float))
                        and isinstance(lat, (int, float))):
                    if lng < min_lng: min_lng = lng
                    if lng > max_lng: max_lng = lng
                    if lat < min_lat: min_lat = lat
                    if lat > max_lat: max_lat = lat
                    found = True
            else:
                for x in c:
                    _walk(x)

    _walk(coords)
    if not found:
        return None
    return (min_lng, min_lat, max_lng, max_lat)


def _build_zone_index(geo_zones_payload):
    """Pre-compute (bbox, kind, name, country_code) tuples once per cycle so
    each entity lookup is O(zones). Skips features without a usable bbox."""
    out = []
    for f in _items(geo_zones_payload):
        if not isinstance(f, dict):
            continue
        geom = f.get("geometry") or {}
        bbox = _coords_bbox(geom.get("coordinates"))
        if not bbox:
            continue
        props = f.get("properties") or {}
        kind = props.get("kind") or props.get("type") or "zone"
        name = props.get("name") or props.get("title") or ""
        # EEZ features expose an ISO-2 owner under several common keys.
        cc = (props.get("iso2") or props.get("ISO_TER1")
              or props.get("country_code") or props.get("territory_iso2"))
        out.append((bbox, str(kind), str(name), cc.upper() if cc else None))
    return out


def _zone_context(zones, lat, lng):
    """Return {nearby_hotspot, in_eez_of} for an entity at lat/lng. Both are
    None when no zone bbox contains the point."""
    nearby = None
    in_eez = None
    if lat is None or lng is None:
        return {"nearby_hotspot": None, "in_eez_of": None}
    for bbox, kind, name, cc in zones:
        min_lng, min_lat, max_lng, max_lat = bbox
        if not (min_lng <= lng <= max_lng and min_lat <= lat <= max_lat):
            continue
        if kind == "eez" and in_eez is None and cc:
            in_eez = cc
        elif kind in ("sanctions", "hotspot", "corridor", "chokepoint") \
                and nearby is None:
            nearby = name.lower() or kind
    return {"nearby_hotspot": nearby, "in_eez_of": in_eez}


# ---------------------------------------------------------------------------
# Per-source observation -> normalized tracked record
# ---------------------------------------------------------------------------
def _observe_boat(item) -> dict | None:
    """Normalize one boats item to the in-state record shape."""
    lat, lng = item.get("lat"), item.get("lng")
    eid = item.get("id")
    if lat is None or lng is None or eid is None:
        return None
    return {
        "source": _BOAT_SOURCE,
        "id": str(eid),
        "lat": float(lat), "lng": float(lng),
        "speed": item.get("speed"), "heading": item.get("heading"),
        "label": _label_for(item),
        "flag": _flag_for_boat(item),
        "squawk": None,
    }


def _observe_plane(item, source) -> dict | None:
    lat, lng = item.get("lat"), item.get("lng")
    eid = item.get("id")
    if lat is None or lng is None or eid is None:
        return None
    return {
        "source": source,
        "id": str(eid),
        "lat": float(lat), "lng": float(lng),
        "speed": item.get("speed"), "heading": item.get("track"),
        "label": _label_for(item),
        "flag": _flag_for_plane(item),
        "squawk": (str(item.get("squawk")).strip()
                   if item.get("squawk") is not None else None),
    }


def _collect_observations(layers) -> dict[str, dict]:
    """Pull entity observations from boats + flights + military_air. Keyed by
    `<source>:<id>` so cross-source ids never collide."""
    obs: dict[str, dict] = {}

    for item in _items(layers.get(_BOAT_SOURCE)):
        rec = _observe_boat(item)
        if not rec:
            continue
        key = _entity_key(_BOAT_SOURCE, rec["id"])
        if key:
            obs[key] = rec

    for src in _FLIGHT_SOURCES:
        for item in _items(layers.get(src)):
            rec = _observe_plane(item, src)
            if not rec:
                continue
            key = _entity_key(src, rec["id"])
            if key:
                obs[key] = rec

    return obs


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------
def _kt_between(prev_rec, cur_rec, dt_s):
    """Instantaneous speed required to move from prev -> cur in dt_s seconds."""
    if dt_s <= 0:
        return 0.0
    dist_nm = _haversine_nm(prev_rec["lat"], prev_rec["lng"],
                            cur_rec["lat"], cur_rec["lng"])
    return dist_nm / (dt_s / 3600.0)


def _event_id(key: str, kind: str, ts: float) -> str:
    safe = key.replace(":", "-")
    return f"anom-{safe}-{kind}-{int(ts)}"


def _emit(events, *, kind, rec, severity, detail, now, swap_from=None, context=None):
    ev = {
        "id": _event_id(_entity_key(rec["source"], rec["id"]), kind, now.timestamp()),
        "t": _iso(now),
        "severity": severity,
        "kind": kind,
        "entity_key": _entity_key(rec["source"], rec["id"]),
        "source": rec["source"],
        "entity_id": rec["id"],
        "label": rec.get("label") or rec["id"],
        "lat": rec["lat"], "lng": rec["lng"],
        "flag": rec.get("flag"),
        "detail": detail,
        "context": context or {"nearby_hotspot": None, "in_eez_of": None},
    }
    if swap_from is not None:
        ev["swap_from"] = swap_from
    events.append(ev)


def _detect_swap_candidates(obs, prev_tracked):
    """Build {(source, rounded lat, rounded lng) -> [id]} index over PREVIOUS
    cycle so we can match a new id at the same point to an old id that vanished
    on this cycle. Quantizing to SWAP_POS_TOL_DEG groups co-located entries."""
    bucket = SWAP_POS_TOL_DEG
    if bucket <= 0:
        return {}
    index = {}
    for key, rec in prev_tracked.items():
        lat = rec.get("last_pos", [None, None])[0]
        lng = rec.get("last_pos", [None, None])[1]
        src = rec.get("source")
        if lat is None or lng is None or src is None:
            continue
        k = (src, round(lat / bucket), round(lng / bucket))
        index.setdefault(k, []).append(key)
    return index


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def detect_anomalies(layers, prev_state, prev_anomalies, *, now=None):
    """Detect per-entity anomalies for this cycle.

    layers:          {layer_id: payload} — only boats/flights/military_air/
                     geo_zones are read.
    prev_state:      output of the previous call's `new_state` (or None on
                     cold start).
    prev_anomalies:  output of the previous call's `payload` (or None on cold
                     start) — provides the rolling event log.
    now:             explicit clock for tests; defaults to wall clock.

    Returns (payload, new_state). `payload` follows the documented contract;
    `new_state` is a JSON-serializable dict ready to round-trip via blob."""
    now = now or _now()
    now_ts = now.timestamp()

    prev_state = prev_state or {"tracked": {}, "ttl_s": TRACKED_TTL_S}
    prev_tracked = dict(prev_state.get("tracked") or {})
    existing_events = list((prev_anomalies or {}).get("events", []))

    obs = _collect_observations(layers)
    zones = _build_zone_index(layers.get("geo_zones"))
    swap_index = _detect_swap_candidates(obs, prev_tracked)

    new_events: list[dict[str, Any]] = []
    new_tracked: dict[str, dict] = {}

    # ---- Pass 1: every CURRENT observation updates state + checks teleport /
    #               heading_flip / emergency_squawk / mmsi_swap. -------------
    for key, rec in obs.items():
        prev = prev_tracked.get(key)
        ctx = _zone_context(zones, rec["lat"], rec["lng"])

        # Carry forward dropout-suppression bookkeeping so we don't re-fire.
        prev_dropped_fired = bool(prev and prev.get("dropped_fired"))

        # teleport: needs a prev sample with a usable timestamp.
        if prev and prev.get("last_seen_ts"):
            dt_s = max(1.0, now_ts - float(prev["last_seen_ts"]))
            kt = _kt_between(
                {"lat": prev["last_pos"][0], "lng": prev["last_pos"][1]},
                rec, dt_s)
            limit = TELEPORT_BOAT_KT if rec["source"] == _BOAT_SOURCE \
                else TELEPORT_PLANE_KT
            if kt > limit:
                _emit(new_events, kind="teleport", rec=rec, severity=_SEVERITY["teleport"],
                      detail=(f"Implied speed {int(kt)}kt between cycles "
                              f"({dt_s:.0f}s gap) — exceeds {int(limit)}kt sanity bound."),
                      now=now, context=ctx)

        # heading_flip: prev moving meaningfully, now near-static at ~same spot.
        if prev and rec["source"] == _BOAT_SOURCE:
            prev_spd = prev.get("last_speed")
            cur_spd = rec.get("speed")
            if (isinstance(prev_spd, (int, float))
                    and isinstance(cur_spd, (int, float))
                    and prev_spd > HEADING_FLIP_PREV_KT
                    and cur_spd <= HEADING_FLIP_NOW_KT):
                # require they're still in the same region (didn't legitimately
                # arrive somewhere else — that's a different story)
                dist_nm = _haversine_nm(prev["last_pos"][0], prev["last_pos"][1],
                                        rec["lat"], rec["lng"])
                if dist_nm < 2.0:
                    _emit(new_events, kind="heading_flip", rec=rec,
                          severity=_SEVERITY["heading_flip"],
                          detail=(f"Speed dropped from {prev_spd:.1f}kt to "
                                  f"{cur_spd:.1f}kt at the same position — "
                                  "possible AIS spoof of static."),
                          now=now, context=ctx)

        # emergency_squawk: aircraft only.
        if rec["source"] in _FLIGHT_SOURCES and rec.get("squawk") in EMERGENCY_SQUAWKS:
            sq = rec["squawk"]
            sq_label = {"7500": "hijack", "7600": "radio failure",
                        "7700": "general emergency"}.get(sq, sq)
            # Suppress repeat fires while the squawk persists.
            prev_sq = (prev or {}).get("last_squawk")
            if prev_sq != sq:
                _emit(new_events, kind="emergency_squawk", rec=rec,
                      severity=_SEVERITY["emergency_squawk"],
                      detail=f"Squawk {sq} ({sq_label}).",
                      now=now, context=ctx)

        # mmsi_swap: a NEW key at a bucket that previously held a DIFFERENT key
        # which is now ABSENT from this cycle's observations.
        if not prev:
            bucket = SWAP_POS_TOL_DEG
            bk = (rec["source"], round(rec["lat"] / bucket), round(rec["lng"] / bucket))
            for candidate_key in swap_index.get(bk, []):
                if candidate_key == key:
                    continue
                if candidate_key in obs:
                    continue   # the old id is ALSO still here -> not a swap
                _, swap_from_id = candidate_key.split(":", 1)
                _emit(new_events, kind="mmsi_swap", rec=rec,
                      severity=_SEVERITY["mmsi_swap"],
                      detail=(f"New id {rec['id']} appeared at the same "
                              f"position previously occupied by {swap_from_id}."),
                      now=now, swap_from=swap_from_id, context=ctx)
                break

        # Update / create state entry.
        history = list((prev or {}).get("history", []))
        history.append({"t": now_ts, "lat": rec["lat"], "lng": rec["lng"],
                        "speed": rec.get("speed")})
        # keep at most 8 samples (light memory, enough for trend inspection)
        history = history[-8:]

        # If the entity just reappeared after a 'dropped' fire, reset suppression.
        dropped_fired = prev_dropped_fired
        if prev_dropped_fired:
            dropped_fired = False

        new_tracked[key] = {
            "source": rec["source"],
            "id": rec["id"],
            "last_seen_ts": now_ts,
            "last_pos": [rec["lat"], rec["lng"]],
            "last_speed": rec.get("speed"),
            "last_heading": rec.get("heading"),
            "last_squawk": rec.get("squawk"),
            "label": rec.get("label"),
            "flag": rec.get("flag"),
            "first_seen_ts": (prev or {}).get("first_seen_ts", now_ts),
            "miss_count": 0,
            "dropped_fired": dropped_fired,
            "history": history,
        }

    # ---- Pass 2: anyone in prev_tracked we DIDN'T observe -> increment miss
    #              counter; fire `dropped` once when threshold crossed. ------
    for key, prev in prev_tracked.items():
        if key in new_tracked:
            continue
        # Drop entirely if past TTL since last observation.
        last_ts = prev.get("last_seen_ts") or 0
        age_s = now_ts - float(last_ts)
        if age_s > TRACKED_TTL_S:
            continue                                      # evict, no event

        miss_count = int(prev.get("miss_count") or 0) + 1
        already_fired = bool(prev.get("dropped_fired"))
        rec_for_ctx = {
            "source": prev.get("source"),
            "id": prev.get("id"),
            "label": prev.get("label") or prev.get("id"),
            "lat": prev["last_pos"][0],
            "lng": prev["last_pos"][1],
            "flag": prev.get("flag"),
        }
        # Initialize BEFORE the branch so the new_tracked write below can never
        # hit UnboundLocalError if _emit raises (e.g. on a malformed last_pos
        # from a partial restart). The branch overwrites on the success path.
        fired = already_fired
        if miss_count >= DROP_MISS_CYCLES and not already_fired:
            ctx = _zone_context(zones, rec_for_ctx["lat"], rec_for_ctx["lng"])
            active_s = float(last_ts) - float(prev.get("first_seen_ts") or last_ts)
            active_m = int(active_s / 60) if active_s > 0 else 0
            where = ""
            if ctx.get("in_eez_of"):
                where = f" Last seen inside {ctx['in_eez_of']} EEZ."
            elif ctx.get("nearby_hotspot"):
                where = f" Last seen near {ctx['nearby_hotspot']}."
            _emit(new_events, kind="dropped", rec=rec_for_ctx,
                  severity=_SEVERITY["dropped"],
                  detail=(f"Stopped broadcasting after {active_m}m active "
                          f"({miss_count} consecutive misses).{where}"),
                  now=now, context=ctx)
            fired = True

        new_tracked[key] = {
            **prev,
            "miss_count": miss_count,
            "dropped_fired": fired,
        }

    # ---- Cap the tracked set (oldest-last_seen evicted first). --------------
    if len(new_tracked) > TRACKED_CAP:
        ordered = sorted(new_tracked.items(),
                         key=lambda kv: kv[1].get("last_seen_ts", 0))
        for key, _v in ordered[:len(new_tracked) - TRACKED_CAP]:
            new_tracked.pop(key, None)

    # ---- Rolling window: newest first, trim to 7d / 250. --------------------
    merged = new_events + existing_events
    cutoff = now - ANOMALIES_WINDOW
    kept: list[dict] = []
    for e in merged:
        try:
            t = datetime.fromisoformat(e["t"])
        except (ValueError, KeyError, TypeError):
            continue
        if t >= cutoff:
            kept.append(e)
        if len(kept) >= ANOMALIES_MAX:
            break

    payload = {
        "updatedAt": _iso(now),
        "count": len(kept),
        "new": len(new_events),
        "events": kept,
    }
    new_state = {
        "tracked": new_tracked,
        "ttl_s": TRACKED_TTL_S,
    }
    return payload, new_state
