"""Cloudflare Radar BGP — leak + hijack events for the kinetic detector.

WHAT THIS GIVES US
==================
Cloudflare Radar aggregates BGP route announcements from Cloudflare's global
network + RouteViews + RIPE RIS. They publish two event streams:

  • /radar/bgp/leaks/events   — improper route propagation (often misconfig,
                                sometimes intentional traffic-redirection)
  • /radar/bgp/hijacks/events — origin-AS prefix takeovers (often malicious,
                                e.g. state-sponsored traffic interception)

These are the early-warning signals of C4ISR disruption — an attacker
manipulating BGP to redirect target-country traffic for surveillance or DoS
before kinetic action. Doctrinally, BGP anomalies cluster with announce-
ments of internet shutdowns, comms blackouts, and CCTV-going-dark.

COVERAGE / CADENCE
==================
Both endpoints are global, refreshed continuously. Default page returns the
most recent ~25 events. We pull per_page=50 to catch a 1-hour window.

AUTH
====
Cloudflare API token (User API Token, not Origin Service Auth). Env var
`CLOUDFLARE_API_TOKEN` (already set in the collector plist). When unset,
returns empty payload — downstream comms_blackout signature no-ops.

NO PROXY NEEDED
===============
Cloudflare's own API is global, doesn't IP-block or geo-fence, and is the
preferred path. We hit it direct (this is a Cloudflare service responding
to a Cloudflare-issued token).

SHAPE
=====
Returns {layer: "bgp_events", updatedAt, count, items: [
  {
    id, kind: "leak" | "hijack",
    title, summary, severity ("info" | "warning"),
    lat, lng,                 # populated from country_code centroid
    country_code,             # primary country affected (first in 'countries' list)
    start_ts, max_ts,         # ISO timestamps
    asn_origin, asn_offender, # the ASN that originated correctly + the offender
    asn_org_offender,         # human-readable org name
    msg_count, peer_asns,     # corroborating breadth
    is_stale,
  }
]}
"""
from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timezone

# Country centroid lookup. country.py exposes _COUNTRY_BBOX as
# (ISO2, name, lat_min, lat_max, lng_min, lng_max, clat, clng) tuples;
# we just flatten its centroid fields. Per-country events get pinned at the
# country centroid for globe display — comms_blackout collapses to one pin
# per country per cycle anyway in the detector.
try:
    from country import _COUNTRY_BBOX  # type: ignore
    COUNTRY_CENTROIDS = {row[0]: (row[6], row[7]) for row in _COUNTRY_BBOX
                         if isinstance(row, (list, tuple)) and len(row) >= 8}
except ImportError:
    COUNTRY_CENTROIDS = {}

_BASE = "https://api.cloudflare.com/client/v4/radar/bgp"
_PER_PAGE = 50  # ~1h of events at typical rate


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _payload(items: list, error: str | None = None) -> dict:
    p = {
        "layer": "bgp_events",
        "updatedAt": _now_iso(),
        "count": len(items),
        "items": items,
    }
    if error:
        p["error"] = error
    return p


def _country_centroid(cc: str) -> tuple[float | None, float | None]:
    """Look up (lat, lng) centroid for an ISO-2 code. Returns (None, None)
    if unknown — caller should drop the item rather than pin at (0, 0)."""
    if not cc:
        return None, None
    cc = cc.upper()
    c = COUNTRY_CENTROIDS.get(cc)
    if isinstance(c, (list, tuple)) and len(c) >= 2:
        return c[0], c[1]
    return None, None


def _fetch_events(kind: str, token: str) -> tuple[list[dict], dict[int, dict]]:
    """kind: 'leaks' or 'hijacks'. Returns (events, asn_info_by_asn)."""
    url = f"{_BASE}/{kind}/events?per_page={_PER_PAGE}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read())
    if not data.get("success"):
        return [], {}
    result = data.get("result") or {}
    asn_info = {a["asn"]: a for a in (result.get("asn_info") or []) if isinstance(a, dict) and "asn" in a}
    events = result.get("events") or []
    return events, asn_info


def _flatten_leak(ev: dict, asn_info: dict[int, dict]) -> dict | None:
    """Leak shape: {leak_seg_path, origin_asn, leak_asn, leaked_to_asns,
    leaked_prefix_count, peers_asns, countries, start_time, ...}"""
    countries = ev.get("countries") or []
    cc = countries[0] if countries else ""
    lat, lng = _country_centroid(cc)
    if lat is None or lng is None:
        return None
    leak_asn = ev.get("leak_asn") or ev.get("leaker_asn")
    leak_org = (asn_info.get(leak_asn) or {}).get("org_name", f"AS{leak_asn}")
    origin_asn = ev.get("origin_asn")
    origin_org = (asn_info.get(origin_asn) or {}).get("org_name", f"AS{origin_asn}") if origin_asn else "?"
    prefix_count = ev.get("leaked_prefix_count") or ev.get("leaked_prefixes_count") or 0
    return {
        "id": f"bgp-leak-{ev.get('id')}",
        "kind": "leak",
        "title": f"BGP route leak by {leak_org}",
        "summary": (f"AS{leak_asn} ({leak_org}) leaked {prefix_count} prefix(es) "
                    f"originally announced by AS{origin_asn} ({origin_org})"),
        "severity": "warning" if prefix_count >= 10 else "info",
        "lat": lat, "lng": lng,
        "country_code": cc,
        "start_ts": ev.get("min_leak_ts") or ev.get("start_time"),
        "max_ts": ev.get("max_leak_ts") or ev.get("max_msg_ts"),
        "asn_origin": origin_asn,
        "asn_offender": leak_asn,
        "asn_org_offender": leak_org,
        "msg_count": ev.get("leak_msgs_count") or ev.get("msg_count") or 0,
        "peer_asns": ev.get("peers_asns") or ev.get("peer_asns") or [],
        "is_stale": bool(ev.get("is_stale")),
    }


def _flatten_hijack(ev: dict, asn_info: dict[int, dict]) -> dict | None:
    """Hijack shape: {hijacker_asn, hijacker_country, hijack_msgs_count,
    duration, max_hijack_ts, peer_asns, ...}"""
    hijacker_asn = ev.get("hijacker_asn")
    cc = ev.get("hijacker_country") or ""
    lat, lng = _country_centroid(cc)
    if lat is None or lng is None:
        return None
    hijacker_org = (asn_info.get(hijacker_asn) or {}).get("org_name", f"AS{hijacker_asn}")
    msg_count = ev.get("hijack_msgs_count") or 0
    return {
        "id": f"bgp-hijack-{ev.get('id')}",
        "kind": "hijack",
        "title": f"BGP hijack by {hijacker_org}",
        "summary": (f"AS{hijacker_asn} ({hijacker_org}) originated prefixes it does "
                    f"not own — {msg_count} hijack announcements over {ev.get('duration') or 0}s"),
        "severity": "warning",  # all hijacks tier 2 by default
        "lat": lat, "lng": lng,
        "country_code": cc,
        "start_ts": ev.get("min_hijack_ts"),
        "max_ts": ev.get("max_hijack_ts"),
        "asn_origin": None,  # by definition no legitimate origin for a hijack
        "asn_offender": hijacker_asn,
        "asn_org_offender": hijacker_org,
        "msg_count": msg_count,
        "peer_asns": ev.get("peer_asns") or [],
        "is_stale": bool(ev.get("is_stale")),
    }


def do_fetch() -> dict:
    """Public entry — fetch both leak and hijack streams, flatten."""
    # Match the plist's existing CLOUDFLARE_RADAR_TOKEN naming; fall back to
    # CLOUDFLARE_API_TOKEN for general Cloudflare API tokens.
    token = os.environ.get("CLOUDFLARE_RADAR_TOKEN") or os.environ.get("CLOUDFLARE_API_TOKEN")
    if not token:
        return _payload([], error="CLOUDFLARE_RADAR_TOKEN / CLOUDFLARE_API_TOKEN not set")
    items: list[dict] = []
    try:
        leaks, leak_asn_info = _fetch_events("leaks", token)
        for ev in leaks:
            item = _flatten_leak(ev, leak_asn_info)
            if item:
                items.append(item)
    except Exception as e:
        return _payload(items, error=f"leaks fetch failed: {e}")
    try:
        hijacks, hijack_asn_info = _fetch_events("hijacks", token)
        for ev in hijacks:
            item = _flatten_hijack(ev, hijack_asn_info)
            if item:
                items.append(item)
    except Exception as e:
        return _payload(items, error=f"hijacks fetch failed: {e}")
    # Most-recent first
    items.sort(key=lambda x: (x.get("max_ts") or ""), reverse=True)
    return _payload(items)


async def fetch_bgp_events() -> dict:
    """Async wrapper for collector.py integration — matches the pattern
    of viirs_vbd.fetch_viirs_vessels (asyncio.to_thread over a sync I/O fn)."""
    import asyncio
    return await asyncio.to_thread(do_fetch)


if __name__ == "__main__":
    p = do_fetch()
    print(json.dumps({k: v for k, v in p.items() if k != "items"}, indent=2))
    print(f"items: {len(p.get('items', []))}")
    for it in (p.get("items") or [])[:3]:
        print(f"  {it['kind']:7} {it['country_code']:3} AS{it.get('asn_offender'):>6} {it.get('msg_count'):>4} msgs  {it['title'][:60]}")
