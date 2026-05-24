"""Per-entity trail recorder — a rolling position track for every active aircraft
and vessel the collector has seen in the last ~6h.

The point: a user clicks a carrier strike group on the globe and wants to see
where it has actually been over the last few hours and scrub through that path
in time. We already pull boats / flights / military_air / military_naval every
~10-90s, so all we need is to APPEND each observation to a per-entity rolling
deque and publish the result as a single compact blob.

Compactness is critical — a naive `{lat, lng, ts}` dict for 2k entities × 240
samples is several MB. We:
  - cap to the top ~2000 currently-active entities (most-recently observed),
  - cap each entity's deque to 240 samples (~6h at 90s cadence),
  - store each sample as a tuple `[ts_ms, lat*1e4, lng*1e4(, speed*10, heading*10)]`
    (~5x smaller than the dict form),
  - store static metadata (label / source / flag / kind) ONCE at the entity
    level, never per-sample,
  - drop any entity not observed in > 6h.

Pure-function pattern (mirrors intel.append_history): `now`/no-clock,
state-in-state-out, easy to unit-test."""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from collections import deque, OrderedDict
import json

# Sources we record trails for. These are the only feeds with stable per-entity
# ids + lat/lng on every observation — fixed installations (military_bases,
# nuclear) don't move and don't need a trail.
TRAIL_SOURCES = ("flights", "military_air", "boats", "military_naval")

# Mapping source -> kind tag used by the frontend to pick the icon / readout
# style. "vessel" vs "flight" is the only distinction the UI cares about.
SOURCE_KIND = {
    "flights": "flight",
    "military_air": "flight",
    "boats": "vessel",
    "military_naval": "vessel",
}

# Default caps — overridable via record_trails kwargs for tests.
DEFAULT_MAX_ENTITIES = 2000
DEFAULT_MAX_POINTS = 240          # ~6h at 90s cadence
DEFAULT_WINDOW_HOURS = 6
# If the serialized blob exceeds this we aggressively re-trim per-entity sample
# counts until it fits. ~1.5MB is well within Vercel-Blob limits and acceptable
# for a "polled every minute" client payload.
BLOB_SIZE_BUDGET_BYTES = 1_500_000


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _items(layer_payload):
    """Same shape every collector layer returns: {"items": [...], ...}."""
    if not isinstance(layer_payload, dict):
        return []
    items = layer_payload.get("items")
    return items if isinstance(items, list) else []


def _label_for(item) -> str | None:
    """Best human-readable label across all four sources."""
    for k in ("label", "name", "callsign", "flight", "registration"):
        v = item.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _flag_for(item) -> str | None:
    """Two-letter country code if we have it; else None. boats/military_naval
    use `country_code`, planes use `country_code` too in the normalized feed."""
    cc = item.get("country_code") or item.get("flag_cc")
    if isinstance(cc, str) and len(cc) == 2:
        return cc.upper()
    return None


def _entity_key(source: str, eid) -> str | None:
    if eid is None:
        return None
    return f"{source}:{eid}"


def _pack_sample(now_ms: int, lat: float, lng: float,
                 speed=None, heading=None, alt=None) -> list:
    """Quantize a sample to a compact tuple. lat/lng to 4dp (~11m at the
    equator — plenty for sparkline replay), speed/heading to 1dp. We pick ONE
    secondary metric per entity kind: flights carry altitude, vessels carry
    speed+heading. Skipping a metric just shortens the tuple."""
    base = [now_ms, int(round(lat * 1e4)), int(round(lng * 1e4))]
    if alt is not None:
        base.append(int(round(alt)))
    elif speed is not None or heading is not None:
        base.append(int(round((speed or 0) * 10)))
        base.append(int(round((heading or 0) * 10)))
    return base


def _observe(item, source: str) -> dict | None:
    """Normalize one upstream item to the {key, meta, sample} shape we store."""
    lat, lng = item.get("lat"), item.get("lng")
    eid = item.get("id")
    if lat is None or lng is None or eid is None:
        return None
    try:
        lat_f = float(lat)
        lng_f = float(lng)
    except (TypeError, ValueError):
        return None
    key = _entity_key(source, str(eid))
    if not key:
        return None
    kind = SOURCE_KIND[source]
    meta = {
        "label": _label_for(item) or str(eid),
        "source": source,
        "kind": kind,
        "flag": _flag_for(item),
    }
    return {
        "key": key, "meta": meta,
        "lat": lat_f, "lng": lng_f,
        "alt": item.get("altitude") if kind == "flight" else None,
        "speed": item.get("speed"),
        "heading": (item.get("heading") if kind == "vessel"
                    else item.get("track")),
    }


def _collect_observations(layers) -> dict[str, dict]:
    """Walk all four sources, return {key: normalized_observation}. Skips
    sources missing from the payload (collector is per-layer-isolated, any
    single source may be empty mid-cycle)."""
    obs: dict[str, dict] = {}
    for src in TRAIL_SOURCES:
        for item in _items(layers.get(src)):
            rec = _observe(item, src)
            if rec is not None:
                obs[rec["key"]] = rec
    return obs


# ---------------------------------------------------------------------------
# State shape
# ---------------------------------------------------------------------------
# state = {
#   "tracks": OrderedDict[key, {"meta": {...}, "last_ts": float,
#                                "samples": deque[list]}],
# }
# OrderedDict is keyed by last-touched order (LRU): every observation move-to-
# end so eviction picks the oldest-touched. Pure-dict-ish so json-roundtrip
# works in tests (we coerce back to OrderedDict on entry).


def _state_in(prev_state, max_points):
    """Coerce a fresh / prior state into our working shape."""
    if not prev_state or not isinstance(prev_state, dict):
        return {"tracks": OrderedDict()}
    raw = prev_state.get("tracks") or {}
    tracks: "OrderedDict[str, dict]" = OrderedDict()
    for key, rec in raw.items():
        if not isinstance(rec, dict):
            continue
        samples = rec.get("samples") or []
        # Accept either list (JSON-rehydrated from blob) or deque (in-process
        # carry-over). Reject anything else.
        if not isinstance(samples, (list, deque)):
            samples = []
        dq: deque = deque(samples, maxlen=max_points)
        tracks[key] = {
            "meta": rec.get("meta") or {},
            "last_ts": float(rec.get("last_ts") or 0.0),
            "samples": dq,
        }
    return {"tracks": tracks}


def record_trails(layers, prev_state, *, now=None,
                  max_entities: int = DEFAULT_MAX_ENTITIES,
                  max_points_per_entity: int = DEFAULT_MAX_POINTS,
                  window_hours: int = DEFAULT_WINDOW_HOURS,
                  blob_budget_bytes: int = BLOB_SIZE_BUDGET_BYTES,
                  ) -> tuple[dict, dict]:
    """Append one sample per observed entity to a rolling per-entity track.

    Returns (publish_payload, new_state). The state is opaque — only this
    module reads/writes it — and is meant to live in the collector's `state`
    dict across cycles (mirrors intel.append_history's contract). Failure
    to find a source key in `layers` is a no-op for that source (collector
    isolation: any feed may be transiently empty).

    Caps:
      - max_points_per_entity: bounded deque (oldest auto-evicted on append).
      - window_hours: any entity not seen in > window_hours is dropped.
      - max_entities: only the N most-recently-touched are kept.
      - blob_budget_bytes: if the serialized payload exceeds budget, per-entity
        sample counts are trimmed in lockstep until it fits.
    """
    now = now or _now()
    now_ms = int(now.timestamp() * 1000)
    state = _state_in(prev_state, max_points_per_entity)
    tracks = state["tracks"]

    obs = _collect_observations(layers)

    # 1) Append every new observation, refresh meta + LRU order.
    for key, rec in obs.items():
        sample = _pack_sample(
            now_ms, rec["lat"], rec["lng"],
            speed=rec.get("speed"), heading=rec.get("heading"),
            alt=rec.get("alt"))
        if key in tracks:
            tracks[key]["samples"].append(sample)
            tracks[key]["meta"] = rec["meta"]
            tracks[key]["last_ts"] = now.timestamp()
            tracks.move_to_end(key)
        else:
            dq: deque = deque(maxlen=max_points_per_entity)
            dq.append(sample)
            tracks[key] = {
                "meta": rec["meta"],
                "last_ts": now.timestamp(),
                "samples": dq,
            }
            tracks.move_to_end(key)

    # 2) Drop stale entities (not seen in > window_hours).
    stale_cutoff = (now - timedelta(hours=window_hours)).timestamp()
    stale_keys = [k for k, r in tracks.items() if r["last_ts"] < stale_cutoff]
    for k in stale_keys:
        del tracks[k]

    # 3) Cap to top-N most-recently-touched (OrderedDict insertion order ==
    #    LRU since we move_to_end on every observation; oldest is at the head).
    while len(tracks) > max_entities:
        tracks.popitem(last=False)

    # 4) Build the publish blob. Most-recent-touched first so the largest
    #    /freshest entities sit at the top of the JSON for predictable parse
    #    order on the client.
    entities: "OrderedDict[str, dict]" = OrderedDict()
    # Reverse so the freshest entities come out first.
    for key in reversed(tracks):
        rec = tracks[key]
        meta = rec["meta"]
        entities[key] = {
            "label": meta.get("label") or key.split(":", 1)[-1],
            "source": meta.get("source"),
            "kind": meta.get("kind"),
            "flag": meta.get("flag"),
            "samples": list(rec["samples"]),
        }

    payload = {
        "updatedAt": _iso(now),
        "windowHours": window_hours,
        "sampledAt": _iso(now),
        "count": len(entities),
        "entities": entities,
    }

    # 5) Size guard — if we're over budget, halve each entity's sample
    #    history until it fits or until we've trimmed everyone to a single
    #    sample. Encoding is cheap relative to the blob upload, so doing
    #    this in a loop is fine.
    payload = _enforce_blob_budget(payload, blob_budget_bytes)

    return payload, state


def _enforce_blob_budget(payload: dict, budget: int) -> dict:
    """Trim per-entity sample counts in halves until serialized < budget.
    Mutates the payload's entity sample lists in place; returns same payload
    for chaining. Worst case (everyone trimmed to 1 sample) is a sub-200KB
    blob even at 2000 entities."""
    encoded = json.dumps(payload, separators=(",", ":"))
    if len(encoded) <= budget:
        return payload
    entities = payload["entities"]
    # We keep halving the MAX allowed per entity until it fits or we hit 1.
    cap = max((len(e.get("samples", [])) for e in entities.values()), default=0)
    while cap > 1 and len(encoded) > budget:
        cap = max(1, cap // 2)
        for ent in entities.values():
            samples = ent.get("samples") or []
            if len(samples) > cap:
                # Keep the most recent `cap` samples — recency wins.
                ent["samples"] = samples[-cap:]
        encoded = json.dumps(payload, separators=(",", ":"))
    return payload
