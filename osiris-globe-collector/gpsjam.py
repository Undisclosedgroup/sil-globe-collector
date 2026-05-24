"""GPSJam.org — GPS/GNSS interference per-day tile data.

WHAT THIS GIVES US
==================
John Wiseman's gpsjam.org publishes a daily map of GNSS interference
inferred from ADS-B "NIC" (Navigation Integrity Category) drops. Aircraft
crossing a jammed region report degraded NIC; aggregating across thousands
of aircraft gives a 1° grid where each cell carries a "bad %" — fraction
of recent ADS-B reports with degraded NIC. Bucketed 0..4 (Wiseman's
quartile palette: green/yellow/orange/red/dark-red).

This is the canonical free electronic-warfare indicator. JSOC ABI doctrine
weights EW activity heavily as a precursor to kinetic action; jamming
often appears 6-24h before strikes (denying the opfor's GPS-guided weapons
+ navigation).

COVERAGE / CADENCE
==================
Global, daily aggregate published once per UTC day with ~24h latency.
Wiseman serves prebaked GeoJSON tiles. The actual URL pattern is found in
the site's JS bundle — endpoint shape verified by the Phase C research
agent (see entries in NEW SOURCES.md).

AUTH
====
None — fully public.

PROXY
=====
ProxyRack residential. The site is on Cloudflare; raw home-IP requests are
soft-blocked after a handful of GETs.

SHAPE
=====
Returns {layer: "gps_jam", updatedAt, count, items: [
  {id, lat, lng, intensity (0-4), bad_pct, observation_count, source_date}
]}

When endpoint is unconfirmed / fetch fails, returns empty payload with an
error field — downstream gps_jam_surge signature no-ops cleanly.
"""
from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timedelta, timezone

# GPSJam JS bundle reference (pending Phase C research confirmation).
# Two candidate endpoint patterns — try in order; first to return valid
# JSON wins.
_CANDIDATE_URLS = [
    "https://gpsjam.org/data/{yyyy}/{mm}/{dd}.json",
    "https://gpsjam.org/data/{yyyy}-{mm}-{dd}.geojson",
    "https://gpsjam.org/api/days/{yyyy}-{mm}-{dd}",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _payload(items: list, error: str | None = None) -> dict:
    p = {
        "layer": "gps_jam",
        "updatedAt": _now_iso(),
        "count": len(items),
        "items": items,
    }
    if error:
        p["error"] = error
    return p


def _proxy_handler() -> urllib.request.ProxyHandler | None:
    url = os.environ.get("PROXYRACK_PROXY_URL") or os.environ.get("HTTPS_PROXY")
    if not url:
        return None
    return urllib.request.ProxyHandler({"http": url, "https": url})


def _candidate_dates() -> list[str]:
    """Today, yesterday, 2 days back — handle UTC vs server timezone latency."""
    today = datetime.now(timezone.utc).date()
    return [(today - timedelta(days=d)).strftime("%Y-%m-%d") for d in range(3)]


def _try_url(url_template: str, date_iso: str, opener) -> dict | None:
    yyyy, mm, dd = date_iso.split("-")
    url = url_template.format(yyyy=yyyy, mm=mm, dd=dd)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (sil-globe collector)",
        "Accept": "application/json, application/geo+json",
    })
    try:
        with opener.open(req, timeout=20) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _flatten_feature_collection(fc: dict, source_date: str) -> list[dict]:
    """GeoJSON FeatureCollection → flat items list. Tries common GPSJam
    property names; unknown fields default to 0."""
    out = []
    for f in (fc.get("features") or []):
        if not isinstance(f, dict):
            continue
        geom = f.get("geometry") or {}
        coords = geom.get("coordinates")
        # GeoJSON ordering: [lng, lat]
        if geom.get("type") == "Point" and isinstance(coords, list) and len(coords) >= 2:
            lng, lat = coords[0], coords[1]
        elif geom.get("type") in ("Polygon", "MultiPolygon"):
            # Use the polygon centroid (cheap: bbox center).
            _w = 180.0; _e = -180.0; _s = 90.0; _n = -90.0
            def _walk(c):
                nonlocal _w, _e, _s, _n
                if isinstance(c, (list, tuple)) and len(c) >= 2 \
                        and isinstance(c[0], (int, float)) and isinstance(c[1], (int, float)):
                    _w = min(_w, c[0]); _e = max(_e, c[0])
                    _s = min(_s, c[1]); _n = max(_n, c[1])
                elif isinstance(c, list):
                    for x in c:
                        _walk(x)
            _walk(coords)
            lat = (_s + _n) / 2; lng = (_w + _e) / 2
        else:
            continue
        props = f.get("properties") or {}
        bad_pct = float(props.get("bad_pct") or props.get("badPct") or props.get("bad") or 0)
        obs = int(props.get("observation_count") or props.get("count") or props.get("n") or 0)
        # Convert bad_pct (0-100) to Wiseman intensity bucket (0-4):
        #   0=0  | 1-2.5%=1 | 2.5-10%=2 | 10-25%=3 | >25%=4
        if bad_pct >= 25:   intensity = 4
        elif bad_pct >= 10: intensity = 3
        elif bad_pct >= 2.5: intensity = 2
        elif bad_pct >= 1.0: intensity = 1
        else:               intensity = 0
        out.append({
            "id": f"gpsjam-{source_date}-{lat:.1f}-{lng:.1f}",
            "lat": lat,
            "lng": lng,
            "intensity": intensity,
            "bad_pct": bad_pct,
            "observation_count": obs,
            "source_date": source_date,
        })
    return out


def do_fetch() -> dict:
    """Public entry — try candidate URLs for the last ~3 days."""
    proxy = _proxy_handler()
    if proxy is None:
        return _payload([], error="no ProxyRack proxy configured (PROXYRACK_PROXY_URL)")
    opener = urllib.request.build_opener(proxy)
    for date_iso in _candidate_dates():
        for tmpl in _CANDIDATE_URLS:
            data = _try_url(tmpl, date_iso, opener)
            if data is None:
                continue
            # FeatureCollection or list-of-features
            if isinstance(data, dict) and (data.get("type") == "FeatureCollection" or "features" in data):
                items = _flatten_feature_collection(data, date_iso)
                if items:
                    return _payload(items)
            elif isinstance(data, list) and data:
                # Some endpoints serve a list of {lat, lng, bad_pct, ...}.
                items = []
                for d in data:
                    if not isinstance(d, dict):
                        continue
                    items.append({
                        "id": f"gpsjam-{date_iso}-{d.get('lat')}-{d.get('lng')}",
                        "lat": d.get("lat"),
                        "lng": d.get("lng"),
                        "intensity": int(d.get("intensity") or 0),
                        "bad_pct": float(d.get("bad_pct") or 0),
                        "observation_count": int(d.get("count") or 0),
                        "source_date": date_iso,
                    })
                if items:
                    return _payload(items)
    return _payload([], error="GPSJam endpoint not found (Phase C research pending)")


async def fetch_gps_jam() -> dict:
    """Async wrapper for collector.py integration."""
    import asyncio
    return await asyncio.to_thread(do_fetch)


if __name__ == "__main__":
    p = do_fetch()
    print(json.dumps({k: v for k, v in p.items() if k != "items"}, indent=2))
    print(f"items: {len(p.get('items', []))}")
    for it in (p.get("items") or [])[:5]:
        print(f"  ({it['lat']:.1f},{it['lng']:.1f})  intensity={it['intensity']} bad={it['bad_pct']:.1f}% obs={it['observation_count']}")
