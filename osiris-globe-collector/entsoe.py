"""ENTSO-E Transparency Platform — EU power-grid unavailability.

WHAT THIS GIVES US
==================
ENTSO-E (European Network of Transmission System Operators for Electricity)
publishes near-real-time data on generation outages, transmission
unavailability, and grid load. The kinetic detector uses this as a
corroborator + a false-positive SUPPRESSOR:

  - Corroborator: simultaneous EU grid outage + comms_blackout + CCTV
    dark = high-confidence infrastructure-attack signature.
  - Suppressor: CCTV dark in a country + a confirmed scheduled outage =
    likely not kinetic, just maintenance.

JSOC ABI maps grid status to I-6 (C2 / Communications) and I-9 (Critical
Infrastructure).

AUTH
====
ENTSO-E requires a free "security token" obtained via email to
transparency@entsoe.eu. Env var: `ENTSOE_API_TOKEN`. When unset, returns
empty payload — downstream signatures fall back to other corroborators.

COVERAGE / CADENCE
==================
EU + adjacent (UK, NO, CH). ENTSO-E publishes outages as they're declared;
the API supports filtering by documentType + a periodStart/periodEnd.
We pull a 1-hour rolling window per cycle (~15-min cadence).

ENDPOINT
========
Base: https://web-api.tp.entsoe.eu/api
documentType=A77 → Production unavailability
documentType=A78 → Transmission unavailability (more JSOC-relevant — grid path failures)
documentType=A80 → Generation unit / production unit details

NOTE: pending Phase C research agent's confirmation of the exact
documentType. Current default is A78 (transmission), which is the
right "is the grid degraded right now?" signal. Falls back to A77
if A78 returns empty.

XML
===
Responses are XML (UN/CEFACT urn:iec62325.351:tc57wg16:451-6 schema).
We use stdlib xml.etree.ElementTree — no third-party XML lib needed.

SHAPE
=====
Returns {layer: "eu_grid", updatedAt, count, items: [
  {
    id, country_code, lat, lng,                 # centroid of affected control area
    title, summary, severity ("info" | "warning"),
    unavail_mw,                                  # affected capacity in MW
    start_ts, end_ts,
    business_type,                               # A53 = planned, A54 = unplanned (the JSOC-meaningful one)
  }
]}
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

try:
    from country import _COUNTRY_BBOX  # type: ignore
    COUNTRY_CENTROIDS = {row[0]: (row[6], row[7]) for row in _COUNTRY_BBOX
                         if isinstance(row, (list, tuple)) and len(row) >= 8}
except ImportError:
    COUNTRY_CENTROIDS = {}

_BASE = "https://web-api.tp.entsoe.eu/api"

# Country code → ENTSO-E EIC area code (for documentType=A78 transmission).
# Limited initial set; covers the EU heavyweights. Expand with confirmation
# from research agent.
EIC = {
    "DE": "10Y1001A1001A83F",  # Germany
    "FR": "10YFR-RTE------C",  # France
    "GB": "10YGB----------A",  # Great Britain
    "ES": "10YES-REE------0",  # Spain
    "IT": "10YIT-GRTN-----B",  # Italy
    "NL": "10YNL----------L",  # Netherlands
    "BE": "10YBE----------2",  # Belgium
    "AT": "10YAT-APG------L",  # Austria
    "CH": "10YCH-SWISSGRIDZ",  # Switzerland
    "PL": "10YPL-AREA-----S",  # Poland
    "SE": "10YSE-1--------K",  # Sweden
    "NO": "10YNO-0--------C",  # Norway
    "DK": "10Y1001A1001A65H",  # Denmark
    "FI": "10YFI-1--------U",  # Finland
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _payload(items: list, error: str | None = None) -> dict:
    p = {
        "layer": "eu_grid",
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


def _window() -> tuple[str, str]:
    """Return (periodStart, periodEnd) in ENTSO-E's yyyyMMddHHmm UTC format
    spanning the last 60 minutes."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(minutes=60)
    fmt = "%Y%m%d%H%M"
    return start.strftime(fmt), now.strftime(fmt)


def _fetch_unavailability(country_code: str, eic: str, token: str,
                          proxy_handler, doc_type: str = "A78") -> list[dict]:
    """One country, one cycle. Returns list of unavailability items.

    documentType: A78 = transmission unavailability (preferred); A77 = production.
    """
    period_start, period_end = _window()
    params = {
        "securityToken": token,
        "documentType": doc_type,
        # For A78/A77 the relevant control area is the BiddingZone EIC.
        "biddingZone_Domain": eic,
        "periodStart": period_start,
        "periodEnd": period_end,
    }
    url = f"{_BASE}?{urllib.parse.urlencode(params)}"
    opener = urllib.request.build_opener(proxy_handler) if proxy_handler else urllib.request.build_opener()
    try:
        with opener.open(url, timeout=30) as r:
            raw = r.read()
    except Exception:
        return []
    # Parse XML — namespace is the IEC tc57wg16 schema
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    # Strip namespace from tags for easier walking
    for el in root.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    items = []
    # ENTSO-E unavailability documents are TimeSeries with a Reason + quantity.
    for ts in root.iter("TimeSeries"):
        mrid = (ts.findtext("mRID") or "").strip()
        business_type = (ts.findtext("businessType") or "").strip()  # A53=planned, A54=unplanned
        # Quantity is in points across a Period
        max_qty = 0.0
        for pt in ts.iter("Point"):
            try:
                q = float(pt.findtext("quantity") or 0)
                max_qty = max(max_qty, q)
            except (TypeError, ValueError):
                continue
        # Time window
        start_ts = (ts.findtext("Period/timeInterval/start") or "").strip()
        end_ts = (ts.findtext("Period/timeInterval/end") or "").strip()
        if not start_ts:
            continue
        clat, clng = COUNTRY_CENTROIDS.get(country_code, (None, None))
        if clat is None or clng is None:
            continue
        sev = "warning" if business_type == "A54" else "info"  # unplanned = sterner
        items.append({
            "id": f"entsoe-{country_code}-{mrid or doc_type}-{start_ts}",
            "country_code": country_code,
            "lat": clat, "lng": clng,
            "title": f"EU grid {('unplanned outage' if business_type == 'A54' else 'unavailability')} ({country_code})",
            "summary": (f"~{int(max_qty)} MW unavailable in {country_code} "
                        f"({start_ts} → {end_ts}). "
                        f"businessType={business_type}, documentType={doc_type}"),
            "severity": sev,
            "unavail_mw": int(max_qty),
            "start_ts": start_ts,
            "end_ts": end_ts,
            "business_type": business_type,
        })
    return items


def do_fetch() -> dict:
    """Public entry — pull unavailability across the EIC table."""
    token = os.environ.get("ENTSOE_API_TOKEN")
    if not token:
        return _payload([], error="ENTSOE_API_TOKEN not set (register at transparency@entsoe.eu)")
    proxy = _proxy_handler()
    items: list[dict] = []
    for cc, eic in EIC.items():
        items.extend(_fetch_unavailability(cc, eic, token, proxy, doc_type="A78"))
        # Fallback: try A77 (production) if A78 returned nothing for this country
        if not any(i["country_code"] == cc for i in items):
            items.extend(_fetch_unavailability(cc, eic, token, proxy, doc_type="A77"))
    return _payload(items)


async def fetch_eu_grid() -> dict:
    """Async wrapper for collector.py integration."""
    import asyncio
    return await asyncio.to_thread(do_fetch)


if __name__ == "__main__":
    p = do_fetch()
    print(json.dumps({k: v for k, v in p.items() if k != "items"}, indent=2))
    print(f"items: {len(p.get('items', []))}")
    for it in (p.get("items") or [])[:5]:
        print(f"  {it['country_code']}  {it['unavail_mw']:>5}MW  {it['business_type']}  {it['start_ts']} → {it['end_ts']}")
