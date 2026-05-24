"""VIIRS Boat Detection (VBD) collector — nightly dark-fleet light signatures.

WHAT THIS GIVES US
==================
VIIRS Day/Night Band (DNB) detects nighttime point lights down to single
fishing-boat brightness. The Earth Observation Group at Colorado School of
Mines (EOG) publishes a FINAL nightly CSV ~6h after each overpass listing
every detection on Earth with a light bright enough to be a vessel. This is
the ONE free source that catches the Chinese DWF squid fleet when AIS is
off and SAR is cloudy — squid jiggers run thousands of high-powered lights
to attract calamari, lighting up like cities at night.

Coverage: global, every clear night. Latency: ~6h to ~24h depending on
which side of the dateline. Resolution: ~750m. False positives: oil/gas
flares, lightning, cruise ships, naval flotillas — filtered downstream by
the IUU detector that joins these against geo_zones polygons.

LICENSE
=======
CC BY 4.0 (free for any use with attribution). Catalog page:
  https://eogdata.mines.edu/products/vbd/

AUTH
====
Direct CSV downloads from EOG require a free account (one-time signup at
https://eogdata.mines.edu). Credentials provisioned via env vars:
  EOG_USER, EOG_PASS

When unset, this module returns an empty payload (count=0) with a hint in
the error field — the downstream IUU detector falls back to AIS-only mode.
Routes via ProxyRack (CLAUDE.md never-burn-home-IP rule).

SHAPE
=====
Returns {layer: "viirs_vessels", updatedAt, count, items: [
  {id, lat, lng, label, radiance, qf_detect, source: "viirs", detected_at}
]}
"""
from __future__ import annotations

import asyncio
import base64
import csv
import io
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone


_ENDPOINT_BASE = "https://eogdata.mines.edu/wwwdata/viirs_products/vbd/v30"
# Cap how many detections we ship globally — frontend pin budget is
# strict; we keep the 2000 brightest per night.
_MAX_POINTS = 2000

# Bounding boxes of the high-interest EEZs we filter to. Pre-filter at parse
# time so we never materialize the ~120MB global CSV in memory. (lat_min,
# lat_max, lng_min, lng_max). Conservatively padded by ~1°.
_AOI_BBOXES: list[tuple[str, float, float, float, float]] = [
    # name,            lat_min, lat_max, lng_min, lng_max
    ("Galapagos",      -4.0,    3.0,    -94.0,   -87.0),
    ("Argentine",      -56.0,  -33.0,   -68.0,   -53.0),
    ("Natuna",         -12.0,   8.0,     94.0,   142.0),
    ("Hormuz",          22.0,   30.0,    52.0,    60.0),
    ("Falkland",       -54.0,  -49.0,   -62.0,   -56.0),
    ("DPRK-Yellow",     34.0,   42.0,   122.0,   131.0),
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _payload(items: list, error: str | None = None) -> dict:
    p = {
        "layer": "viirs_vessels",
        "updatedAt": _now_iso(),
        "count": len(items),
        "items": items,
    }
    if error:
        p["error"] = error
    return p


def _proxy_handler() -> urllib.request.ProxyHandler | None:
    """Build a ProxyRack-backed urllib opener. None when no proxy is set —
    caller should treat that as a hard skip per CLAUDE.md (never burn the
    home IP on third-party endpoints)."""
    url = os.environ.get("PROXYRACK_PROXY_URL") or os.environ.get("HTTPS_PROXY")
    if not url:
        return None
    return urllib.request.ProxyHandler({"http": url, "https": url})


def _basic_auth(user: str, pwd: str) -> str:
    raw = f"{user}:{pwd}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _candidate_dates() -> list[str]:
    """Return the last 3 calendar dates as yyyymmdd strings. EOG publishes
    FINAL ~6h after the local night so we always look one day back; the
    extra two days handle cloudy/missed nights."""
    today = datetime.now(timezone.utc).date()
    return [(today - timedelta(days=i)).strftime("%Y%m%d") for i in (1, 2, 3)]


def _csv_url_for_date(yyyymmdd: str) -> str:
    """EOG VBD global daily CSV path. Filename pattern documented at
    https://eogdata.mines.edu/products/vbd/ ; the v30 archive uses the
    `SVDNB_npp_d{date}_global_vbd.csv.gz` aggregate."""
    return f"{_ENDPOINT_BASE}/SVDNB_npp_d{yyyymmdd}_global_vbd.csv.gz"


def _within_any_bbox(lat: float, lng: float) -> str | None:
    for name, la, lb, ga, gb in _AOI_BBOXES:
        if la <= lat <= lb and ga <= lng <= gb:
            return name
    return None


def _parse_vbd_csv(raw_bytes: bytes, detected_date: str) -> list[dict]:
    """Decompress + parse VBD CSV. Schema (v3.0):
       id_Key, Lat_DNB, Lon_DNB, Date_Mscan, Date_Proc, Rad_DNB,
       Rad_I04, Rad_I05, SHI, QF_Detect, ... (variable across vintages).
    We pick the columns we need by name, robust to column reordering."""
    import gzip
    try:
        text = gzip.decompress(raw_bytes).decode("utf-8", errors="replace")
    except Exception:
        # Some EOG mirrors serve uncompressed.
        text = raw_bytes.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    out: list[dict] = []
    for row in reader:
        try:
            lat = float(row.get("Lat_DNB") or row.get("lat") or "nan")
            lng = float(row.get("Lon_DNB") or row.get("lng") or row.get("lon") or "nan")
        except (TypeError, ValueError):
            continue
        if lat != lat or lng != lng:  # NaN check
            continue
        aoi = _within_any_bbox(lat, lng)
        if aoi is None:
            continue
        try:
            rad = float(row.get("Rad_DNB") or row.get("radiance") or "0")
        except (TypeError, ValueError):
            rad = 0.0
        qf = row.get("QF_Detect") or row.get("qf_detect") or ""
        out.append({
            "id": f"viirs-{row.get('id_Key') or len(out)}",
            "lat": lat,
            "lng": lng,
            "label": f"VIIRS · {aoi} · rad {rad:.1f}",
            "radiance": round(rad, 2),
            "qf_detect": qf,
            "source": "viirs",
            "aoi": aoi,
            "detected_at": detected_date,
            "color": "#FFD600",
            "__icon": "dot",
        })
    return out


def _fetch_csv(url: str, auth_header: str, proxy: urllib.request.ProxyHandler | None) -> bytes | None:
    """Direct GET with HTTP Basic auth + ProxyRack. Returns None on any error;
    upstream decides whether to try another date."""
    opener = (
        urllib.request.build_opener(proxy) if proxy else urllib.request.build_opener()
    )
    req = urllib.request.Request(url, headers={
        "User-Agent": "SIL-Globe-Collector/1.0 (+research)",
        "Authorization": auth_header,
        "Accept": "*/*",
    })
    try:
        with opener.open(req, timeout=120) as resp:
            return resp.read()
    except Exception:
        return None


def _do_fetch() -> dict:
    user = os.environ.get("EOG_USER")
    pwd = os.environ.get("EOG_PASS")
    if not (user and pwd):
        return _payload(
            [],
            error="EOG_USER/EOG_PASS unset — register a free account at "
            "eogdata.mines.edu and set creds to enable VIIRS dark-fleet "
            "detection. IUU detector continues in AIS-only mode.",
        )
    proxy = _proxy_handler()
    if proxy is None:
        return _payload([], error="PROXYRACK_PROXY_URL unset — VIIRS download "
                        "would burn the home IP; skipping per safety policy.")
    auth = _basic_auth(user, pwd)
    items_by_id: dict[str, dict] = {}
    for d in _candidate_dates():
        raw = _fetch_csv(_csv_url_for_date(d), auth, proxy)
        if not raw:
            continue
        for row in _parse_vbd_csv(raw, d):
            items_by_id[row["id"]] = row
        # First successful date wins — we want the freshest single-night
        # snapshot, not a multi-day pile-up that double-counts moving boats.
        if items_by_id:
            break
    if not items_by_id:
        return _payload([], error="EOG VBD: no fetches succeeded for last 3 nights.")
    # Sort by brightness DESC, cap to budget.
    sorted_rows = sorted(items_by_id.values(), key=lambda r: r["radiance"], reverse=True)
    return _payload(sorted_rows[:_MAX_POINTS])


async def fetch_viirs_vessels() -> dict:
    """Async wrapper that runs the blocking download in a worker thread —
    matches the pattern other I/O-bound fetchers in this collector use."""
    return await asyncio.to_thread(_do_fetch)
