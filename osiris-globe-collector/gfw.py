"""Global Fishing Watch (GFW) events adapter — the "free satellite AIS" channel.

GFW aggregates SATELLITE AIS (sourced commercially from Spire/ORBCOMM upstream)
and republishes derived EVENTS — port visits, vessel encounters (rendezvous in
open ocean), loitering, fishing activity — free for non-commercial use under a
registration-required API key. This module pulls recent events with lat/lng so
they render as point markers on the globe alongside our terrestrial-AIS boats.

Why events not positions: GFW's free API does NOT expose a "give me every vessel
position globally" feed (that would be commercial satellite AIS proper). What it
gives, freely, is DERIVED EVENTS — which is arguably better signal anyway:
encounters in the middle of the ocean and loitering events near contested
coastlines are exactly the maritime-intelligence tells you actually want surfaced.

Activation: set GFW_API_KEY in the collector's environment (register free at
https://globalfishingwatch.org/our-apis/). Until then this fetcher is a no-op
that publishes an empty `vessel_events` blob — the layer is safe to ship dark.

Schema (one item per event):
    { id, lat, lng, label, kind (port_visit/encounter/loitering/fishing),
      vessel, time, color }
"""
import json
import os
from datetime import datetime, timedelta, timezone
from urllib import request as _rq
from urllib.parse import urlencode
from urllib.error import URLError, HTTPError

GFW_BASE = "https://gateway.api.globalfishingwatch.org/v3"

# Free public dataset slugs (per GFW's published catalog). Each event type lives
# in its own dataset; we query them all and merge. Encounters + loitering are the
# highest-signal events for our intel use-case (rendezvous and dark-vessel
# behaviour); port visits give us terminal/operator context.
_EVENT_DATASETS = {
    "encounter":  "public-global-encounters-events:latest",
    "loitering":  "public-global-loitering-events:latest",
    "port_visit": "public-global-port-visits-c2-events:latest",
    # public-global-fishing-events is high-volume; left off by default so a free
    # key doesn't burn its rate budget on a layer most users won't toggle on.
}

# Per-event-type marker color so the layer reads at a glance.
_KIND_COLOR = {
    "encounter":  "#ff3d3d",   # rendezvous - red (highest interest)
    "loitering":  "#ffb300",   # loitering - amber
    "port_visit": "#22d3ee",   # port visit - cyan
    "fishing":    "#a3e635",   # fishing - lime
}

# Per-dataset caps weighted by signal-per-event. Encounters are rare and highest
# signal (rendezvous at sea, sanctions-evasion tell); loitering is moderately
# rare and indicates dark-vessel behaviour; port visits are very high volume but
# lower signal-per-event; fishing is highest volume and lowest per-event signal.
# The live API probe (last 10 days globally) returned ~6.7k encounters, ~106k
# loitering, ~515k port visits, ~100k fishing — these caps keep the blob bounded
# while preferring the high-signal events when the API trims.
_KIND_CAP = {
    "encounter":  300,
    "loitering":  200,
    "port_visit": 100,
    "fishing":     50,
}
# 7 days. Satellite AIS → GFW detection pipeline has multi-day latency for some
# event types (loitering needs a settled track; encounters need post-hoc trajectory
# analysis), so anything narrower silently returns 0. Live probe at 10d returned
# ~6.7k encounters, ~106k loitering, ~515k port-visits globally; 7d is the
# tightest window that reliably yields events while keeping the per-kind cap fresh.
LOOKBACK_HOURS = 24 * 7
HTTP_TIMEOUT = 30
RETRY_STATUS = {429, 502, 503, 504, 524}   # transient → backoff + retry
MAX_RETRIES = 3


def _now():
    return datetime.now(timezone.utc).isoformat()


def _payload(items, *, error=None):
    p = {"layer": "vessel_events", "updatedAt": _now(),
         "count": len(items), "items": items}
    if error:
        p["error"] = error
    return p


def _get(url, *, token, timeout=HTTP_TIMEOUT):
    """Authenticated GET against the GFW gateway with retry on transient codes.

    Returns parsed JSON or None on any unrecoverable failure (auth / 4xx other
    than the retry set / malformed body) — the caller treats None as 'no events
    this cycle' rather than crashing the collector. GFW docs explicitly call out
    429 (rate limit) and 524 (gateway timeout) as retryable; we also retry the
    other classic-transient 5xxs with exponential backoff.

    Adaptive backpressure: if the budget module says GFW is over its budget,
    return None immediately — the caller treats this exactly like a transient
    failure (no events this cycle). Counts every actual call against the quota
    tracker so the budget tier can re-evaluate."""
    try:
        from budget import _budget
        from quota import _tracker
        if _budget.should_skip("gfw"):
            return None
        _tracker.record("gfw")
    except Exception:
        pass
    import time as _t
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "osiris-globe-collector/1.0 (+gfw-vessel-events)",
    }
    last_status = None
    for attempt in range(MAX_RETRIES + 1):
        req = _rq.Request(url, headers=headers)
        try:
            with _rq.urlopen(req, timeout=timeout) as resp:
                last_status = resp.status
                if resp.status == 200:
                    return json.loads(resp.read())
                if resp.status not in RETRY_STATUS:
                    return None
        except HTTPError as e:
            last_status = e.code
            if e.code not in RETRY_STATUS:
                return None
        except (URLError, TimeoutError, ValueError, OSError):
            pass  # treat as transient
        if attempt < MAX_RETRIES:
            _t.sleep(2 ** attempt)   # 1s, 2s, 4s
    return None


def _normalize_event(raw, kind):
    """One GFW event dict → one globe point item (lat, lng, label, …). GFW event
    payloads share a common envelope: {id, type, start, end, position:{lat,lon},
    vessel:{name,flag}, ...}. We only emit items with a usable lat/lng."""
    pos = raw.get("position") or {}
    lat = pos.get("lat")
    lng = pos.get("lon") if pos.get("lon") is not None else pos.get("lng")
    if not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
        return None
    vessel = raw.get("vessel") or {}
    name = (vessel.get("name") or vessel.get("ssvid") or "vessel").strip()
    flag = (vessel.get("flag") or "").strip()
    started = raw.get("start") or raw.get("startTime")
    label_map = {"encounter": "Encounter", "loitering": "Loitering",
                 "port_visit": "Port Visit", "fishing": "Fishing"}
    sub_bits = [label_map.get(kind, kind)]
    if flag:
        sub_bits.append(flag)
    if started:
        sub_bits.append(str(started)[:16])
    return {
        "id": raw.get("id") or f"{kind}-{lat:.4f}-{lng:.4f}-{started}",
        "lat": float(lat), "lng": float(lng),
        "label": name, "name": name,
        "kind": kind, "vessel_flag": flag or None,
        "time": started,
        "sub": " · ".join(sub_bits),
        # Per GFW terms: "Attribute Global Fishing Watch in anything you publish."
        # Per-item source field surfaces the credit in the entity detail panel.
        "source": "Global Fishing Watch",
        "source_url": "https://globalfishingwatch.org",
        "color": _KIND_COLOR.get(kind, "#ff3d3d"),
    }


def fetch_vessel_events():
    """Fetch recent satellite-AIS-derived vessel events from GFW. No-op (returns
    an empty payload, no error) when GFW_API_KEY is unset. Each request is
    independently guarded — one failing dataset just contributes 0 items rather
    than killing the whole layer."""
    token = os.environ.get("GFW_API_KEY")
    if not token:
        # Clean dark mode: empty payload, no error noise — the layer stays empty
        # in the UI until a key is provisioned. This is the documented activation
        # gate; see module docstring for setup.
        return _payload([])

    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=LOOKBACK_HOURS)
    items = []
    errors = []
    for kind, dataset in _EVENT_DATASETS.items():
        cap = _KIND_CAP.get(kind, 100)
        # offset is required when limit is set (the API returns 422 otherwise —
        # this is also what the live probe confirmed).
        q = {
            "datasets[0]":  dataset,
            "start-date":   start.strftime("%Y-%m-%d"),
            "end-date":     end.strftime("%Y-%m-%d"),
            "limit":        str(cap),
            "offset":       "0",
        }
        url = f"{GFW_BASE}/events?{urlencode(q, doseq=True)}"
        body = _get(url, token=token)
        if not body:
            errors.append(kind)
            continue
        entries = body.get("entries") or body.get("events") or []
        kept = 0
        for e in entries:
            n = _normalize_event(e, kind)
            if n:
                items.append(n)
                kept += 1
            if kept >= cap:
                break

    err = None
    if errors and not items:
        err = f"all GFW datasets failed ({', '.join(errors)})"
    elif errors:
        err = f"partial: failed datasets {', '.join(errors)}"
    return _payload(items, error=err)
