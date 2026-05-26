"""Layer registry + normalizers for the OSIRIS globe collector.

Each layer in LAYERS is `{ "id", "interval_s", "fetch": async () -> payload }`.
`fetch()` returns a normalized payload dict:
    { "layer", "updatedAt" (ISO), "count", "items": [...] , "error"? }

Point layers: items are GlobePoint dicts {id, lat, lng, label, color, ...}.
frontlines.items are GeoJSON Feature[]; news/markets/cyber are record arrays.

Upstreams + parse logic are reused verbatim from the sibling osiris-* scrapers:
  earthquakes  -> USGS GeoJSON summary feeds            (osiris-earthquakes)
  flights      -> adsb.lol /v2/mil (+ adsb.fi fallback) (spec; adsb v2 schema)
  satellites   -> CelesTrak GP JSON + SGP4 propagation  (osiris-satellites)
  markets      -> Yahoo Finance chart API (Stooq fb)    (osiris-markets)
  news         -> BBC + Al Jazeera + Google News RSS     (osiris-conflict-news)
  natural-events-> NASA EONET v3                          (osiris-natural-events)
  wildfire     -> NIFC WFIGS ArcGIS (keyless)            (osiris-wildfire-hotspots)
  cyber        -> CISA KEV catalog                        (osiris-cyber-threats)
  frontlines   -> DeepStateMap history GeoJSON           (osiris-frontlines)
  cctv         -> TfL + CalTrans + 511 + CARS networks    (osiris-cctv-cameras)
  infrastructure-> OSM Overpass nuclear plants            (osiris-critical-infrastructure)

The normalizers are pure (raw -> payload) so they unit-test against fixtures.
The fetch() coroutines do the network I/O through the shared ProxyRack fetcher.
"""
import sys, json, math, re, asyncio, os, time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))
from proxy_fetcher import fetcher  # noqa: E402

# Offline country tagging (ICAO hex / registration / lat-lng -> ISO2 + name).
# Tables are built at import; every per-record lookup is O(1)/O(log n) and makes
# NO network call (safe to run on the every-cycle flights / military_air layers).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from country import (  # noqa: E402
    country_from_aircraft, country_from_latlng,
)
from gfw import fetch_vessel_events as _gfw_fetch_sync  # noqa: E402
from notams import fetch_notams as _notams_fetch_async   # noqa: E402  (already async)
from geo_zones import fetch_geo_zones as _geo_zones_fetch_async  # noqa: E402  (already async)


async def _gfw_fetch_async():
    """Async wrapper around the synchronous GFW events fetcher so it slots into
    the LAYERS registry contract. The underlying call uses stdlib urllib so we
    run it in a thread to avoid blocking the asyncio loop."""
    return await asyncio.to_thread(_gfw_fetch_sync)

# Flightradar24 real-time scraper (sibling recon-out/flightradar24/). Optional —
# the flights layer prefers FR24 for rich detail and falls back to adsb.lol.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "flightradar24"))
try:
    import scraper as fr24  # noqa: E402
    _HAS_FR24 = True
except Exception:
    _HAS_FR24 = False

F = fetcher()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _payload(layer, items, error=None):
    p = {"layer": layer, "updatedAt": _now(), "count": len(items), "items": items}
    if error:
        p["error"] = error
    return p


def _iso_from_ms(ms):
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
    except Exception:
        return None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# JSON GET helper through ProxyRack (with light retry; first proxy call can 500).
# ---------------------------------------------------------------------------
async def _aget_json(url, *, headers=None, timeout=40, tries=3):
    r = None
    for _ in range(tries):
        r = await F.aget(url, headers=headers or {}, timeout=timeout)
        if r.tier == "refused_no_proxy":
            raise RuntimeError("proxy not configured (refused_no_proxy)")
        if r.status > 0:
            break
    if r is None or r.status != 200:
        raise RuntimeError(f"GET {url} -> status {getattr(r, 'status', '?')}")
    return json.loads(r.body)


async def _aget_text(url, *, headers=None, timeout=40, tries=3):
    r = None
    for _ in range(tries):
        r = await F.aget(url, headers=headers or {}, timeout=timeout)
        if r.tier == "refused_no_proxy":
            raise RuntimeError("proxy not configured (refused_no_proxy)")
        if r.status > 0:
            break
    if r is None or r.status != 200:
        raise RuntimeError(f"GET {url} -> status {getattr(r, 'status', '?')}")
    return r.text


# ===========================================================================
# earthquakes — USGS GeoJSON summary feeds
# ===========================================================================
USGS_FEEDS = [
    "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_day.geojson",
    "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_week.geojson",
]


def normalize_earthquakes(raw):
    items = []
    for f in raw.get("features", []):
        c = (f.get("geometry") or {}).get("coordinates") or [None, None, None]
        c = (c + [None, None, None])[:3]
        p = f.get("properties") or {}
        items.append({"id": f.get("id"), "lat": c[1], "lng": c[0],
                      "label": p.get("place"), "magnitude": p.get("mag"),
                      "depth": c[2], "time": _iso_from_ms(p.get("time")),
                      "url": p.get("url"), "alert": p.get("alert"),
                      "tsunami": p.get("tsunami"), "color": "#ff5a5a"})
    return _payload("earthquakes", items)


async def fetch_earthquakes():
    seen, feats = set(), []
    for url in USGS_FEEDS:
        data = await _aget_json(url, timeout=30)
        for f in data.get("features", []):
            qid = f.get("id")
            if qid in seen:
                continue
            seen.add(qid)
            feats.append(f)
    return normalize_earthquakes({"features": feats})


# ===========================================================================
# flights — adsb.lol radius tiles unioned for broad commercial coverage.
# We tile 250 nm-radius point queries over major world airports/regions and
# union the `ac[]` arrays, deduped by hex. This yields several thousand unique
# aircraft per cycle (vs ~170 from /v2/mil). adsb.fi mirror is the fallback.
# Both return an `ac` array of aircraft with lat/lon/alt_baro/hex/flight/t/r.
# adsbexchange-compatible v2 schema. Keyless, unlimited — no proxy-burn issues
# and no anonymous rate limits (unlike OpenSky /states/all).
# ===========================================================================
# (lat, lon) centers — ~250 nm radius each, spread across major air-traffic hubs.
# ~50 tiles covering N. America, Europe, Asia, Mid-East, Oceania, S. America,
# Africa. Fetched concurrently and unioned/deduped by hex => several thousand
# unique aircraft per cycle.
FLIGHT_TILES = [
    # North America
    (40.6413, -73.7781),   # JFK
    (42.3656, -71.0096),   # BOS
    (33.9416, -118.4085),  # LAX
    (47.4502, -122.3088),  # SEA
    (33.4342, -112.0116),  # PHX
    (39.8561, -104.6737),  # DEN
    (41.9742, -87.9073),   # ORD
    (32.8998, -97.0403),   # DFW
    (33.6407, -84.4277),   # ATL
    (25.7959, -80.2870),   # MIA
    (43.6777, -79.6248),   # YYZ
    (19.4363, -99.0721),   # MEX
    # Europe
    (51.4700, -0.4543),    # LHR
    (50.0379, 8.5622),     # FRA
    (49.0097, 2.5479),     # CDG
    (52.3105, 4.7683),     # AMS
    (40.4719, -3.5626),    # MAD
    (41.2974, 2.0833),     # BCN
    (48.3538, 11.7861),    # MUC
    (47.4647, 8.5492),     # ZRH
    (48.1103, 16.5697),    # VIE
    (59.6519, 17.9186),    # ARN
    (55.6180, 12.6508),    # CPH
    (52.1657, 20.9671),    # WAW
    (37.9364, 23.9445),    # ATH
    (41.2753, 28.7519),    # IST
    (45.6306, 9.7000),     # MXP (N. Italy)
    # Middle East
    (25.2532, 55.3657),    # DXB
    (32.0114, 34.8867),    # TLV
    (25.2731, 51.6080),    # DOH
    (24.9576, 46.6988),    # RUH
    (21.6796, 39.1565),    # JED
    # Asia
    (28.5562, 77.1000),    # DEL
    (19.0896, 72.8656),    # BOM
    (1.3644, 103.9915),    # SIN
    (2.7456, 101.7099),    # KUL
    (13.6900, 100.7501),   # BKK
    (-6.1256, 106.6558),   # CGK
    (22.3080, 113.9185),   # HKG
    (23.3924, 113.2988),   # CAN
    (31.1443, 121.8083),   # PVG
    (30.5785, 103.9472),   # CTU
    (40.0799, 116.6031),   # PEK
    (37.4602, 126.4407),   # ICN
    (25.0777, 121.2328),   # TPE
    (35.7720, 140.3929),   # NRT
    # Oceania
    (-33.9399, 151.1753),  # SYD
    (-37.6690, 144.8410),  # MEL
    (-31.9385, 115.9672),  # PER
    (-37.0082, 174.7850),  # AKL
    # South America
    (-23.4356, -46.4731),  # GRU
    (4.7016, -74.1469),    # BOG
    (-34.8222, -58.5358),  # EZE
    (-33.3930, -70.7858),  # SCL
    (-12.0219, -77.1143),  # LIM
    # Africa
    (-26.1392, 28.2460),   # JNB
    (30.1219, 31.4056),    # CAI
    (6.5774, 3.3212),      # LOS
    (-1.3192, 36.9278),    # NBO
    (8.9779, 38.7993),     # ADD
]
FLIGHT_BASES = [
    "https://api.adsb.lol/v2/lat/{lat}/lon/{lon}/dist/250",
    "https://opendata.adsb.fi/api/v2/lat/{lat}/lon/{lon}/dist/250",
]
FLIGHT_MIL_URLS = [
    "https://api.adsb.lol/v2/mil",
    "https://opendata.adsb.fi/api/v2/mil",
]
FLIGHT_HDR = {"Accept": "application/json", "User-Agent": "globe-recon/1.0"}

# Per-category point colors (commercial is the default cyan).
_CAT_COLORS = {"military": "#ff5a5a", "jet": "#feca57",
               "private": "#48dbfb", "commercial": "#7ad7ff"}

# Curated business/private-jet ICAO type designators (exact match set).
_JET_TYPES = {
    "GLF4", "GLF5", "GLF6", "GULF", "G280", "GALX", "GLEX",
    "LJ31", "LJ35", "LJ40", "LJ45", "LJ60", "LJ70", "LJ75",
    "C25A", "C25B", "C25C", "C500", "C501", "C510", "C525", "C526",
    "C550", "C551", "C560", "C56X", "C650", "C680", "C68A", "C700",
    "C750", "CL30", "CL35", "CL60", "CL64",
    "FA10", "FA20", "FA50", "FA5X", "FA7X", "FA8X", "F2TH", "F900",
    "E50P", "E55P", "E545", "E550", "E135", "EA50",
    "H25A", "H25B", "H25C", "HA4T", "HDJT",
    "PC24", "BE40", "PRM1", "ASTR", "WW24", "B190",
    "GL5T", "GL7T", "GL5000", "G650", "G700", "CL35",
}
# Bizjet prefix match (covers Learjet, Citation 5xx, Challenger, Falcon, Gulfstream).
_JET_PREFIXES = ("LJ", "C5", "CL", "FA", "GLF")

# General-aviation / light private types (exact set + prefix match).
_PRIVATE_TYPES = {
    "C172", "C152", "C150", "C162", "C182", "C206", "C210",
    "SR20", "SR22", "BE36", "BE33", "BE35", "BE58", "BE55",
    "DA40", "DA42", "DA62", "DA20", "C72R", "AA5",
}
_PRIVATE_PREFIXES = ("PA", "M20", "TBM", "PC12", "P28", "DV20", "RV")


def _flight_category(a, mil_hexes):
    """Classify one adsb v2 aircraft dict into a coverage category."""
    h = (a.get("hex") or "").lower()
    # military: mil feed membership OR dbFlags military bit (bit 0).
    if h in mil_hexes:
        return "military"
    try:
        if int(a.get("dbFlags") or 0) & 1:
            return "military"
    except (TypeError, ValueError):
        pass
    t = (a.get("t") or "").upper().strip()
    # jet (business/private jet): curated set + bizjet prefixes.
    if t:
        if t in _JET_TYPES or t.startswith(_JET_PREFIXES):
            return "jet"
        # private (general aviation): light category A1 or small types.
        if t in _PRIVATE_TYPES or t.startswith(_PRIVATE_PREFIXES):
            return "private"
    if (a.get("category") or "").upper() == "A1" and t not in _JET_TYPES:
        return "private"
    # commercial: everything else (airliners + unclassified).
    return "commercial"


def normalize_flights(raw):
    """raw is {"ac": [...], "mil_hexes"?: set/list}. Each item gets a category;
    the payload carries a counts_by_category breakdown."""
    mil_hexes = {str(h).lower() for h in (raw.get("mil_hexes") or [])}
    items = []
    counts = {"commercial": 0, "private": 0, "jet": 0, "military": 0}
    for a in (raw.get("ac") or raw.get("aircraft") or []):
        lat, lng = a.get("lat"), a.get("lon")
        if lat is None or lng is None:
            continue
        cat = _flight_category(a, mil_hexes)
        counts[cat] = counts.get(cat, 0) + 1
        code, name = country_from_aircraft(a.get("hex") or a.get("icao"), a.get("r"))
        items.append({"id": a.get("hex"), "lat": lat, "lng": lng,
                      "label": (a.get("flight") or "").strip() or a.get("r"),
                      "alt": a.get("alt_baro"), "model": a.get("t"),
                      "reg": a.get("r"), "speed": a.get("gs"),
                      "track": a.get("track"), "squawk": a.get("squawk"),
                      "category": cat,
                      "country_code": code, "flag": name,
                      "color": _CAT_COLORS.get(cat, _CAT_COLORS["commercial"])})
    p = _payload("flights", items)
    p["counts_by_category"] = counts
    return p


async def _flight_tile(url):
    """Fetch one radius/mil tile; return its `ac[]` list (empty on failure)."""
    try:
        data = await _aget_json(url, headers=FLIGHT_HDR, timeout=25, tries=2)
    except Exception:
        return []
    return data.get("ac") or data.get("aircraft") or []


async def _flights_from_base(base, mil_url):
    """Fetch all radius tiles + the mil feed concurrently against one provider.
    Returns (deduped ac list, set of military hexes)."""
    tile_urls = [base.format(lat=lat, lon=lon) for lat, lon in FLIGHT_TILES]
    results = await asyncio.gather(*(_flight_tile(u) for u in tile_urls + [mil_url]))
    mil_ac = results[-1]
    by_hex = {}
    for ac_list in results:
        for a in ac_list:
            h = a.get("hex")
            if h and h not in by_hex:
                by_hex[h] = a
    mil_hexes = {a.get("hex").lower() for a in mil_ac if a.get("hex")}
    return list(by_hex.values()), mil_hexes


# --- FR24 categorization + normalization --------------------------------
# Military operator ICAO codes commonly seen on FR24 (callsign/airline prefix).
_FR24_MIL_ICAO = {"RCH", "RRR", "CFC", "NATO", "FAF", "GAF", "IAM", "ASY",
                  "BAF", "HUNTER", "PLF", "AME", "RFR", "RSD"}


def _fr24_category(f):
    """Classify one parsed FR24 feed record into a coverage category,
    reusing the adsb type sets so the globe colors stay consistent."""
    t = (f.get("aircraft_type") or "").upper().strip()
    icao = (f.get("airline_icao") or "").upper().strip()
    cs = (f.get("callsign") or "").upper().strip()
    if icao in _FR24_MIL_ICAO or any(cs.startswith(m) for m in _FR24_MIL_ICAO):
        return "military"
    if t:
        if t in _JET_TYPES or t.startswith(_JET_PREFIXES):
            return "jet"
        if t in _PRIVATE_TYPES or t.startswith(_PRIVATE_PREFIXES):
            return "private"
    # Commercial flights carry an airline ICAO + a flight number.
    if icao and f.get("flight_number"):
        return "commercial"
    # No operator and a tail-number-only callsign -> general aviation.
    if not icao:
        return "private"
    return "commercial"


def normalize_flights_fr24(flights):
    """flights: {fr24_id: parsed_record} from fr24.fetch_live (+enrich_subset).
    Emits the existing globe point shape PLUS FR24 detail fields."""
    items = []
    counts = {"commercial": 0, "private": 0, "jet": 0, "military": 0}
    for f in flights.values():
        lat, lng = f.get("lat"), f.get("lng")
        if lat is None or lng is None:
            continue
        cat = _fr24_category(f)
        counts[cat] = counts.get(cat, 0) + 1
        label = (f.get("flight_number") or f.get("callsign")
                 or f.get("registration") or "").strip()
        # FR24's parsed record carries the registration (tail) but no ICAO hex,
        # so country comes from the registration-prefix table (hex if present).
        code, name = country_from_aircraft(f.get("hex") or f.get("icao"),
                                           f.get("registration"))
        items.append({
            "id": f.get("fr24_id"), "lat": lat, "lng": lng,
            "label": label or f.get("fr24_id"),
            "alt": f.get("alt_ft"), "model": f.get("aircraft_type"),
            "reg": f.get("registration"), "speed": f.get("speed_kt"),
            "track": f.get("track"), "squawk": f.get("squawk"),
            "category": cat,
            "country_code": code, "flag": name,
            # FR24 enrichment:
            "airline": f.get("airline"),
            "origin": f.get("origin_iata"), "destination": f.get("dest_iata"),
            "origin_name": f.get("origin_name"), "dest_name": f.get("dest_name"),
            "flight_number": (f.get("flight_number") or "").strip() or None,
            "aircraft_type": f.get("aircraft_type"),
            "aircraft_model": f.get("aircraft_model"),
            "registration": f.get("registration"),
            "photo_url": f.get("photo_url"),
            "callsign": (f.get("callsign") or "").strip() or None,
            "source": "fr24",
            "color": _CAT_COLORS.get(cat, _CAT_COLORS["commercial"]),
        })
    p = _payload("flights", items)
    p["counts_by_category"] = counts
    p["source"] = "fr24"
    return p


# Wall-clock ceiling for a single flights cycle. Bumped 8→13s for concurrent
# OpenSky + FR24 fetches — OpenSky's global /states/all is ~12s through
# residential proxy on cold cycles.
_FLIGHTS_FETCH_DEADLINE_S = 13.0


# ===========================================================================
# OpenSky `/states/all` — global ADS-B snapshot, keyless, no feeder required.
# Adds aircraft FR24 omits (military operators on the BAARR list, private
# blocked tails, primary radar / non-ICAO targets) — merged by ICAO hex.
# ===========================================================================
OPENSKY_URL = "https://opensky-network.org/api/states/all"
OPENSKY_HDR = {"Accept": "application/json", "User-Agent": "globe-recon/1.0"}
_M_TO_FT = 3.28084
_MS_TO_KT = 1.94384


async def _fetch_opensky_states():
    try:
        data = await _aget_json(OPENSKY_URL, headers=OPENSKY_HDR, timeout=12, tries=1)
    except Exception:
        return {}
    out = {}
    for s in data.get("states") or []:
        if not s or len(s) < 11: continue
        hex_ = (s[0] or "").lower().lstrip("~")
        lat = s[6]; lng = s[5]
        if not hex_ or lat is None or lng is None: continue
        alt_m = s[7] if s[7] is not None else s[13]
        vel_ms = s[9]
        out[hex_] = {"modeS": hex_, "lat": lat, "lng": lng,
                     "alt_ft": int(alt_m * _M_TO_FT) if alt_m is not None else None,
                     "speed_kt": vel_ms * _MS_TO_KT if vel_ms is not None else None,
                     "track": s[10], "callsign": (s[1] or "").strip() or None,
                     "squawk": s[14] if len(s) > 14 else None,
                     "country": s[2], "_source": "opensky"}
    return out


def _opensky_record_to_item(rec):
    hex_ = rec["modeS"]; cs = rec.get("callsign")
    cat = "private"; cs_u = (cs or "").upper().strip()
    if any(cs_u.startswith(m) for m in _FR24_MIL_ICAO):
        cat = "military"
    elif cs and len(cs.strip()) >= 5:
        cat = "commercial"
    code, name = country_from_aircraft(hex_, None)
    return {"id": hex_, "lat": rec["lat"], "lng": rec["lng"],
            "label": cs or hex_.upper(),
            "alt": rec.get("alt_ft"), "model": None, "reg": None,
            "speed": rec.get("speed_kt"), "track": rec.get("track"),
            "squawk": rec.get("squawk"), "category": cat,
            "country_code": code, "flag": name, "callsign": cs,
            "source": "opensky",
            "color": _CAT_COLORS.get(cat, _CAT_COLORS["commercial"])}


def _merge_opensky(fr24_payload, fr24_hexes, opensky_map):
    if not opensky_map: return fr24_payload
    added = 0
    for hex_, rec in opensky_map.items():
        if hex_ in fr24_hexes: continue
        fr24_payload["items"].append(_opensky_record_to_item(rec))
        added += 1
        cat = fr24_payload["items"][-1]["category"]
        fr24_payload.setdefault("counts_by_category", {})
        fr24_payload["counts_by_category"][cat] = (
            fr24_payload["counts_by_category"].get(cat, 0) + 1)
    fr24_payload["count"] = len(fr24_payload["items"])
    fr24_payload["opensky_added"] = added
    return fr24_payload


async def _fetch_flights_fr24():
    """Returns (payload, fr24_hexes) — hexes needed for OpenSky merge dedup."""
    flights = await fr24.fetch_live()
    if not flights:
        raise RuntimeError("FR24 returned 0 flights (soft-block?)")
    try:
        await fr24.enrich_subset(flights, limit=80, time_budget=4.0)
    except Exception:
        fr24.apply_photo_cache(flights)
    payload = normalize_flights_fr24(flights)
    fr24_hexes = {(f.get("modeS") or "").lower().lstrip("~")
                  for f in flights.values() if f.get("modeS")}
    fr24_hexes.discard("")
    return payload, fr24_hexes


async def _gather_flights_or_fallback(fr24_task, opensky_task):
    try:
        fr24_result = await asyncio.wait_for(fr24_task, timeout=_FLIGHTS_FETCH_DEADLINE_S)
    except Exception:
        opensky_task.cancel()
        raise
    try:
        opensky_map = await asyncio.wait_for(opensky_task, timeout=_FLIGHTS_FETCH_DEADLINE_S)
    except Exception:
        opensky_map = {}
    return fr24_result, opensky_map


async def fetch_flights():
    """FR24 + OpenSky merged by ICAO hex. FR24 wins on shared airframes;
    OpenSky-only records fill the gaps (military/private blocks)."""
    if _HAS_FR24:
        try:
            fr24_task = asyncio.create_task(_fetch_flights_fr24())
            opensky_task = asyncio.create_task(_fetch_opensky_states())
            (payload, fr24_hexes), opensky_map = await _gather_flights_or_fallback(
                fr24_task, opensky_task)
            if payload and payload["count"]:
                if opensky_map:
                    _merge_opensky(payload, fr24_hexes, opensky_map)
                payload["source"] = "fr24+opensky" if opensky_map else "fr24"
                return payload
        except Exception:
            pass
    last_err = None
    for base, mil_url in zip(FLIGHT_BASES, FLIGHT_MIL_URLS):
        try:
            ac, mil_hexes = await _flights_from_base(base, mil_url)
            p = normalize_flights({"ac": ac, "mil_hexes": mil_hexes})
            if p["count"]:
                p["source"] = "adsb.lol"
                return p
        except Exception as e:
            last_err = e
    if last_err:
        raise last_err
    return normalize_flights({"ac": []})


# ===========================================================================
# satellites — CelesTrak GP JSON; positions recomputed each cycle via SGP4.
# TLE refresh cadence is slow; positions are cheap, so we re-propagate every
# fetch (the collector calls fetch() on the satellites interval).
# ===========================================================================
def _gp_url(group):
    return f"https://celestrak.org/NORAD/elements/gp.php?GROUP={group}&FORMAT=json"


# (group/mission tag, URL, per-group cap). Unioned by NORAD id; the overall
# total is also capped (SAT_TOTAL_CAP) to keep per-cycle SGP4 propagation cheap.
# Union by NORAD id, capped at SAT_TOTAL_CAP. Specific mission groups are listed
# FIRST so they claim their NORAD ids (and the more-specific `_group` tag used
# for per-sat coloring) before the broad `active` group. `active` (~15k objects)
# is the catch-all filler at the end — it backfills remaining slots up to the cap
# so the globe stays densely populated even as the specific groups shrink.
SAT_GROUPS = [
    ("stations",           _gp_url("stations"),           100),
    ("visual",             _gp_url("visual"),             200),
    ("weather",            _gp_url("weather"),            150),
    ("science",            _gp_url("science"),            200),
    ("geo",                _gp_url("geo"),                600),
    ("gps-ops",            _gp_url("gps-ops"),            50),
    ("starlink",           _gp_url("starlink"),           6000),
    ("oneweb",             _gp_url("oneweb"),             1000),
    ("planet",             _gp_url("planet"),             600),
    ("spire",              _gp_url("spire"),              300),
    ("iridium-NEXT",       _gp_url("iridium-NEXT"),       100),
    ("globalstar",         _gp_url("globalstar"),         100),
    ("ses",                _gp_url("ses"),                100),
    ("intelsat",           _gp_url("intelsat"),           100),
    ("telesat",            _gp_url("telesat"),            100),
    ("last-30-days",       _gp_url("last-30-days"),       600),
    ("cosmos-2251-debris", _gp_url("cosmos-2251-debris"), 800),
    ("iridium-33-debris",  _gp_url("iridium-33-debris"),  500),
    ("active",             _gp_url("active"),             11000),
]
SAT_TOTAL_CAP = 8000

try:
    from sgp4.api import Satrec, jday, WGS72  # type: ignore
    _HAS_SGP4 = True
except Exception:
    _HAS_SGP4 = False

_RE = 6378.137  # Earth equatorial radius, km


def _gmst(jd_ut1: float) -> float:
    t = (jd_ut1 - 2451545.0) / 36525.0
    g = (67310.54841 + (876600.0 * 3600 + 8640184.812866) * t
         + 0.093104 * t * t - 6.2e-6 * t * t * t)
    g = (g % 86400.0) * (2 * math.pi / 86400.0)
    return g % (2 * math.pi)


def _teme_to_latlonalt(r_teme, jd, fr):
    x, y, z = r_teme
    theta = _gmst(jd + fr)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    xe = x * cos_t + y * sin_t
    ye = -x * sin_t + y * cos_t
    ze = z
    f = 1 / 298.26
    e2 = f * (2 - f)
    lon = math.atan2(ye, xe)
    p = math.sqrt(xe * xe + ye * ye)
    lat = math.atan2(ze, p)
    for _ in range(5):
        sin_lat = math.sin(lat)
        c = _RE / math.sqrt(1 - e2 * sin_lat * sin_lat)
        lat = math.atan2(ze + c * e2 * sin_lat, p)
    sin_lat = math.sin(lat)
    c = _RE / math.sqrt(1 - e2 * sin_lat * sin_lat)
    alt = p / math.cos(lat) - c
    return math.degrees(lat), (math.degrees(lon) + 180) % 360 - 180, alt


def _propagate(gp: dict):
    if not _HAS_SGP4:
        return None
    try:
        sat = Satrec()
        epoch = gp["EPOCH"]
        dt = datetime.fromisoformat(epoch.replace("Z", "+00:00"))
        epoch_days = (dt.replace(tzinfo=timezone.utc)
                      - datetime(1949, 12, 31, tzinfo=timezone.utc)).total_seconds() / 86400.0
        no_kozai = float(gp["MEAN_MOTION"]) * 2 * math.pi / 1440.0
        sat.sgp4init(
            WGS72, 'i', int(gp["NORAD_CAT_ID"]), epoch_days,
            float(gp.get("BSTAR", 0.0)),
            float(gp.get("MEAN_MOTION_DOT", 0.0)),
            float(gp.get("MEAN_MOTION_DDOT", 0.0)),
            float(gp["ECCENTRICITY"]),
            math.radians(float(gp["ARG_OF_PERICENTER"])),
            math.radians(float(gp["INCLINATION"])),
            math.radians(float(gp["MEAN_ANOMALY"])),
            no_kozai,
            math.radians(float(gp["RA_OF_ASC_NODE"])),
        )
        now = datetime.now(timezone.utc)
        jd, fr = jday(now.year, now.month, now.day, now.hour, now.minute,
                      now.second + now.microsecond / 1e6)
        e, r, v = sat.sgp4(jd, fr)
        if e != 0:
            return None
        lat, lon, alt = _teme_to_latlonalt(r, jd, fr)
        speed = math.sqrt(sum(c * c for c in v))
        return {"lat": round(lat, 4), "lng": round(lon, 4),
                "alt_km": round(alt, 2), "velocity_km_s": round(speed, 4)}
    except Exception:
        return None


def normalize_satellites(raw):
    """raw is a list of GP element-set dicts (optionally tagged with `_group`)."""
    items = []
    for gp in raw:
        pos = _propagate(gp)
        if not pos:
            continue
        items.append({"id": gp.get("NORAD_CAT_ID"),
                      "lat": pos["lat"], "lng": pos["lng"], "alt": pos["alt_km"],
                      "label": gp.get("OBJECT_NAME"),
                      "object_id": gp.get("OBJECT_ID"),
                      "group": gp.get("_group"),
                      "velocity_km_s": pos["velocity_km_s"],
                      "color": "#ffd166"})
    return _payload("satellites", items)


# GP element-set cache. Orbital elements drift on the order of hours, but SGP4
# propagation is cheap (~0.05s for 8k objects), so we download the union of GP
# groups at most every SAT_ELEM_TTL_S and re-propagate every cycle. This keeps a
# satellites cycle network-free (and well under the ~10s cadence) once warm,
# instead of re-downloading ~15k elements (10s+ over the proxy) every cycle.
SAT_ELEM_TTL_S = 1800  # refresh element sets every 30 min
_sat_elem_cache = {"ts": 0.0, "sats": None}


async def _fetch_sat_elements():
    """Download + union the GP groups by NORAD id, capped at SAT_TOTAL_CAP."""
    sats = []
    seen = set()
    for group, url, cap in SAT_GROUPS:
        try:
            data = await _aget_json(url, timeout=45)
        except Exception:
            continue
        if not isinstance(data, list):
            continue
        for gp in data[:cap]:
            if len(sats) >= SAT_TOTAL_CAP:
                break
            nid = gp.get("NORAD_CAT_ID")
            if nid in seen:
                continue
            seen.add(nid)
            gp["_group"] = group
            sats.append(gp)
        if len(sats) >= SAT_TOTAL_CAP:
            break
    return sats


async def fetch_satellites():
    now = time.monotonic()
    cached = _sat_elem_cache["sats"]
    if cached is None or (now - _sat_elem_cache["ts"]) >= SAT_ELEM_TTL_S:
        sats = await _fetch_sat_elements()
        # Only replace the cache if the download actually produced elements;
        # a transient proxy failure keeps the last good set propagating.
        if sats:
            _sat_elem_cache["sats"] = sats
            _sat_elem_cache["ts"] = now
        elif cached is not None:
            sats = cached
    else:
        sats = cached
    p = normalize_satellites(sats)
    if not _HAS_SGP4:
        p["error"] = "sgp4 not importable — positions unavailable"
    return p


# ===========================================================================
# markets — Yahoo Finance chart API (+ Stooq CSV fallback). Record array.
# ===========================================================================
MARKET_SYMBOLS = [
    ("RTX",  "RTX Corp (Raytheon)", "rtx.us"),
    ("LMT",  "Lockheed Martin",     "lmt.us"),
    ("NOC",  "Northrop Grumman",    "noc.us"),
    ("GD",   "General Dynamics",    "gd.us"),
    ("BA",   "Boeing",              "ba.us"),
    ("PLTR", "Palantir",            "pltr.us"),
    ("CL=F", "WTI Crude Oil",       "cl.f"),
    ("BZ=F", "Brent Crude Oil",     "bz.f"),
    ("GC=F", "Gold",                "gc.f"),
    ("SI=F", "Silver",              "si.f"),
    ("HG=F", "Copper",              "hg.f"),
    ("NG=F", "Natural Gas",         "ng.f"),
]
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
STOOQ = "https://stooq.com/q/l/?s={symbol}&f=sd2t2ohlcv&h&e=csv"


def normalize_markets(raw):
    """raw is already a list of {symbol,name,price,change_percent,up,source}."""
    items = [r for r in raw if r.get("price") is not None]
    return _payload("markets", items)


async def _market_yahoo(symbol):
    try:
        data = await _aget_json(YAHOO.format(symbol=quote(symbol)),
                                headers={"Accept": "application/json"}, timeout=20, tries=2)
    except Exception:
        return None
    result = (((data.get("chart") or {}).get("result")) or [None])[0]
    if not result:
        return None
    meta = result.get("meta") or {}
    price = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    if price is None:
        return None
    change_pct = round((price - prev) / prev * 100, 4) if prev else None
    return {"price": price, "change_percent": change_pct,
            "up": (change_pct is not None and change_pct >= 0), "source": "yahoo"}


async def _market_stooq(stooq_symbol):
    if not stooq_symbol:
        return None
    try:
        text = await _aget_text(STOOQ.format(symbol=stooq_symbol), timeout=20, tries=2)
    except Exception:
        return None
    lines = text.strip().splitlines()
    if len(lines) < 2:
        return None
    header = [h.strip().lower() for h in lines[0].split(",")]
    rec = dict(zip(header, lines[1].split(",")))
    try:
        close, open_ = float(rec.get("close")), float(rec.get("open"))
    except Exception:
        return None
    change_pct = round((close - open_) / open_ * 100, 4) if open_ else None
    return {"price": close, "change_percent": change_pct,
            "up": (change_pct is not None and change_pct >= 0), "source": "stooq"}


async def fetch_markets():
    rows = []
    for ysym, name, ssym in MARKET_SYMBOLS:
        q = await _market_yahoo(ysym)
        if not q:
            q = await _market_stooq(ssym)
        if not q:
            continue
        rows.append({"symbol": ysym, "name": name, "price": q["price"],
                     "change_percent": q["change_percent"], "up": q["up"],
                     "source": q["source"]})
    return normalize_markets(rows)


# ===========================================================================
# news — BBC + Al Jazeera + Google News RSS with risk score. Record array.
# ===========================================================================
NEWS_FEEDS = [
    ("BBC World",  "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
    ("Reuters/Wire (Google News)",
     "https://news.google.com/rss/search?q=when:24h+conflict+OR+war+OR+strike&hl=en-US&gl=US&ceid=US:en"),
]
CONFLICT_KEYWORDS = {
    "nuclear": 3, "missile": 3, "airstrike": 3, "air strike": 3, "war": 2,
    "invasion": 3, "killed": 2, "dead": 2, "casualt": 2, "troops": 2,
    "strike": 1, "sanction": 2, "ceasefire": 2, "militant": 2, "terror": 2,
    "explosion": 2, "bomb": 2, "shelling": 2, "offensive": 1, "rebel": 1,
    "coup": 3, "hostage": 2, "drone": 1, "border clash": 3, "genocide": 3,
    "evacuat": 1, "refugee": 1, "conflict": 1, "attack": 2, "wounded": 1,
}
_STEM_KEYS = {"casualt", "evacuat", "militant", "shelling"}


def _risk_score(title, summary):
    text = f"{title} {summary}".lower()
    score, hits = 0, []
    for kw, w in CONFLICT_KEYWORDS.items():
        if kw in _STEM_KEYS:
            matched = re.search(r"\b" + re.escape(kw), text) is not None
        else:
            matched = re.search(r"\b" + re.escape(kw) + r"\b", text) is not None
        if matched:
            score += w
            hits.append(kw)
    return min(score, 10), hits


def normalize_news(raw):
    """raw is already a list of parsed article dicts (with risk_score)."""
    items = sorted(raw, key=lambda r: r.get("risk_score", 0), reverse=True)
    return _payload("news", items)


def _parse_rss(source_name, xml_bytes):
    out = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return out
    for item in root.iter("item"):
        def _txt(tag):
            el = item.find(tag)
            return el.text.strip() if el is not None and el.text else None
        title = _txt("title") or ""
        desc = _txt("description") or ""
        summary = re.sub(r"<[^>]+>", "", desc).strip()
        score, hits = _risk_score(title, summary)
        out.append({"source_feed": source_name, "title": title, "link": _txt("link"),
                    "published": _txt("pubDate"), "summary": summary[:500],
                    "risk_score": score, "risk_keywords": hits})
    return out


async def fetch_news():
    articles = []
    hdr = {"Accept": "application/rss+xml, application/xml, text/xml"}
    for name, url in NEWS_FEEDS:
        try:
            r = await F.aget(url, headers=hdr, timeout=30)
            if r.tier == "refused_no_proxy":
                raise RuntimeError("proxy not configured")
            if r.status != 200:
                continue
            articles.extend(_parse_rss(name, r.body))
        except Exception:
            continue
    return normalize_news(articles)


# ===========================================================================
# natural-events — NASA EONET v3 open events.
# ===========================================================================
EONET_URL = "https://eonet.gsfc.nasa.gov/api/v3/events?status=open"


def normalize_natural_events(raw):
    items = []
    for ev in raw.get("events", []):
        cats = ev.get("categories") or []
        category = cats[0].get("title") if cats else None
        sources = ev.get("sources") or []
        link = sources[0].get("url") if sources else ev.get("link")
        geoms = ev.get("geometry") or []
        last = geoms[-1] if geoms else {}
        coords = last.get("coordinates")
        lat = lng = None
        if isinstance(coords, list) and coords and isinstance(coords[0], (int, float)):
            lng = coords[0]
            lat = coords[1] if len(coords) > 1 else None
        if lat is None or lng is None:
            continue  # skip polygon-only events (no plottable point)
        items.append({"id": ev.get("id"), "lat": lat, "lng": lng,
                      "label": ev.get("title"), "category": category,
                      "categories": [c.get("title") for c in cats],
                      "date": last.get("date"), "url": link,
                      "magnitude": last.get("magnitudeValue"),
                      "magnitude_unit": last.get("magnitudeUnit"),
                      "color": "#26de81"})
    return _payload("natural-events", items)


async def fetch_natural_events():
    data = await _aget_json(EONET_URL, timeout=30)
    return normalize_natural_events(data)


# ===========================================================================
# wildfire — NIFC WFIGS ArcGIS GeoJSON (keyless US incidents).
# ===========================================================================
NIFC_URL = ("https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
            "WFIGS_Incident_Locations_Current/FeatureServer/0/query"
            "?where=1%3D1&outFields=*&f=geojson&resultRecordCount=2000")


def normalize_wildfire(raw):
    items = []
    for feat in raw.get("features", []):
        p = feat.get("properties") or {}
        coords = (feat.get("geometry") or {}).get("coordinates") or [None, None]
        coords = (coords + [None, None])[:2]
        lng, lat = coords[0], coords[1]
        if lat is None or lng is None:
            continue
        items.append({"id": p.get("IrwinID") or p.get("OBJECTID"), "lat": lat, "lng": lng,
                      "label": p.get("IncidentName"),
                      "size_acres": p.get("IncidentSize") or p.get("DailyAcres"),
                      "cause": p.get("FireCause"),
                      "containment": p.get("PercentContained"),
                      "state": p.get("POOState"),
                      "discovery": _iso_from_ms(p.get("FireDiscoveryDateTime")),
                      "color": "#ff9f43"})
    return _payload("wildfire", items)


async def fetch_wildfire():
    data = await _aget_json(NIFC_URL, timeout=45)
    return normalize_wildfire(data)


# ===========================================================================
# cyber — CISA KEV catalog. Record array (no coordinates).
# ===========================================================================
KEV_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"


def normalize_cyber(raw):
    items = []
    for v in (raw.get("vulnerabilities") or []):
        items.append({"id": v.get("cveID"), "label": v.get("vulnerabilityName"),
                      "vendor": v.get("vendorProject"), "product": v.get("product"),
                      "dateAdded": v.get("dateAdded"),
                      "shortDescription": v.get("shortDescription"),
                      "dueDate": v.get("dueDate"),
                      "ransomware": v.get("knownRansomwareCampaignUse"),
                      "color": "#ee5253"})
    # newest-first
    items.sort(key=lambda x: x.get("dateAdded") or "", reverse=True)
    return _payload("cyber", items)


async def fetch_cyber():
    data = await _aget_json(KEV_URL, timeout=60)
    return normalize_cyber(data)


# ===========================================================================
# frontlines — DeepStateMap history GeoJSON. items = GeoJSON Feature[].
# ===========================================================================
DSM_LAST = "https://deepstatemap.live/api/history/last"
DSM_GEOJSON = "https://deepstatemap.live/api/history/{ts}/geojson"


def _extract_features(data):
    if isinstance(data, dict):
        if data.get("type") == "FeatureCollection" and isinstance(data.get("features"), list):
            return data["features"]
        for key in ("map", "geojson", "data", "result"):
            feats = _extract_features(data.get(key))
            if feats:
                return feats
    return []


def normalize_frontlines(raw):
    """Keep GeoJSON Feature[] as-is, but normalize the name (DeepState localizes)."""
    feats = _extract_features(raw)
    out = []
    for feat in feats:
        if not isinstance(feat, dict):
            continue
        props = dict(feat.get("properties") or {})
        name = props.get("name")
        if isinstance(name, dict):
            name = name.get("en") or name.get("uk") or next(iter(name.values()), None)
        props["name"] = name
        out.append({"type": "Feature",
                    "id": feat.get("id") or props.get("id"),
                    "properties": props,
                    "geometry": feat.get("geometry") or {}})
    return _payload("frontlines", out)


async def fetch_frontlines():
    data = await _aget_json(DSM_LAST, timeout=45)
    feats = _extract_features(data)
    if not feats:
        hid = data.get("id") if isinstance(data, dict) else None
        if hid:
            data = await _aget_json(DSM_GEOJSON.format(ts=hid), timeout=45)
    return normalize_frontlines(data)


# ===========================================================================
# cctv — TfL + CalTrans + 511 DataTables + CARS gateway + city DOT feeds.
# Reuses osiris-cctv-cameras parse logic for the keyless networks, plus
# city-targeted networks so the user's CITIES (NYC, LA, SF, Chicago, Miami,
# Boston, DC) all carry real cameras with a working image_url snapshot.
#
# Coverage map (city -> network):
#   NYC      -> nyctmc (961 NYC-metro snapshots) + 511ny (NY State, incl. NYC)
#   LA / SF  -> caltrans (D7=LA, D4=SF Bay) + optional 511_sfbay (env token)
#   Chicago  -> idot_gateway (IL/IN/WI Lake Michigan gateway, ~3.6k snapshots)
#   Miami    -> fl511 (FL incl. Miami-Dade)
#   Boston   -> mass511 (CARS) + nh511/newengland (DataTables)
#   DC       -> ddot_dc (DDOT CCTV locations; image best-effort, see note)
# ===========================================================================
_CCTV_API_HDR = {"Accept": "application/json", "User-Agent": "globe-cctv/1.0"}
_CCTV_DT_HDR = {"Accept": "application/json", "User-Agent": "globe-cctv/1.0",
                "X-Requested-With": "XMLHttpRequest"}
# A real browser UA — DataTables endpoints + ArcGIS edges return cleaner JSON
# with this than with the bare globe-cctv UA on some sites.
_CCTV_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/146.0.0.0 Safari/537.36")

CARS_SITES = [
    ("511mn",  "https://511mn.org",       "https://mntg.carsprogram.org", "US", "Minnesota"),
    ("mass511", "https://mass511.com",     "https://matg.carsprogram.org", "US", "Massachusetts"),
    ("511ia",  "https://511ia.org",       "https://iatg.carsprogram.org", "US", "Iowa"),
    ("cotrip", "https://cotrip.org",      "https://cotg.carsprogram.org", "US", "Colorado"),
    ("511ne",  "https://511.nebraska.gov", "https://netg.carsprogram.org", "US", "Nebraska"),
    ("kandrive", "https://www.kandrive.gov", "https://kstg.carsprogram.org", "US", "Kansas"),
]

# 511 DataTables family — shared vendor software; keyless POST
# /List/GetData/Cameras returning DataTables JSON. imageUrl is relative
# (prefix with base) and is a still-snapshot proxy (/map/Cctv/{n}) that
# refreshes. These regressed from the collector simply because they were never
# wired into fetch_cctv(); the upstream pattern still works through the proxy
# (verified 2026-05-21, no cookie-warm needed). WKT is POINT(lng lat).
#   (slug, base, country, region, default_city)
CCTV_DT_SITES = [
    ("fl511",   "https://fl511.com",             "US", "Florida",       None),
    ("ga511",   "https://511ga.org",             "US", "Georgia",       None),
    ("511ny",   "https://511ny.org",             "US", "New York",      None),
    ("511pa",   "https://www.511pa.com",         "US", "Pennsylvania",  None),
    ("511la",   "https://www.511la.org",         "US", "Louisiana",     None),
    ("id511",   "https://511.idaho.gov",         "US", "Idaho",         None),
    ("az511",   "https://az511.com",             "US", "Arizona",       None),
    ("nh511",   "https://www.newengland511.org", "US", "New England",   None),
    # Added 2026-05-21 — same DataTables vendor (/List/GetData/Cameras), each
    # verified through the proxy returning camera lists with WKT POINT lat/lng
    # and a relative /map/Cctv/{n} snapshot (prefixed -> absolute https JPG).
    ("511ak",   "https://511.alaska.gov",        "US", "Alaska",        None),
    ("511nv",   "https://www.nvroads.com",       "US", "Nevada",        None),
    ("511on",   "https://511on.ca",              "CA", "Ontario",       None),
    ("511nb",   "https://511.gnb.ca",            "CA", "New Brunswick", None),
    ("511yk",   "https://511yukon.ca",           "CA", "Yukon",         None),
    ("511ns",   "https://511.novascotia.ca",     "CA", "Nova Scotia",   None),
    ("511ab",   "https://511.alberta.ca",        "CA", "Alberta",       None),
    ("511sk",   "https://hotline.gov.sk.ca",     "CA", "Saskatchewan",  None),
    ("511nl",   "https://511nl.ca",              "CA", "Newfoundland",  None),
    ("511pei",  "https://511.gov.pe.ca",         "CA", "Prince Edward Island", None),
    # Added 2026-05-23 (Agent E sweep) — same DataTables vendor (IBI/Iteris).
    # These three sit behind Akamai-class edges that require fresh-IP rotation
    # per attempt, which is why _dt_post_page was hardened (see below).
    # Probe counts: udot=2045 cams, 511wi=482, ctroads=347.
    ("ctroads", "https://ctroads.org",                "US", "Connecticut", None),
    ("udot",    "https://udottraffic.utah.gov",       "US", "Utah",        None),
    ("511wi",   "https://511wi.gov",                  "US", "Wisconsin",   None),
]


def normalize_cctv(raw):
    rows = raw if isinstance(raw, list) else raw.get("items", [])
    items = [{"id": r.get("camera_id") or r.get("id"), "lat": r.get("lat"),
              "lng": r.get("lng"), "label": r.get("name"),
              "image_url": r.get("image_url"), "stream_url": r.get("stream_url"),
              "network": r.get("network"), "city": r.get("city"),
              "country": r.get("country"), "color": "#9b8cff"}
             for r in rows if r.get("lat") is not None and r.get("lng") is not None]
    return _payload("cctv", items)


def _cars_views(views):
    image_url, stream_url = None, None
    for v in (views or []):
        url = v.get("url")
        vtype = (v.get("type") or "").upper()
        prev = v.get("videoPreviewUrl")
        if prev and not image_url:
            image_url = prev
        if not url:
            continue
        if vtype == "STILL_IMAGE" or url.lower().endswith((".jpg", ".jpeg", ".png")):
            if not image_url:
                image_url = url
        elif not stream_url:
            stream_url = url
    return image_url, stream_url


async def _cctv_tfl():
    out = []
    data = await _aget_json("https://api.tfl.gov.uk/Place/Type/JamCam",
                            headers=_CCTV_API_HDR, timeout=40)
    for p in data:
        props = {ap.get("key"): ap.get("value") for ap in (p.get("additionalProperties") or [])}
        out.append({"network": "tfl_jamcams", "camera_id": "tfl-" + str(p.get("id")),
                    "name": p.get("commonName"), "lat": _num(p.get("lat")),
                    "lng": _num(p.get("lon")), "city": "London", "country": "GB",
                    "image_url": props.get("imageUrl"), "stream_url": props.get("videoUrl")})
    return out


async def _cctv_caltrans():
    out = []
    for d in range(1, 13):
        url = f"https://cwwp2.dot.ca.gov/data/d{d}/cctv/cctvStatusD{d:02d}.json"
        try:
            data = await _aget_json(url, headers=_CCTV_API_HDR, timeout=30, tries=2)
        except Exception:
            continue
        for row in data.get("data", []):
            c = row.get("cctv", {})
            loc = c.get("location", {})
            still = (c.get("imageData", {}).get("static") or {}).get("currentImageURL")
            out.append({"network": "caltrans", "camera_id": f"caltrans-d{d}-{c.get('index')}",
                        "name": loc.get("locationName"), "lat": _num(loc.get("latitude")),
                        "lng": _num(loc.get("longitude")), "city": loc.get("nearbyPlace"),
                        "country": "US", "image_url": still,
                        "stream_url": c.get("imageData", {}).get("streamingVideoURL")})
    return out


async def _cctv_cars(slug, front_end, gateway, country, region):
    try:
        await F.aget(front_end + "/", timeout=25)  # cookie warm (non-fatal)
    except Exception:
        pass
    url = gateway + "/cameras_v1/api/cameras"
    try:
        rows = await _aget_json(url, headers={**_CCTV_API_HDR, "Referer": front_end + "/",
                                              "Origin": front_end}, timeout=45, tries=2)
    except Exception:
        return []
    out = []
    for row in rows:
        if row.get("public") is False:
            continue
        loc = row.get("location") or {}
        image_url, stream_url = _cars_views(row.get("views"))
        out.append({"network": slug, "camera_id": f"{slug}-{row.get('id')}",
                    "name": row.get("name"), "lat": _num(loc.get("latitude")),
                    "lng": _num(loc.get("longitude")), "city": loc.get("cityReference"),
                    "country": country, "image_url": image_url, "stream_url": stream_url})
    return out


# --- 511 DataTables family (POST /List/GetData/Cameras) -------------------
def _wkt_point(latlng):
    """(lat, lng) from a DataTables latLng field.

    Shape: {"geography": {"wellKnownText": "POINT (<lng> <lat>)"}}.
    WKT is (longitude latitude) order — longitude first.
    """
    if not isinstance(latlng, dict):
        return None, None
    wkt = (latlng.get("geography") or {}).get("wellKnownText") or ""
    if "POINT" in wkt:
        try:
            inner = wkt[wkt.index("(") + 1:wkt.index(")")]
            lng_s, lat_s = inner.split()
            return _num(lat_s), _num(lng_s)
        except (ValueError, IndexError):
            pass
    return _num(latlng.get("lat")), _num(latlng.get("lng"))


def _dt_post_page(base, start, length, *, tries=20):
    """Sync POST one DataTables page with fresh-IP rotation on failure.

    The new DataTables sites (udot, 511wi, ctroads — added 2026-05-23) sit
    behind Akamai-class edges that throw CONNECT-565 / TLS-reject against the
    shared cached ProxyRack session. Mirrors `_cctv_nyctmc`/`_cctv_skyline`:
    build a fresh ProxyFetcher per attempt (session=None ⇒ new exit IP,
    country=US, uptime=10m) and rotate impersonate. Empirically 4-8 attempts
    lands a 200 on the hardened sites; the easy sites (FL511, 511ny) still
    land on attempt 0. cctv runs on a 6h interval so blocking inside the
    coroutine is fine."""
    from proxy_fetcher import ProxyFetcher

    url = base + "/List/GetData/Cameras"
    hdr = {**_CCTV_DT_HDR, "User-Agent": _CCTV_BROWSER_UA,
           "Referer": base + "/List/Cameras", "Origin": base}
    imps = ("chrome131", "safari17_0", "chrome146")
    refused = False
    for i in range(tries):
        try:
            f = ProxyFetcher(country="US", uptime="10m",
                             impersonate=imps[i % 3], session=None)
            r = f.post(url, headers=hdr,
                       data={"draw": "1", "start": str(start),
                             "length": str(length)}, timeout=35)
        except Exception:
            continue
        if r.tier == "refused_no_proxy":
            refused = True
            continue
        if r.status != 200 or not r.body or r.body[:1] != b"{":
            continue
        try:
            return json.loads(r.body)
        except Exception:
            continue
    if refused:
        raise RuntimeError("proxy not configured (refused_no_proxy)")
    return None


def _dt_rows_to_records(slug, base, country, region, default_city, rows):
    out = []
    for row in rows:
        imgs = row.get("images") or [{}]
        first = imgs[0] if imgs else {}
        img = first.get("imageUrl")
        if img and img.startswith("/"):
            img = base + img            # /map/Cctv/{n} -> absolute refreshing JPG
        vid = first.get("videoUrl")
        lat, lng = _wkt_point(row.get("latLng") or {})
        name = row.get("location") or row.get("roadway") or row.get("nickname")
        out.append({"network": slug, "camera_id": f"{slug}-{row.get('id')}",
                    "name": name, "lat": lat, "lng": lng,
                    "city": row.get("city") or default_city,
                    "country": row.get("country") or country,
                    "image_url": img, "stream_url": vid})
    return out


async def _cctv_dt_site(slug, base, country, region, default_city):
    """Paginate a DataTables 511 site to exhaustion via start/length.

    Page size 100 — the vendor server caps each response at 100 rows regardless
    of a larger requested `length` (verified on 511ny, which returns
    recordsTotal=2282 but only 100 rows/page), so 100 is the max useful page and
    cuts round-trips ~10x vs the old size-10 default. We keep paginating on
    `start` until we've collected recordsTotal rows (or a short page / repeated
    id-set signals the site ignored pagination). This is what makes 511ny pull
    the FULL ~2282 NY State set rather than only the first page.
    """
    def _pull():
        rows_all, start, page = [], 0, 100
        seen = set()
        while True:
            d = _dt_post_page(base, start, page)
            if d is None:
                break
            rows = d.get("data") or d.get("aaData") or []
            if not rows:
                break
            ids = {r.get("id") for r in rows}
            if ids and ids <= seen:        # site ignored pagination — stop
                break
            seen |= ids
            rows_all.extend(rows)
            start += len(rows)
            total = d.get("recordsTotal")
            if total and start >= int(total):
                break
            if len(rows) < page:
                break
        return _dt_rows_to_records(slug, base, country, region, default_city, rows_all)
    return await asyncio.to_thread(_pull)


# --- NYC DOT (NYCTMC) live snapshots --------------------------------------
# webcams.nyctmc.org rejects chrome146 at the proxy edge (CONNECT 56/565) and
# accepts chrome131 / safari17_0 only on a *clean* exit node — the residential
# pool throws a high rate of CONNECT-565 / "CONNECT aborted" for this host, so
# we retry across many fresh rotating IPs (ProxyFetcher(session=None) synthesizes
# a new session id, hence a new exit IP, every call). 28 attempts reliably lands
# a working node (verified 2026-05-21; 1-15 rotations needed during a 565 storm).
# If a cycle still exhausts all attempts, fetch_cctv's per-network last-good
# cache reuses the previous good pull so NYC never drops out of the blob.
# /api/cameras returns the SAME full list the /cameras-list + /map views use:
# ~963 cams with id/name/latitude/longitude/area/isOnline and an https snapshot
# imageUrl (https://webcams.nyctmc.org/api/cameras/{id}/image), dense over the
# five boroughs (the NYC-metro coverage the user asked for). We pull ALL online.
async def _cctv_nyctmc():
    def _pull():
        from proxy_fetcher import ProxyFetcher
        last = None
        imps = ("chrome131", "safari17_0")
        for i in range(28):
            f = ProxyFetcher(impersonate=imps[i % 2], session=None)  # fresh IP
            try:
                r = f.get("https://webcams.nyctmc.org/api/cameras",
                          headers={"Accept": "application/json",
                                   "User-Agent": _CCTV_BROWSER_UA}, timeout=40)
            except Exception:
                continue
            if r.tier == "refused_no_proxy":
                raise RuntimeError("proxy not configured (refused_no_proxy)")
            if r.status == 200 and r.body[:1] in (b"[", b"{"):
                last = r.body
                break
        if not last:
            return []
        data = json.loads(last)
        if isinstance(data, dict):
            for v in data.values():
                if isinstance(v, list) and v:
                    data = v
                    break
        out = []
        for c in (data if isinstance(data, list) else []):
            if str(c.get("isOnline")).lower() == "false":
                continue
            out.append({"network": "nyctmc", "camera_id": f"nyctmc-{c.get('id')}",
                        "name": c.get("name"), "lat": _num(c.get("latitude")),
                        "lng": _num(c.get("longitude")),
                        "city": c.get("area") or "New York", "country": "US",
                        "image_url": c.get("imageUrl"), "stream_url": None})
        return out
    return await asyncio.to_thread(_pull)


# --- Chicago / Illinois (IDOT Travel Midwest gateway) ---------------------
# Illinois Gateway Traffic Cameras hosted ArcGIS FeatureServer. Covers the
# Chicago metro (Lake Michigan Interstate Gateway: IL/IN/WI). `SnapShot` is a
# direct live JPG on cctv.travelmidwest.com that refreshes. ~3.6k cameras.
IDOT_FS = ("https://services2.arcgis.com/aIrBD8yn1TDTEXoz/arcgis/rest/services/"
           "TrafficCamerasTM_Public/FeatureServer/0/query")


async def _cctv_idot():
    out = []
    offset = 0
    page = 1000
    while True:
        url = (IDOT_FS + "?where=1%3D1&outFields=*&outSR=4326&f=json"
               f"&resultOffset={offset}&resultRecordCount={page}")
        data = await _aget_json(url, headers=_CCTV_API_HDR, timeout=50, tries=2)
        feats = data.get("features", [])
        if not feats:
            break
        for ft in feats:
            a = ft.get("attributes") or {}
            g = ft.get("geometry") or {}
            lat = _num(a.get("y")) if a.get("y") is not None else _num(g.get("y"))
            lng = _num(a.get("x")) if a.get("x") is not None else _num(g.get("x"))
            snap = a.get("SnapShot")
            out.append({"network": "idot_gateway",
                        "camera_id": f"idot-{a.get('OBJECTID')}",
                        "name": a.get("CameraLocation"), "lat": lat, "lng": lng,
                        "city": "Chicago", "country": "US",
                        "image_url": snap, "stream_url": None})
        if len(feats) < page or not data.get("exceededTransferLimit"):
            break
        offset += len(feats)
    return out


# --- Washington DC (DDOT TrafficOperations CCTV location layer) -----------
# DDOT's CCTV location layer gives 250 located DC-metro cameras (lat/lng/name).
# DDOT's live-image hosts (cctv.ddot.dc.gov) are Cloudflare-gated / non-routable
# through the residential proxy with curl_cffi (would need Camoufox), so we
# cannot verify a snapshot URL server-side. We still surface DC on the globe
# (location + DDOT viewer page as stream_url) and set image_url=None rather
# than emit a known-broken JPG URL. See RECON note.
DDOT_CCTV = ("https://maps2.dcgis.dc.gov/dcgis/rest/services/DDOT/"
             "TrafficOperations/MapServer/2/query")


async def _cctv_ddot_dc():
    url = (DDOT_CCTV + "?where=1%3D1&outFields=OBJECTID,Location,Latitude,"
           "Longitude,CameraID&outSR=4326&f=json")
    data = await _aget_json(url, headers=_CCTV_API_HDR, timeout=45, tries=2)
    out = []
    for ft in data.get("features", []):
        a = ft.get("attributes") or {}
        g = ft.get("geometry") or {}
        lat = _num(a.get("Latitude")) if a.get("Latitude") is not None else _num(g.get("y"))
        lng = _num(a.get("Longitude")) if a.get("Longitude") is not None else _num(g.get("x"))
        out.append({"network": "ddot_dc", "camera_id": f"ddot-{a.get('CameraID') or a.get('OBJECTID')}",
                    "name": a.get("Location"), "lat": lat, "lng": lng,
                    "city": "Washington", "country": "US",
                    "image_url": None,
                    "stream_url": "https://ddot.dc.gov/services/traffic_cameras"})
    return out


# --- Michigan MiDrive (HTML-in-JSON list) --------------------------------
# https://mdotjboss.state.mi.us/MiDrive/camera/list returns a JSON array of
# ~800 entries. Each row's "county" field is an HTML fragment with lat/lon
# embedded in a /MiDrive/map href; "image" is an <img src=...> on
# micamerasimages.net. Verified pull through ProxyRack 2026-05-23: 801 cams.
_MIDRIVE_LIST = "https://mdotjboss.state.mi.us/MiDrive/camera/list"
_MIDRIVE_LATLON_RE = re.compile(
    r'lat=(?P<lat>-?\d+(?:\.\d+)?)&lon=(?P<lon>-?\d+(?:\.\d+)?)'
    r'(?:&[^"]*)?&id=(?P<id>\d+)')
_MIDRIVE_IMG_RE = re.compile(r'<img[^>]*\bsrc="([^"]+)"', re.I)


def _midrive_parse(rows):
    out = []
    for row in rows:
        href = row.get("county") or ""
        m = _MIDRIVE_LATLON_RE.search(href)
        if not m:
            continue
        cam_id = m.group("id")
        lat = _num(m.group("lat"))
        lng = _num(m.group("lon"))
        if lat is None or lng is None:
            continue
        county = href.split(" <a", 1)[0].strip() or None
        img_m = _MIDRIVE_IMG_RE.search(row.get("image") or "")
        image_url = img_m.group(1) if img_m else None
        stream_url = (f"https://mdotjboss.state.mi.us/MiDrive/map"
                      f"?cameras=true&lat={lat}&lon={lng}&zoom=15&id={cam_id}")
        name_parts = [p for p in (row.get("route"), row.get("location"))
                      if p and isinstance(p, str)]
        name = " ".join(p.strip() for p in name_parts) or None
        out.append({"network": "midrive", "camera_id": f"midrive-{cam_id}",
                    "name": name, "lat": lat, "lng": lng,
                    "city": county, "country": "US",
                    "image_url": image_url, "stream_url": stream_url})
    return out


async def _cctv_midrive():
    def _pull():
        from proxy_fetcher import ProxyFetcher
        imps = ("chrome131", "safari17_0", "chrome146")
        for i in range(15):
            try:
                f = ProxyFetcher(country="US", uptime="10m",
                                 impersonate=imps[i % 3], session=None)
                r = f.get(_MIDRIVE_LIST,
                          headers={"User-Agent": _CCTV_BROWSER_UA,
                                   "Accept": "application/json,*/*"},
                          timeout=45)
            except Exception:
                continue
            if r.tier == "refused_no_proxy":
                raise RuntimeError("proxy not configured (refused_no_proxy)")
            if r.status != 200 or not r.body or r.body[:1] != b"[":
                continue
            try:
                rows = json.loads(r.body)
            except Exception:
                continue
            return _midrive_parse(rows)
        return []
    return await asyncio.to_thread(_pull)


# --- Maryland CHART (ArcGIS MapServer, off-platform) ----------------------
# https://mdgeodata.md.gov/imap/rest/services/Transportation/MD_TrafficCameras
# Keyless Esri service. Returns ~451 features with lat/long + a feed-page URL.
# No durable still snapshot exposed, so image_url=None (matches ddot_dc).
_MD_CHART_Q = (
    "https://mdgeodata.md.gov/imap/rest/services/Transportation/"
    "MD_TrafficCameras/MapServer/0/query"
    "?where=1%3D1&outFields=*&outSR=4326&f=json&resultRecordCount=2000")


async def _cctv_md_chart():
    def _pull():
        from proxy_fetcher import ProxyFetcher
        imps = ("chrome131", "safari17_0", "chrome146")
        for i in range(20):
            try:
                f = ProxyFetcher(country="US", uptime="10m",
                                 impersonate=imps[i % 3], session=None)
                r = f.get(_MD_CHART_Q,
                          headers={"User-Agent": _CCTV_BROWSER_UA,
                                   "Accept": "application/json,*/*"},
                          timeout=45)
            except Exception:
                continue
            if r.tier == "refused_no_proxy":
                raise RuntimeError("proxy not configured (refused_no_proxy)")
            if r.status != 200 or not r.body or r.body[:1] != b"{":
                continue
            try:
                d = json.loads(r.body)
            except Exception:
                continue
            out = []
            for ft in d.get("features", []):
                a = ft.get("attributes") or {}
                g = ft.get("geometry") or {}
                lat = _num(a.get("lat")) if a.get("lat") is not None \
                    else _num(g.get("y"))
                lng = _num(a.get("long")) if a.get("long") is not None \
                    else _num(g.get("x"))
                if lat is None or lng is None:
                    continue
                cam_id = a.get("feedID") or a.get("OBJECTID")
                out.append({
                    "network": "md_chart",
                    "camera_id": f"mdchart-{cam_id}",
                    "name": a.get("location"),
                    "lat": lat, "lng": lng,
                    "city": a.get("county") or "Maryland",
                    "country": "US",
                    "image_url": None,
                    "stream_url": a.get("url"),
                })
            return out
        return []
    return await asyncio.to_thread(_pull)


# --- Washington State DOT (env-gated, free key) ---------------------------
# https://wsdot.wa.gov/Traffic/api/Cameras/CamerasREST.svc/GetCamerasAsJson
# Requires AccessCode (free). Skip cleanly when WSDOT_ACCESS_CODE unset.
async def _cctv_wsdot():
    token = os.environ.get("WSDOT_ACCESS_CODE")
    if not token:
        return []
    url = ("https://wsdot.wa.gov/Traffic/api/Cameras/CamerasREST.svc/"
           f"GetCamerasAsJson?AccessCode={token}")
    data = await _aget_json(url, headers=_CCTV_API_HDR, timeout=45, tries=2)
    out = []
    for c in (data or []):
        loc = c.get("CameraLocation") or {}
        lat = _num(loc.get("Latitude") or c.get("Latitude"))
        lng = _num(loc.get("Longitude") or c.get("Longitude"))
        if lat is None or lng is None:
            continue
        out.append({"network": "wsdot",
                    "camera_id": f"wsdot-{c.get('CameraID')}",
                    "name": c.get("Title") or c.get("Description"),
                    "lat": lat, "lng": lng,
                    "city": loc.get("Description"), "country": "US",
                    "image_url": c.get("ImageURL"),
                    "stream_url": None})
    return out


# --- SF Bay Area (511.org) — KEY REQUIRED, env-gated ----------------------
# 511.org has an open traffic-camera API but requires a free token. We skip it
# cleanly when SF_BAY_511_TOKEN (or FIVEONEONE_ORG_TOKEN) is unset. CalTrans
# D4 already covers the SF Bay Area, so SF is covered regardless.
async def _cctv_511_sfbay():
    token = (os.environ.get("SF_BAY_511_TOKEN")
             or os.environ.get("FIVEONEONE_ORG_TOKEN"))
    if not token:
        return []
    url = (f"https://api.511.org/traffic/cameras?api_key={token}&format=json")
    data = await _aget_json(url, headers=_CCTV_API_HDR, timeout=45, tries=2)
    feats = data.get("cctvs") or data.get("features") or []
    out = []
    for c in feats:
        rec = c.get("Cctv") or c.get("properties") or c
        loc = rec.get("location") or {}
        lat = _num(loc.get("latitude") or rec.get("latitude"))
        lng = _num(loc.get("longitude") or rec.get("longitude"))
        img = rec.get("imageUrl") or rec.get("inServiceImageUrl")
        out.append({"network": "511_sfbay", "camera_id": f"sfbay-{rec.get('id')}",
                    "name": rec.get("name") or rec.get("nearbyPlace"),
                    "lat": lat, "lng": lng, "city": "San Francisco",
                    "country": "US", "image_url": img,
                    "stream_url": rec.get("recordedImageUrl")})
    return out


# --- Skylinewebcams (worldwide commercial live-cam network) ---------------
# https://www.skylinewebcams.com — a JS-light, server-rendered listing: each
# country page (/en/webcam/<country>.html) statically lists its cam cards as
#   <a href="en/webcam/<country>/<region>/<city>/<cam>.html" class="col-...">
#     <div class="cam-light"><img src="https://cdn.skylinewebcams.com/liveNNN.jpg"
#       ...><p class="tcam">Title</p><p class="subt">Desc</p></div></a>
# (hrefs are RELATIVE, no leading slash — easy to miss.) curl_cffi chrome146 via
# residential proxy clears it on the FIRST ladder rung — no Cloudflare challenge
# in the response body (verified 2026-05-21: 69 countries, ~2.1k cam URLs).
#
# GEO: Skyline exposes NO lat/lng anywhere (no schema.org GeoCoordinates, no geo
# meta, no map JSON — confirmed). The only location signal is the cam URL's
# country/region/city path segments. We geocode the unique (city, region,
# country) string via OSM Nominatim (keyless, through the proxy) to a CITY-level
# lat/lng and cache it to disk (_SKYLINE_GEO_CACHE) so geocoding runs ~once and
# every later 6h cycle is geocode-free. Cams without a resolvable city are
# dropped by normalize_cctv (lat/lng required). image_url = the https cdn
# thumbnail; stream_url = the cam page (its livee.m3u8 token is per-session
# ephemeral, so the durable entry point is the page URL).
SKYLINE_BASE = "https://www.skylinewebcams.com"
SKYLINE_INDEX = SKYLINE_BASE + "/en/webcam.html"
SKYLINE_COUNTRY = SKYLINE_BASE + "/en/webcam/{slug}.html"
# Map Skyline's localized country slug -> English country name for geocoding.
_SKYLINE_COUNTRY_NAME = {
    "united-states": "United States", "united-kingdom": "United Kingdom",
    "deutschland": "Germany", "espana": "Spain", "italia": "Italy",
    "ellada": "Greece", "hrvatska": "Croatia", "norge": "Norway",
    "schweiz": "Switzerland", "brasil": "Brazil", "slovenija": "Slovenia",
    "repubblica-di-san-marino": "San Marino", "czech-republic": "Czech Republic",
    "bosnia-and-herzegovina": "Bosnia and Herzegovina",
    "caribbean-netherlands": "Caribbean Netherlands",
    "us-virgin-islands": "U.S. Virgin Islands", "cabo-verde": "Cape Verde",
    "faroe-islands": "Faroe Islands", "sint-maarten": "Sint Maarten",
    "dominican-republic": "Dominican Republic", "el-salvador": "El Salvador",
    "costa-rica": "Costa Rica", "south-africa": "South Africa",
    "sri-lanka": "Sri Lanka", "new-jersey": "New Jersey",
}
# Skyline cam card: relative href + cdn thumbnail + title. DOTALL across the
# small <div class="cam-light"> block.
_SKYLINE_CARD_RE = re.compile(
    r'<a href="(en/webcam/[^"]+\.html)"[^>]*class="col-[^"]*"\s*>\s*'
    r'<div class="cam-light">\s*<img src="([^"]+)"[^>]*?>\s*'
    r'<p class="tcam">([^<]*)</p>', re.S)

# Disk-backed city geocode cache (place-string -> [lat, lng] or null for misses),
# alongside this module so it survives restarts and rsyncs to the launchd copy.
_SKYLINE_GEO_CACHE = Path(__file__).resolve().parent / "skyline_geocache.json"
_NOMINATIM_UA = "osiris-globe-recon/1.0 (+osiris-globe-collector; contact zac@zacstern.co)"
# Per-cycle geocode budget — never block the cctv cycle on a cold cache. Unknown
# cities resolve a few per cycle (throttled ~1 req/s for Nominatim's policy) and
# accrue over cycles; until resolved a cam is simply not yet plotted.
_SKYLINE_GEOCODE_BUDGET = 120


def _skyline_place(url):
    """(city, region, country_name, place_query) from a cam URL, or None.

    URL shape (verified): en/webcam/<country>/<region>/<city>/<cam>.html
    """
    path = url.split("/en/webcam/", 1)[-1] if "/en/webcam/" in url else url
    path = path.replace("en/webcam/", "").replace(".html", "")
    parts = [p for p in path.split("/") if p]
    if len(parts) < 4:
        return None
    country_slug, region_slug, city_slug = parts[0], parts[1], parts[2]
    def _t(s):
        return s.replace("-", " ").title()
    country = _SKYLINE_COUNTRY_NAME.get(country_slug, _t(country_slug))
    region = _SKYLINE_COUNTRY_NAME.get(region_slug, _t(region_slug))
    city = _t(city_slug)
    query = ", ".join([city, region, country])
    return city, region, country, query


def _skyline_load_geocache():
    try:
        return json.loads(_SKYLINE_GEO_CACHE.read_text())
    except Exception:
        return {}


def _skyline_save_geocache(cache):
    try:
        _SKYLINE_GEO_CACHE.write_text(json.dumps(cache))
    except Exception:
        pass


def _skyline_geocode(query, cache):
    """City-level (lat, lng) via OSM Nominatim through the proxy; cached.
    Returns None on miss (and caches the miss as null to avoid re-querying)."""
    if query in cache:
        v = cache[query]
        return (v[0], v[1]) if v else None
    url = ("https://nominatim.openstreetmap.org/search?format=json&limit=1&q="
           + quote(query))
    r = F.get(url, headers={"User-Agent": _NOMINATIM_UA,
                            "Accept": "application/json"}, timeout=30)
    if r.tier == "refused_no_proxy":
        raise RuntimeError("proxy not configured (refused_no_proxy)")
    latlng = None
    if r.status == 200 and r.body[:1] == b"[":
        try:
            d = json.loads(r.body)
            if d:
                latlng = (float(d[0]["lat"]), float(d[0]["lon"]))
        except Exception:
            latlng = None
    cache[query] = [latlng[0], latlng[1]] if latlng else None
    return latlng


async def _cctv_skyline():
    """Discover all Skyline cams worldwide, geocode their cities (cached), and
    emit normalized cctv records. Blocking I/O runs in a worker thread; the
    cctv layer is on a 6h interval so this is fine."""
    def _pull():
        from proxy_fetcher import ProxyFetcher
        import time as _time

        def _get_html(url):
            for i in range(5):
                f = ProxyFetcher(impersonate="chrome146" if i % 2 == 0
                                 else "chrome131", session=None)
                try:
                    r = f.get(url, headers={"User-Agent": _CCTV_BROWSER_UA,
                                            "Accept": "text/html"}, timeout=40)
                except Exception:
                    continue
                if r.tier == "refused_no_proxy":
                    raise RuntimeError("proxy not configured (refused_no_proxy)")
                if r.status == 200 and r.body and len(r.body) > 3000:
                    return r.text
            return None

        # 1. Country slugs from the master index (filter the obvious non-cams).
        idx = _get_html(SKYLINE_INDEX)
        if not idx:
            return []
        slugs = sorted(set(re.findall(
            r'href="/?en/webcam/([a-z0-9\-]+)\.html"', idx)))
        slugs = [s for s in slugs if s not in ("webcam",)]

        # 2. Each country page lists its cam cards (url, thumb, title). Dedup by
        #    cam URL (some cams alias across country listings).
        cams = {}
        for slug in slugs:
            html = _get_html(SKYLINE_COUNTRY.format(slug=slug))
            if not html:
                continue
            for href, thumb, title in _SKYLINE_CARD_RE.findall(html):
                cam_url = SKYLINE_BASE + "/" + href.lstrip("/")
                if cam_url not in cams:
                    cams[cam_url] = {"thumb": thumb, "title": title.strip()}

        # 3. Geocode unique cities (disk-cached); throttle cold lookups.
        cache = _skyline_load_geocache()
        budget = _SKYLINE_GEOCODE_BUDGET
        dirty = False
        out = []
        for cam_url, meta in cams.items():
            place = _skyline_place(cam_url)
            if not place:
                continue
            city, region, country, query = place
            if query not in cache and budget > 0:
                try:
                    _skyline_geocode(query, cache)
                    dirty = True
                    budget -= 1
                    _time.sleep(1.1)   # Nominatim 1 req/s usage policy
                except Exception:
                    pass
            latlng = cache.get(query)
            if not latlng:
                continue
            cam_id = cam_url.rstrip("/").rsplit("/", 1)[-1].replace(".html", "")
            out.append({"network": "skyline", "camera_id": f"skyline-{cam_id}",
                        "name": meta["title"] or city, "lat": latlng[0],
                        "lng": latlng[1], "city": city, "country": country,
                        "image_url": meta["thumb"], "stream_url": cam_url})
        if dirty:
            _skyline_save_geocache(cache)
        return out
    return await asyncio.to_thread(_pull)


# Per-network last-good cache. The cctv blob is ONE aggregate across ~20
# networks; a single flaky network returning 0 (e.g. nyctmc during a proxy-edge
# CONNECT-565 storm) would otherwise silently drop all its cameras from the live
# globe, since the aggregate still has count>0 and overwrites the blob. This
# mirrors the collector's blob-level "never blank a good blob" guarantee at
# per-network granularity: once a network has produced cameras this process
# lifetime, a later empty/failed cycle reuses the last good set instead of
# vanishing. In-memory (resets on restart); the first post-restart cycle
# re-fetches each network fresh.
_CCTV_LAST_GOOD: dict[str, list] = {}


async def fetch_cctv():
    rows = []

    # Each network isolated: a failure logs + contributes 0, never crashes.
    # On empty/failure, fall back to the network's last good result so a
    # transient outage never erases that network from the aggregate blob.
    async def _safe(label, coro):
        try:
            recs = await coro
            n = len([r for r in recs if r.get("lat") is not None
                     and r.get("lng") is not None])
            if n > 0:
                _CCTV_LAST_GOOD[label] = recs
                print(f"cctv: {label} -> {n} cams")
                return recs
            cached = _CCTV_LAST_GOOD.get(label)
            if cached:
                print(f"cctv: {label} -> 0 cams, reusing {len(cached)} cached")
                return cached
            print(f"cctv: {label} -> 0 cams (no cache)")
            return []
        except Exception as e:
            cached = _CCTV_LAST_GOOD.get(label)
            if cached:
                print(f"cctv: {label} FAIL {type(e).__name__}: {e}; "
                      f"reusing {len(cached)} cached")
                return cached
            print(f"cctv: {label} FAIL {type(e).__name__}: {e}")
            return []

    rows += await _safe("tfl_jamcams", _cctv_tfl())
    rows += await _safe("caltrans", _cctv_caltrans())

    # 511 DataTables family (restored): FL511 (Miami), 511NY (NYC), GA, PA, LA,
    # Idaho, AZ, New England.
    for slug, base, country, region, city in CCTV_DT_SITES:
        rows += await _safe(slug, _cctv_dt_site(slug, base, country, region, city))

    # CARS-program family (incl. mass511 = Boston).
    for slug, fe, gw, country, region in CARS_SITES:
        rows += await _safe(slug, _cctv_cars(slug, fe, gw, country, region))

    # City-targeted networks.
    rows += await _safe("nyctmc", _cctv_nyctmc())          # NYC metro snapshots
    rows += await _safe("idot_gateway", _cctv_idot())      # Chicago metro
    rows += await _safe("ddot_dc", _cctv_ddot_dc())        # Washington DC
    rows += await _safe("511_sfbay", _cctv_511_sfbay())    # SF Bay (env-gated)
    # 2026-05-23 Agent E sweep — 3 new bespoke sources (+ wsdot env-gated).
    rows += await _safe("midrive", _cctv_midrive())        # Michigan (~801)
    rows += await _safe("md_chart", _cctv_md_chart())      # Maryland (~451)
    rows += await _safe("wsdot", _cctv_wsdot())            # Washington State (env-gated)

    # Worldwide commercial live-cam network (city-level geocoded, disk-cached).
    rows += await _safe("skyline", _cctv_skyline())

    return normalize_cctv(rows)


# ===========================================================================
# infrastructure — OSM Overpass nuclear power plants. Point array.
# ===========================================================================
OVERPASS_QL = (
    '[out:json][timeout:90];'
    '('
    'way["plant:source"="nuclear"];'
    'relation["plant:source"="nuclear"];'
    'node["plant:source"="nuclear"];'
    'way["generator:source"="nuclear"];'
    'node["generator:source"="nuclear"];'
    ');'
    'out center 2000;'
)
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
OVERPASS_HDR = {
    "User-Agent": "globe-recon/1.0 (+osiris-globe-collector)",
    "Accept": "application/json",
    "Accept-Encoding": "gzip, deflate",
}


def normalize_infrastructure(raw):
    items = []
    for el in raw.get("elements", []):
        tags = el.get("tags") or {}
        lat, lng = el.get("lat"), el.get("lon")
        center = el.get("center")
        if (lat is None or lng is None) and isinstance(center, dict):
            lat, lng = center.get("lat"), center.get("lon")
        if lat is None or lng is None:
            continue
        items.append({"id": f"{el.get('type')}-{el.get('id')}", "lat": lat, "lng": lng,
                      "label": tags.get("name") or tags.get("name:en"),
                      "category": "nuclear_power_plant",
                      "country": tags.get("addr:country") or tags.get("country"),
                      "operator": tags.get("operator") or tags.get("operator:en"),
                      "output": tags.get("plant:output:electricity")
                      or tags.get("generator:output:electricity"),
                      "color": "#feca57"})
    return _payload("infrastructure", items)


async def fetch_infrastructure():
    last_err = None
    for base in OVERPASS_ENDPOINTS:
        try:
            url = base + "?data=" + quote(OVERPASS_QL, safe="")
            data = await _aget_json(url, headers=OVERPASS_HDR, timeout=90, tries=2)
            p = normalize_infrastructure(data)
            if p["count"]:
                return p
        except Exception as e:
            last_err = e
    if last_err:
        raise last_err
    return _payload("infrastructure", [])


# ===========================================================================
# military_bases — global military installations (every country) from OSM
# Overpass. A SINGLE worldwide `["military"]` query is too heavy: Overpass
# computes it server-side for 60-180s before any bytes flow, and the residential
# proxy drops the idle CONNECT tunnel ("connection closed abruptly", verified
# 2026-05-21). So we issue ONE FAST query PER military type and union the
# results — the small/fast types (naval_base, airfield, danger_area) return in
# seconds and reliably come back through the proxy, while the huge ubiquitous
# tags (military=base/barracks/training_area) that always time the tunnel out are
# intentionally NOT queried worldwide. Each per-type query is independently
# retried; a type that fails simply contributes nothing (resilient like
# fetch_infrastructure). Long interval (24h — installations are static).
# Verified live through ProxyRack (2026-05-21): naval_base 514, airfield 2365,
# danger_area 2923 => 5802 installations, lng -174..179 / lat -67..83 (truly
# global; samples from US, Sweden, Djibouti, Thailand, Finland, Japan).
#
# `military=*` value -> (human type, point color hint kept uniform per layer).
# Only the fast types that reliably return through the proxy AND represent ACTIVE
# installations. Deliberately excluded: military=base/barracks/training_area (too
# heavy worldwide -> proxy tunnel drops), and military=nuclear_explosion_site
# (returns ~2800 HISTORICAL detonation craters, mostly the US Nevada Test Site —
# real but noise relative to "active military bases", so it's left out).
MIL_BASE_TYPES = [
    # (osm military tag value, normalized military_type label)
    ("naval_base",    "Naval Base"),
    ("airfield",      "Military Airfield"),
    ("danger_area",   "Danger Area"),
]
MIL_BASE_QL = ('[out:json][timeout:90];'
               '(nwr["military"="{mt}"];);'
               'out center 4000;')


def normalize_military_bases(raw, military_type=None):
    """raw is an Overpass {"elements": [...]} result for ONE military type.
    Emit a point per geolocatable element (node lat/lon or way/relation center).
    `military_type` is the human label for this batch (the OSM tag value is also
    carried). Country is best-effort from OSM address tags (rarely present)."""
    items = []
    for el in raw.get("elements", []):
        tags = el.get("tags") or {}
        lat, lng = el.get("lat"), el.get("lon")
        center = el.get("center")
        if (lat is None or lng is None) and isinstance(center, dict):
            lat, lng = center.get("lat"), center.get("lon")
        if lat is None or lng is None:
            continue
        # Country: prefer an explicit OSM 2-letter country tag; otherwise derive
        # offline from lat/lng (no network — runs at import-built table speed).
        osm_cc = (tags.get("addr:country") or tags.get("country")
                  or tags.get("is_in:country"))
        code = name = None
        if osm_cc and len(str(osm_cc).strip()) == 2:
            code = str(osm_cc).strip().upper()
            _, name = country_from_latlng(lat, lng)  # name lookup; keep OSM code
            if name is None:
                name = code
        if not code:
            code, name = country_from_latlng(lat, lng)
        items.append({
            "id": f"{el.get('type')}-{el.get('id')}", "lat": lat, "lng": lng,
            "label": tags.get("name:en") or tags.get("name")
            or (military_type or "Military site"),
            "military_type": military_type or tags.get("military"),
            "osm_military": tags.get("military"),
            "country": osm_cc,
            "country_code": code, "flag": name,
            "operator": tags.get("operator") or tags.get("operator:en"),
            "color": "#c8a951"})
    return _payload("military_bases", items)


async def _mil_base_one(mt, label):
    """Fetch one military-type Overpass query across endpoints; [] on failure."""
    for base in OVERPASS_ENDPOINTS:
        url = base + "?data=" + quote(MIL_BASE_QL.format(mt=mt), safe="")
        try:
            data = await _aget_json(url, headers=OVERPASS_HDR, timeout=120, tries=2)
        except Exception:
            continue
        return normalize_military_bases(data, military_type=label)["items"]
    return []


async def fetch_military_bases():
    # Per-type queries run sequentially (Overpass throttles concurrent heavy
    # queries from one IP); each is fast on its own. Union by id, dedup.
    by_id, errors = {}, 0
    for mt, label in MIL_BASE_TYPES:
        try:
            for it in await _mil_base_one(mt, label):
                by_id[it["id"]] = it
        except Exception:
            errors += 1
    items = list(by_id.values())
    err = None
    if not items:
        err = "Overpass military_bases: all per-type queries failed"
    return _payload("military_bases", items, error=err)


# ===========================================================================
# military_naval — naval / warship / coast-guard vessels, filtered out of the
# global AIS stream. Warships frequently run AIS dark, but a meaningful number
# broadcast either ship_type=35 (AIS "Military") or a recognizable naval name
# (WARSHIP / NAVY / HNLMS / USS / FS / coast-guard prefixes). We can't read the
# boats task's in-memory vessel dict from here (it lives in that task's closure),
# so this layer takes its OWN short, time-boxed one-shot AISStream snapshot
# (env AISSTREAM_API_KEY) and applies the naval filter. Verified live
# (2026-05-21, ~20s window): ~4600 vessels seen, ~15 naval-matched (NOR WARSHIP
# F312, SWEDISH WARSHIP, HNLMS SNELLIUS, FS ETOILE, patrol craft, ...). A longer
# window catches more; the snapshot window is bounded so the cycle can't hang.
# Falls back to an UNVERIFIED TLS context ONLY for AISStream's known-expired leaf
# cert (same scoped exception the boats task already makes). No key => 0 + note.
# ===========================================================================
NAVAL_SNAPSHOT_S = 35          # seconds to collect AIS before filtering
NAVAL_FETCH_DEADLINE_S = 55    # hard wall-clock cap on the whole fetch
NAVAL_MAX = 1500               # cap on naval matches kept (reasonable upper bound)
# Recognizable naval / coast-guard name patterns (uppercased vessel name).
# Broadened across many navies' ship-name prefixes + coast-guard / patrol terms.
# Prefixes (with trailing space so they anchor as the leading token): national
# warship prefixes for the US, UK, Commonwealth, NATO, EU, Nordic, Asian, LatAm,
# and Middle-East fleets, plus generic naval/coast-guard/patrol vocabulary.
# Three independent naval signals OR'd together:
#  (a) naval / coast-guard / patrol vocabulary anywhere in the name (multi-lang);
#  (b) a known multi-letter national warship prefix as the LEADING token,
#      followed by either a name word or a pennant number (USS NIMITZ, HMS DARING,
#      FGS BAYERN, USCGC BERTHOLF, TCG ANADOLU, ITS CAVOUR, KRI ...);
#  (c) a bare single-letter+digits pennant (F312, P820, L9015, D88, M270) — a
#      strong naval signal in AIS names; single letters REQUIRE digits to avoid
#      civilian false positives.
_NAVAL_VOCAB = (
    r"WARSHIP|NAVY|NAVAL|MILITARY|"
    r"COAST ?GUARD|COASTGUARD|GUARDA ?COSTA|GARDE ?COTE|KUSTBEVAKNING|"
    r"KYSTVAKT|KUSTWACHT|KUSTENWACHE|GUARDIA COSTERA|"
    r"PATROL|OPV|"
    r"FRIGATE|CORVETTE|DESTROYER|CRUISER|SUBMARINE|MINEHUNTER|MINESWEEPER|"
    r"AMPHIBIOUS|LANDING SHIP|SUPPLY SHIP|"
    r"FREGATE|FREGATTE|FRAGATA|FREGATA"
)
# Multi-letter warship prefixes (>=2 chars) — safe to match when leading a name.
_NAVAL_PREFIX = (
    r"USS|USNS|USCGC|USCG|HMS|HMCS|HMAS|HMNZS|HNLMS|HSWMS|HDMS|HNOMS|HMNoS|"
    r"FGS|FS|ITS|ENS|KRI|FNS|ARM|BNS|ROKS|RFA|RSS|TCG|SPS|NRP|HS|HQ|CCG|JS|"
    r"PNS|BNS|KD|LE|ORP|HNMS|SAS|ROCN|RNON|RBNS"
)
_NAVAL_NAME_RE = re.compile(
    r"\b(?:" + _NAVAL_VOCAB + r")\b"
    r"|^\s*(?:" + _NAVAL_PREFIX + r")\s+[A-Z0-9]"
    r"|\b(?:" + _NAVAL_PREFIX + r"|[FPALDM])\s*\d{2,4}\b"
)
# AIS military ship-type codes treated as naval/maritime-security:
# 35 Military, 55 Law Enforcement.
_NAVAL_SHIP_TYPES = {35, 55}


def _is_naval(v):
    """True if a merged AIS vessel record looks naval/military/coast-guard."""
    try:
        if int(v.get("ship_type") or 0) in _NAVAL_SHIP_TYPES:
            return True
    except (TypeError, ValueError):
        pass
    name = (v.get("name") or "").upper().strip()
    if not name:
        return False
    return bool(_NAVAL_NAME_RE.search(name))


def normalize_military_naval(vessels):
    """vessels: iterable of merged per-MMSI AIS records (same shape the boats
    task holds). Keep only naval/military/coast-guard ones with a position.
    Emits the boats-style point shape, recolored, with a `naval` flag and the
    reason it matched (`military` ship_type vs. name pattern)."""
    items = []
    for v in vessels:
        if len(items) >= NAVAL_MAX:
            break
        if not _is_naval(v):
            continue
        lat, lng = v.get("lat"), v.get("lng")
        if lat is None or lng is None:
            continue
        mmsi = v.get("mmsi")
        name = (v.get("name") or "").strip()
        country, code = ais_country_from_mmsi(mmsi)
        is_mil_type = False
        try:
            is_mil_type = int(v.get("ship_type") or 0) in _NAVAL_SHIP_TYPES
        except (TypeError, ValueError):
            pass
        items.append({
            "id": mmsi, "lat": lat, "lng": lng,
            "label": name or str(mmsi), "name": name or None,
            "flag": country, "country_code": code,
            "ship_type": ais_ship_type_label(v.get("ship_type")),
            "match": "ship_type=military" if is_mil_type else "naval_name",
            "naval": True,
            "destination": (v.get("destination") or "").strip() or None,
            "callsign": (v.get("callsign") or "").strip() or None,
            "imo": v.get("imo") or None,
            "speed": v.get("speed"), "heading": v.get("heading"),
            "color": "#5a9bff"})
    return _payload("military_naval", items)


async def _naval_ais_snapshot(seconds):
    """One-shot AISStream collection -> {mmsi: merged record}. Bounded by
    `seconds`. Mirrors run_boats_task's ingest + scoped expired-cert TLS
    fallback. Returns {} if no key / websockets unavailable."""
    api_key = os.environ.get("AISSTREAM_API_KEY")
    if not api_key:
        return None  # signal "no key" so the caller can annotate the payload
    try:
        import websockets  # type: ignore
        import ssl, certifi  # type: ignore
    except Exception:
        return None
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    insecure_ctx = ssl.create_default_context(cafile=certifi.where())
    insecure_ctx.check_hostname = False
    insecure_ctx.verify_mode = ssl.CERT_NONE
    vessels: dict = {}
    sub = json.dumps({
        "APIKey": api_key,
        "BoundingBoxes": [[[-90, -180], [90, 180]]],
        "FilterMessageTypes": ["PositionReport", "ShipStaticData"],
    })

    def _ingest(msg):
        mt = msg.get("MessageType")
        meta = msg.get("MetaData") or {}
        body = msg.get("Message") or {}
        mmsi = meta.get("MMSI")
        if mt == "PositionReport":
            pr = body.get("PositionReport") or {}
            mmsi = mmsi or pr.get("UserID")
            lat = pr.get("Latitude") if pr.get("Latitude") is not None else meta.get("latitude")
            lng = pr.get("Longitude") if pr.get("Longitude") is not None else meta.get("longitude")
            if mmsi is None or lat is None or lng is None:
                return
            rec = vessels.get(mmsi) or {"mmsi": mmsi}
            rec.update({"mmsi": mmsi, "lat": lat, "lng": lng,
                        "speed": pr.get("Sog"),
                        "heading": pr.get("TrueHeading") if pr.get("TrueHeading") not in (None, 511)
                        else pr.get("Cog")})
            if not rec.get("name") and meta.get("ShipName"):
                rec["name"] = meta["ShipName"].strip()
            vessels[mmsi] = rec
        elif mt == "ShipStaticData":
            sd = body.get("ShipStaticData") or {}
            mmsi = mmsi or sd.get("UserID")
            if mmsi is None:
                return
            rec = vessels.get(mmsi) or {"mmsi": mmsi}
            name = (sd.get("Name") or meta.get("ShipName") or "").strip()
            if name:
                rec["name"] = name
            rec["ship_type"] = sd.get("Type") if sd.get("Type") else rec.get("ship_type")
            rec["destination"] = (sd.get("Destination") or "").strip() or rec.get("destination")
            rec["imo"] = sd.get("ImoNumber") or rec.get("imo")
            rec["callsign"] = (sd.get("CallSign") or "").strip() or rec.get("callsign")
            vessels[mmsi] = rec

    for ctx in (ssl_ctx, insecure_ctx):
        try:
            async with websockets.connect(AIS_WS_URL, ping_interval=20,
                                           max_size=None, ssl=ctx) as ws:
                await ws.send(sub)
                t0 = time.monotonic()
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    if msg.get("MessageType") in ("PositionReport", "ShipStaticData"):
                        _ingest(msg)
                    if time.monotonic() - t0 >= seconds:
                        break
            return vessels
        except Exception as e:
            # Only fall through to the insecure context for the known expired
            # AISStream leaf cert (same scoped exception as the boats task).
            if ctx is ssl_ctx and ("certificate has expired" in str(e).lower()
                                   or isinstance(e, ssl.SSLCertVerificationError)):
                continue
            return vessels  # any other error: return whatever we collected
    return vessels


async def fetch_military_naval():
    try:
        vessels = await asyncio.wait_for(
            _naval_ais_snapshot(NAVAL_SNAPSHOT_S), timeout=NAVAL_FETCH_DEADLINE_S)
    except asyncio.TimeoutError:
        vessels = {}
    if vessels is None:
        return _payload("military_naval", [],
                        error="AISSTREAM_API_KEY unset or websockets unavailable")
    return normalize_military_naval(vessels.values())


# ===========================================================================
# boats — AISStream.io global vessel positions via a long-lived WebSocket.
# AISStream is a free-key WebSocket feed (env AISSTREAM_API_KEY). The collector
# runs a persistent background task that subscribes to global PositionReport
# messages, keeps the latest position per MMSI in memory (capped + stale-evicted),
# and snapshots a normalized payload to Blob every SNAPSHOT_INTERVAL_S.
#
# This layer is NOT in the LAYERS interval list — the websocket task owns its
# own blob writes (out-of-band from the scheduler's interval fetches).
# ===========================================================================
AIS_WS_URL = "wss://stream.aisstream.io/v0/stream"
# CAP bumped 40k → 100k 2026-05-26: AISStream sees ~50-70k unique vessels
# per hour globally on terrestrial AIS, plus we're catching short-lived
# fixes that linger inside the BOATS_STALE_S window. 40k was hitting
# eviction on every snapshot cycle (visible at https://socialintelligencelabs.com/globe);
# 100k gives headroom to actually hold what the WebSocket sends instead
# of evicting freshly-received positions before they ever publish.
BOATS_CAP = 100000             # max vessels held in memory
# STALE window stretched 60m → 180m so a vessel pinging once an hour
# stays on the map across a full multi-tick browsing session. Pier-side
# tugs / fishing boats often broadcast at 5-10 min intervals only when
# moving; the longer window keeps them visible while sitting in port.
BOATS_STALE_S = 180 * 60       # evict positions older than 3h (was 60m)
BOATS_SNAPSHOT_INTERVAL_S = 30


# AIS ship-type code (0-99) -> human label. The tens digit groups the type:
# 2x WIG, 3x special (fishing/tug/dredging/diving/military/sailing/pleasure),
# 4x high-speed craft (HSC), 5x special craft (pilot/SAR/tug/...), 6x passenger,
# 7x cargo, 8x tanker, 9x other. This is the best "what it carries" proxy AIS
# offers — there is NO cargo manifest in AIS (see RECON).
_AIS_SPECIFIC = {
    30: "Fishing", 31: "Tug", 32: "Tug", 33: "Dredger", 34: "Dive Vessel",
    35: "Military", 36: "Sailing", 37: "Pleasure Craft",
    50: "Pilot Vessel", 51: "Search & Rescue", 52: "Tug", 53: "Port Tender",
    54: "Anti-pollution", 55: "Law Enforcement", 58: "Medical Transport",
    59: "Special Craft",
}
_AIS_TENS = {2: "Wing-in-Ground", 4: "High-Speed Craft", 6: "Passenger",
             7: "Cargo", 8: "Tanker", 9: "Other"}


def ais_ship_type_label(code):
    """Map a numeric AIS ship-type code to a human label (None if unknown/0)."""
    try:
        c = int(code)
    except (TypeError, ValueError):
        return None
    if c <= 0 or c > 99:
        return None
    if c in _AIS_SPECIFIC:
        return _AIS_SPECIFIC[c]
    return _AIS_TENS.get(c // 10)


# MMSI MID (first 3 digits) -> (country name, ISO-3166 alpha-2). Maritime
# Identification Digits per ITU. Covers the common maritime nations; unknown
# MIDs fall through to (None, None) so the UI just omits the flag.
_AIS_MID = {
    201: ("Albania", "AL"), 202: ("Andorra", "AD"), 203: ("Austria", "AT"),
    204: ("Azores", "PT"), 205: ("Belgium", "BE"), 206: ("Belarus", "BY"),
    207: ("Bulgaria", "BG"), 208: ("Vatican", "VA"), 209: ("Cyprus", "CY"),
    210: ("Cyprus", "CY"), 211: ("Germany", "DE"), 212: ("Cyprus", "CY"),
    213: ("Georgia", "GE"), 214: ("Moldova", "MD"), 215: ("Malta", "MT"),
    218: ("Germany", "DE"), 219: ("Denmark", "DK"), 220: ("Denmark", "DK"),
    224: ("Spain", "ES"), 225: ("Spain", "ES"), 226: ("France", "FR"),
    227: ("France", "FR"), 228: ("France", "FR"), 229: ("Malta", "MT"),
    230: ("Finland", "FI"), 231: ("Faroe Islands", "FO"), 232: ("United Kingdom", "GB"),
    233: ("United Kingdom", "GB"), 234: ("United Kingdom", "GB"), 235: ("United Kingdom", "GB"),
    236: ("Gibraltar", "GI"), 237: ("Greece", "GR"), 238: ("Croatia", "HR"),
    239: ("Greece", "GR"), 240: ("Greece", "GR"), 241: ("Greece", "GR"),
    242: ("Morocco", "MA"), 243: ("Hungary", "HU"), 244: ("Netherlands", "NL"),
    245: ("Netherlands", "NL"), 246: ("Netherlands", "NL"), 247: ("Italy", "IT"),
    248: ("Malta", "MT"), 249: ("Malta", "MT"), 250: ("Ireland", "IE"),
    251: ("Iceland", "IS"), 252: ("Liechtenstein", "LI"), 253: ("Luxembourg", "LU"),
    254: ("Monaco", "MC"), 255: ("Madeira", "PT"), 256: ("Malta", "MT"),
    257: ("Norway", "NO"), 258: ("Norway", "NO"), 259: ("Norway", "NO"),
    261: ("Poland", "PL"), 262: ("Montenegro", "ME"), 263: ("Portugal", "PT"),
    264: ("Romania", "RO"), 265: ("Sweden", "SE"), 266: ("Sweden", "SE"),
    267: ("Slovakia", "SK"), 268: ("San Marino", "SM"), 269: ("Switzerland", "CH"),
    270: ("Czech Republic", "CZ"), 271: ("Turkey", "TR"), 272: ("Ukraine", "UA"),
    273: ("Russia", "RU"), 274: ("North Macedonia", "MK"), 275: ("Latvia", "LV"),
    276: ("Estonia", "EE"), 277: ("Lithuania", "LT"), 278: ("Slovenia", "SI"),
    279: ("Serbia", "RS"),
    301: ("Anguilla", "AI"), 303: ("Alaska (USA)", "US"), 304: ("Antigua & Barbuda", "AG"),
    305: ("Antigua & Barbuda", "AG"), 306: ("Curacao", "CW"), 307: ("Aruba", "AW"),
    308: ("Bahamas", "BS"), 309: ("Bahamas", "BS"), 310: ("Bermuda", "BM"),
    311: ("Bahamas", "BS"), 312: ("Belize", "BZ"), 314: ("Barbados", "BB"),
    316: ("Canada", "CA"), 319: ("Cayman Islands", "KY"), 321: ("Costa Rica", "CR"),
    323: ("Cuba", "CU"), 325: ("Dominica", "DM"), 327: ("Dominican Republic", "DO"),
    329: ("Guadeloupe", "GP"), 330: ("Grenada", "GD"), 331: ("Greenland", "GL"),
    332: ("Guatemala", "GT"), 334: ("Honduras", "HN"), 336: ("Haiti", "HT"),
    338: ("United States", "US"), 339: ("Jamaica", "JM"), 341: ("St Kitts & Nevis", "KN"),
    343: ("St Lucia", "LC"), 345: ("Mexico", "MX"), 347: ("Martinique", "MQ"),
    348: ("Montserrat", "MS"), 350: ("Nicaragua", "NI"), 351: ("Panama", "PA"),
    352: ("Panama", "PA"), 353: ("Panama", "PA"), 354: ("Panama", "PA"),
    355: ("Panama", "PA"), 356: ("Panama", "PA"), 357: ("Panama", "PA"),
    358: ("Puerto Rico", "PR"), 359: ("El Salvador", "SV"), 361: ("St Pierre & Miquelon", "PM"),
    362: ("Trinidad & Tobago", "TT"), 364: ("Turks & Caicos", "TC"), 366: ("United States", "US"),
    367: ("United States", "US"), 368: ("United States", "US"), 369: ("United States", "US"),
    370: ("Panama", "PA"), 371: ("Panama", "PA"), 372: ("Panama", "PA"),
    373: ("Panama", "PA"), 374: ("Panama", "PA"), 375: ("St Vincent & Grenadines", "VC"),
    376: ("St Vincent & Grenadines", "VC"), 377: ("St Vincent & Grenadines", "VC"),
    378: ("British Virgin Islands", "VG"), 379: ("US Virgin Islands", "VI"),
    401: ("Afghanistan", "AF"), 403: ("Saudi Arabia", "SA"), 405: ("Bangladesh", "BD"),
    408: ("Bahrain", "BH"), 410: ("Bhutan", "BT"), 412: ("China", "CN"),
    413: ("China", "CN"), 414: ("China", "CN"), 416: ("Taiwan", "TW"),
    417: ("Sri Lanka", "LK"), 419: ("India", "IN"), 422: ("Iran", "IR"),
    423: ("Azerbaijan", "AZ"), 425: ("Iraq", "IQ"), 428: ("Israel", "IL"),
    431: ("Japan", "JP"), 432: ("Japan", "JP"), 434: ("Turkmenistan", "TM"),
    436: ("Kazakhstan", "KZ"), 437: ("Uzbekistan", "UZ"), 438: ("Jordan", "JO"),
    440: ("South Korea", "KR"), 441: ("South Korea", "KR"), 443: ("Palestine", "PS"),
    445: ("North Korea", "KP"), 447: ("Kuwait", "KW"), 450: ("Lebanon", "LB"),
    451: ("Kyrgyzstan", "KG"), 453: ("Macao", "MO"), 455: ("Maldives", "MV"),
    457: ("Mongolia", "MN"), 459: ("Nepal", "NP"), 461: ("Oman", "OM"),
    463: ("Pakistan", "PK"), 466: ("Qatar", "QA"), 468: ("Syria", "SY"),
    470: ("United Arab Emirates", "AE"), 471: ("United Arab Emirates", "AE"),
    472: ("Tajikistan", "TJ"), 473: ("Yemen", "YE"), 475: ("Yemen", "YE"),
    477: ("Hong Kong", "HK"), 478: ("Bosnia & Herzegovina", "BA"),
    501: ("Adelie Land", "FR"), 503: ("Australia", "AU"), 506: ("Myanmar", "MM"),
    508: ("Brunei", "BN"), 510: ("Micronesia", "FM"), 511: ("Palau", "PW"),
    512: ("New Zealand", "NZ"), 514: ("Cambodia", "KH"), 515: ("Cambodia", "KH"),
    516: ("Christmas Island", "CX"), 518: ("Cook Islands", "CK"), 520: ("Fiji", "FJ"),
    523: ("Cocos Islands", "CC"), 525: ("Indonesia", "ID"), 529: ("Kiribati", "KI"),
    531: ("Laos", "LA"), 533: ("Malaysia", "MY"), 536: ("Northern Mariana Islands", "MP"),
    538: ("Marshall Islands", "MH"), 540: ("New Caledonia", "NC"), 542: ("Niue", "NU"),
    544: ("Nauru", "NR"), 546: ("French Polynesia", "PF"), 548: ("Philippines", "PH"),
    550: ("East Timor", "TL"), 553: ("Papua New Guinea", "PG"), 555: ("Pitcairn Island", "PN"),
    557: ("Solomon Islands", "SB"), 559: ("American Samoa", "AS"), 561: ("Samoa", "WS"),
    563: ("Singapore", "SG"), 564: ("Singapore", "SG"), 565: ("Singapore", "SG"),
    566: ("Singapore", "SG"), 567: ("Thailand", "TH"), 570: ("Tonga", "TO"),
    572: ("Tuvalu", "TV"), 574: ("Vietnam", "VN"), 576: ("Vanuatu", "VU"),
    577: ("Vanuatu", "VU"), 578: ("Wallis & Futuna", "WF"),
    601: ("South Africa", "ZA"), 603: ("Angola", "AO"), 605: ("Algeria", "DZ"),
    607: ("St Paul & Amsterdam Islands", "FR"), 608: ("Ascension Island", "SH"),
    609: ("Burundi", "BI"), 610: ("Benin", "BJ"), 611: ("Botswana", "BW"),
    612: ("Central African Republic", "CF"), 613: ("Cameroon", "CM"), 615: ("Congo", "CG"),
    616: ("Comoros", "KM"), 617: ("Cape Verde", "CV"), 618: ("Crozet Archipelago", "FR"),
    619: ("Ivory Coast", "CI"), 620: ("Comoros", "KM"), 621: ("Djibouti", "DJ"),
    622: ("Egypt", "EG"), 624: ("Ethiopia", "ET"), 625: ("Eritrea", "ER"),
    626: ("Gabon", "GA"), 627: ("Ghana", "GH"), 629: ("Gambia", "GM"),
    630: ("Guinea-Bissau", "GW"), 631: ("Equatorial Guinea", "GQ"), 632: ("Guinea", "GN"),
    633: ("Burkina Faso", "BF"), 634: ("Kenya", "KE"), 635: ("Kerguelen Islands", "FR"),
    636: ("Liberia", "LR"), 637: ("Liberia", "LR"), 638: ("South Sudan", "SS"),
    642: ("Libya", "LY"), 644: ("Lesotho", "LS"), 645: ("Mauritius", "MU"),
    647: ("Madagascar", "MG"), 649: ("Mali", "ML"), 650: ("Mozambique", "MZ"),
    654: ("Mauritania", "MR"), 655: ("Malawi", "MW"), 656: ("Niger", "NE"),
    657: ("Nigeria", "NG"), 659: ("Namibia", "NA"), 660: ("Reunion", "RE"),
    661: ("Rwanda", "RW"), 662: ("Sudan", "SD"), 663: ("Senegal", "SN"),
    664: ("Seychelles", "SC"), 665: ("St Helena", "SH"), 666: ("Somalia", "SO"),
    667: ("Sierra Leone", "SL"), 668: ("Sao Tome & Principe", "ST"), 669: ("Eswatini", "SZ"),
    670: ("Chad", "TD"), 671: ("Togo", "TG"), 672: ("Tunisia", "TN"),
    674: ("Tanzania", "TZ"), 675: ("Uganda", "UG"), 676: ("DR Congo", "CD"),
    677: ("Tanzania", "TZ"), 678: ("Zambia", "ZM"), 679: ("Zimbabwe", "ZW"),
    701: ("Argentina", "AR"), 710: ("Brazil", "BR"), 720: ("Bolivia", "BO"),
    725: ("Chile", "CL"), 730: ("Colombia", "CO"), 735: ("Ecuador", "EC"),
    740: ("Falkland Islands", "FK"), 745: ("French Guiana", "GF"), 750: ("Guyana", "GY"),
    755: ("Paraguay", "PY"), 760: ("Peru", "PE"), 765: ("Suriname", "SR"),
    770: ("Uruguay", "UY"), 775: ("Venezuela", "VE"),
}


def ais_country_from_mmsi(mmsi):
    """Derive (country_name, alpha2) from an MMSI's MID (first 3 digits)."""
    try:
        s = str(int(mmsi))
    except (TypeError, ValueError):
        return (None, None)
    if len(s) < 3:
        return (None, None)
    return _AIS_MID.get(int(s[:3]), (None, None))


def _ais_eta(eta):
    """Format a ShipStaticData Eta dict {Month,Day,Hour,Minute} as MM-DD HH:MM.
    AIS ETA carries no year; 0/0/0/0 means 'not available'."""
    if not isinstance(eta, dict):
        return None
    mo, d, h, mi = eta.get("Month"), eta.get("Day"), eta.get("Hour"), eta.get("Minute")
    if not mo or not d:
        return None
    try:
        return "%02d-%02d %02d:%02d" % (int(mo), int(d), int(h or 0), int(mi or 0))
    except (TypeError, ValueError):
        return None


def _ais_dims(dim):
    """Derive (length_m, width_m) from a ShipStaticData Dimension {A,B,C,D}.
    Length = A+B (bow+stern from antenna), Width = C+D (port+starboard)."""
    if not isinstance(dim, dict):
        return (None, None)
    a, b, c, d = (dim.get("A"), dim.get("B"), dim.get("C"), dim.get("D"))
    length = (a or 0) + (b or 0) if (a or b) else None
    width = (c or 0) + (d or 0) if (c or d) else None
    return (length or None, width or None)


def normalize_boats(vessels):
    """vessels: iterable of merged per-MMSI records carrying position
    (lat/lng/speed/heading) from PositionReport and static fields from
    ShipStaticData. Emits the rich globe-point shape with flag/type/destination.

    AIS carries NO cargo manifest — ship_type (Cargo/Tanker/Passenger/...) is the
    best available proxy for "what it's carrying" and is included as such.
    """
    items = []
    for v in vessels:
        lat, lng = v.get("lat"), v.get("lng")
        if lat is None or lng is None:
            continue
        mmsi = v.get("mmsi")
        name = (v.get("name") or "").strip()
        country, code = ais_country_from_mmsi(mmsi)
        length, width = _ais_dims(v.get("dimension"))
        size = None
        if length:
            size = ("%dx%dm" % (length, width)) if width else ("%dm" % length)
        items.append({
            "id": mmsi, "lat": lat, "lng": lng,
            "label": name or str(mmsi), "name": name or None,
            "flag": country, "country_code": code,
            "ship_type": ais_ship_type_label(v.get("ship_type")),
            "destination": (v.get("destination") or "").strip() or None,
            "draught": v.get("draught"),
            "size": size,
            "callsign": (v.get("callsign") or "").strip() or None,
            "imo": v.get("imo") or None,
            "eta": v.get("eta"),
            "speed": v.get("speed"), "heading": v.get("heading"),
            "color": "#48dbfb",
        })
    return _payload("boats", items)


async def run_boats_task(put_blob, log):
    """Long-lived AISStream consumer. No-op (single info log) if no key set.

    `put_blob(layer, payload)` is the collector's blob writer (token-bound);
    `log` is the collector's logger. Runs forever, reconnecting on drop.
    """
    api_key = os.environ.get("AISSTREAM_API_KEY")
    if not api_key:
        log.info("boats: AISSTREAM_API_KEY unset, skipping")
        return
    try:
        import websockets  # type: ignore
    except Exception:
        log.warning("boats: websockets package not importable, skipping")
        return

    # SSL contexts for the wss:// endpoint.
    #
    # Default ("verified"): certifi's fresh CA bundle, full verification ON.
    #
    # Fallback ("insecure"): AISStream's own LEAF certificate is currently
    # EXPIRED server-side (CN=stream.aisstream.io, Let's Encrypt R12,
    # notAfter=May 20 10:59:33 2026 GMT). certifi cannot help — the expiry is
    # on their server, not in our trust store. Because this is a PUBLIC AIS
    # broadcast stream and the only credential is our low-value AIS key, we fall
    # back to an UNVERIFIED context scoped to THIS websocket only when (and only
    # when) the verified handshake fails with a "certificate has expired" error.
    # We periodically re-attempt the verified context so this self-heals the
    # moment AISStream renews their cert. Verification is NEVER disabled globally
    # or for any other layer.
    import ssl, certifi  # type: ignore
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    insecure_ctx = ssl.create_default_context(cafile=certifi.where())
    insecure_ctx.check_hostname = False
    insecure_ctx.verify_mode = ssl.CERT_NONE

    # Fallback state: once we hit the expired-cert error we stick to the
    # insecure context, but retry the verified one every BOATS_TLS_REVERIFY_S
    # so we re-secure automatically after they renew.
    BOATS_TLS_REVERIFY_S = 600  # ~10 min
    use_insecure = False
    last_verified_attempt = 0.0
    warned_insecure = False

    def _is_expired_cert_error(exc: Exception) -> bool:
        if isinstance(exc, ssl.SSLCertVerificationError):
            return True
        return "certificate has expired" in str(exc).lower()

    # Per-MMSI merged record. Position fields (lat/lng/speed/heading/ts) come
    # from PositionReport; static fields (name/ship_type/destination/draught/
    # dimension/imo/callsign/eta) come from ShipStaticData. We merge both message
    # types into one record keyed by MMSI so each boat carries everything we've
    # seen for it. `ts` only advances on position updates (drives staleness).
    vessels: dict = {}

    # ----- Restart-seed: prime `vessels` from the previously-published boats
    # blob so a collector restart doesn't drop the count to 0 and force a full
    # ~hour refill from the live stream. Each seeded record gets a ts ~1 min old
    # (well within BOATS_STALE_S) so it survives a few snapshot cycles but is
    # naturally overwritten as real PositionReport / ShipStaticData arrive. We
    # deliberately skip ship_type — the published value is a LABEL ("Cargo")
    # while the in-memory path expects the raw AIS int code; the first
    # ShipStaticData for the vessel re-populates it correctly.
    try:
        from forecast import fetch_layer
        prev = fetch_layer("boats")
    except Exception as e:
        prev = None
        log.info("boats: skip restart-seed (%s)", e)
    if prev and isinstance(prev, dict):
        seed_ts = time.time() - 60
        seeded = 0
        for it in (prev.get("items") or []):
            mmsi = it.get("id")
            if mmsi is None or it.get("lat") is None or it.get("lng") is None:
                continue
            rec = {"mmsi": mmsi, "lat": it["lat"], "lng": it["lng"],
                   "ts": seed_ts}
            for src in ("name", "destination", "callsign", "imo", "draught",
                        "speed", "heading"):
                v = it.get(src)
                if v not in (None, ""):
                    rec[src] = v
            vessels[mmsi] = rec
            seeded += 1
        log.info("boats: seeded %d vessels from previous blob", seeded)

    sub = json.dumps({
        "APIKey": api_key,
        "BoundingBoxes": [[[-90, -180], [90, 180]]],
        "FilterMessageTypes": ["PositionReport", "ShipStaticData"],
    })

    def _ingest(msg):
        """Merge one AIS message into the per-MMSI record."""
        mt = msg.get("MessageType")
        meta = msg.get("MetaData") or {}
        body = msg.get("Message") or {}
        mmsi = meta.get("MMSI")
        if mt == "PositionReport":
            pr = body.get("PositionReport") or {}
            mmsi = mmsi or pr.get("UserID")
            lat = pr.get("Latitude") if pr.get("Latitude") is not None else meta.get("latitude")
            lng = pr.get("Longitude") if pr.get("Longitude") is not None else meta.get("longitude")
            if mmsi is None or lat is None or lng is None:
                return
            rec = vessels.get(mmsi) or {"mmsi": mmsi}
            rec.update({
                "mmsi": mmsi, "lat": lat, "lng": lng,
                "speed": pr.get("Sog"),
                "heading": pr.get("TrueHeading") if pr.get("TrueHeading") not in (None, 511)
                else pr.get("Cog"),
                "ts": time.time(),
            })
            # ShipName from metadata is a useful fallback before static data lands.
            if not rec.get("name") and meta.get("ShipName"):
                rec["name"] = meta["ShipName"].strip()
            vessels[mmsi] = rec
        elif mt == "ShipStaticData":
            sd = body.get("ShipStaticData") or {}
            mmsi = mmsi or sd.get("UserID")
            if mmsi is None:
                return
            rec = vessels.get(mmsi) or {"mmsi": mmsi}
            name = (sd.get("Name") or meta.get("ShipName") or "").strip()
            if name:
                rec["name"] = name
            rec.update({
                "ship_type": sd.get("Type") if sd.get("Type") else rec.get("ship_type"),
                "destination": (sd.get("Destination") or "").strip() or rec.get("destination"),
                "draught": sd.get("MaximumStaticDraught") or rec.get("draught"),
                "dimension": sd.get("Dimension") or rec.get("dimension"),
                "imo": sd.get("ImoNumber") or rec.get("imo"),
                "callsign": (sd.get("CallSign") or "").strip() or rec.get("callsign"),
                "eta": _ais_eta(sd.get("Eta")) or rec.get("eta"),
            })
            # Static-only vessels (seen before any position) get no ts and are
            # not snapshotted until a PositionReport supplies coordinates.
            vessels[mmsi] = rec

    async def _snapshotter():
        while True:
            await asyncio.sleep(BOATS_SNAPSHOT_INTERVAL_S)
            now = time.time()
            for m in [m for m, v in vessels.items()
                      if now - v.get("ts", 0) > BOATS_STALE_S]:
                vessels.pop(m, None)
            if len(vessels) > BOATS_CAP:  # evict oldest
                for m, _v in sorted(vessels.items(),
                                    key=lambda kv: kv[1].get("ts", 0))[:len(vessels) - BOATS_CAP]:
                    vessels.pop(m, None)
            payload = normalize_boats(vessels.values())
            try:
                await asyncio.to_thread(put_blob, "boats", payload)
                log.info("boats OK %d", payload["count"])
            except Exception as e:
                log.warning("boats snapshot FAIL %s", e)

    snap = asyncio.create_task(_snapshotter())
    try:
        while True:
            # Decide which context to use this attempt. If we're in insecure
            # fallback mode, periodically retry the verified context so we
            # re-secure automatically once AISStream renews their cert.
            now = time.time()
            if use_insecure and (now - last_verified_attempt) >= BOATS_TLS_REVERIFY_S:
                use_insecure = False  # give verified another shot
            attempt_ctx = insecure_ctx if use_insecure else ssl_ctx
            if not use_insecure:
                last_verified_attempt = now
            try:
                async with websockets.connect(AIS_WS_URL, ping_interval=20,
                                               max_size=None, ssl=attempt_ctx) as ws:
                    await ws.send(sub)
                    if use_insecure:
                        log.info("boats: AISStream connected (UNVERIFIED TLS fallback)")
                    else:
                        if warned_insecure:
                            log.info("boats: AISStream cert verified again — back to secure TLS")
                            warned_insecure = False
                        log.info("boats: AISStream connected")
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue
                        if msg.get("MessageType") not in ("PositionReport", "ShipStaticData"):
                            continue
                        _ingest(msg)
            except Exception as e:
                # Scoped insecure fallback: ONLY for the AISStream expired-cert
                # case, ONLY for this public AIS websocket.
                if not use_insecure and _is_expired_cert_error(e):
                    use_insecure = True
                    if not warned_insecure:
                        warned_insecure = True
                        log.warning(
                            "boats: AISStream server cert is EXPIRED (their side) — "
                            "falling back to UNVERIFIED TLS for this public AIS feed; "
                            "will re-verify automatically once they renew."
                        )
                    # Immediate reconnect with the insecure context.
                    continue
                log.warning("boats: AISStream disconnected (%s); reconnecting in 5s", e)
                await asyncio.sleep(5)
    finally:
        snap.cancel()


# ===========================================================================
# military_air — adsb.lol /v2/mil: ALL military aircraft broadcasting ADS-B
# worldwide (keyless, unlimited). This is the escalation-detection blind-spot
# fix: FR24 strips MILITARY aircraft, so the main `flights` layer under-reports
# tankers / recon / gunships / drones. adsb.lol's mil feed exposes them directly
# with the same adsbexchange-compatible v2 schema (lat/lon/alt_baro/hex/flight/
# t/r/gs/track/squawk/dbFlags). adsb.fi mirror is the fallback. Verified live
# through ProxyRack (2026-05-21): 222 aircraft, global lng -169..+176, Caribbean
# /Cuba region present. ~15s interval keeps it near-real-time alongside flights.
# ===========================================================================
# Keyless military ADS-B mirrors (adsbexchange-compatible v2 schema). UNIONED by
# hex per cycle for max coverage — each mirror sees a slightly different slice of
# receivers, so the union (~210-230) beats any single feed (~177-189). adsb.one
# is Cloudflare-blocked and theairtraffic returns empty through the proxy
# (verified 2026-05-21), so they are intentionally not in the list.
MIL_AIR_URLS = [
    "https://api.adsb.lol/v2/mil",
    "https://opendata.adsb.fi/api/v2/mil",
    "https://api.airplanes.live/v2/mil",
]
MIL_AIR_HDR = {"Accept": "application/json", "User-Agent": "globe-recon/1.0"}

# ---------------------------------------------------------------------------
# Military aircraft role classification from the ICAO type designator (`t`).
# Roles: fighter / bomber / tanker / transport / ISR / helicopter / drone /
# trainer / patrol. Exact-match set first, then prefix match, then a small
# heuristic on the type string. Unknown -> None (the UI just omits the role).
# ---------------------------------------------------------------------------
_MIL_ROLE_TYPES = {
    # fighters / attack
    "F16": "fighter", "F15": "fighter", "F18": "fighter", "F/A-18": "fighter",
    "F22": "fighter", "F35": "fighter", "F35A": "fighter", "F35B": "fighter",
    "F2": "fighter", "F5": "fighter", "F4": "fighter", "EUFI": "fighter",
    "TYPH": "fighter", "TOR": "fighter", "RFAL": "fighter", "EF2000": "fighter",
    "MIG29": "fighter", "MIG31": "fighter", "SU27": "fighter", "SU30": "fighter",
    "SU34": "fighter", "SU35": "fighter", "J10": "fighter", "JAS39": "fighter",
    "GR4": "fighter", "A10": "fighter", "AV8B": "fighter", "HARR": "fighter",
    "FA50": "fighter", "M2000": "fighter", "MIR2": "fighter",
    # bombers
    "B52": "bomber", "B1": "bomber", "B2": "bomber", "B21": "bomber",
    "TU95": "bomber", "TU22": "bomber", "TU160": "bomber", "H6": "bomber",
    # tankers
    "K35R": "tanker", "KC35": "tanker", "KC10": "tanker", "KC46": "tanker",
    "KE3": "tanker", "A332": "tanker", "MRTT": "tanker", "VC10": "tanker",
    "K30": "tanker", "KC30": "tanker", "TANK": "tanker", "IL78": "tanker",
    # transport / cargo
    "C17": "transport", "C5": "transport", "C5M": "transport",
    "C130": "transport", "C30J": "transport", "C30": "transport",
    "C160": "transport", "C27J": "transport", "C295": "transport",
    "CN35": "transport", "A400": "transport", "A400M": "transport",
    "IL76": "transport", "AN12": "transport", "AN26": "transport",
    "AN124": "transport", "AN225": "transport", "C2": "transport",
    "C40": "transport", "B762": "transport", "B763": "transport",
    "C141": "transport", "C123": "transport", "DHC6": "transport",
    "BE20": "transport", "B350": "transport", "C12": "transport",
    # VIP / government transport jets frequently in mil feeds
    "B737": "transport", "B736": "transport", "B738": "transport",
    "B739": "transport", "B752": "transport", "B77W": "transport",
    "A320": "transport", "SW4": "transport", "D328": "transport",
    "B190": "transport",
    "F900": "ISR", "GLF4": "ISR", "GLF6": "ISR", "E50P": "ISR",
    "W135": "ISR", "TWR": "trainer",
    "A189": "helicopter", "A169": "helicopter", "B407": "helicopter",
    "B429": "helicopter", "EC75": "helicopter", "S92": "helicopter",
    # ISR / surveillance / EW / AEW
    "E3": "ISR", "E3CF": "ISR", "E3TF": "ISR", "E737": "ISR", "E767": "ISR",
    "E2": "ISR", "E2D": "ISR", "RC135": "ISR", "R135": "ISR", "RC12": "ISR",
    "U2": "ISR", "E8": "ISR", "E6": "ISR", "P8": "patrol", "P3": "patrol",
    "EP3": "ISR", "EC30": "ISR", "RJ85": "ISR", "GLF5": "ISR", "C560": "ISR",
    "MC12": "ISR", "DH8D": "ISR", "SENT": "ISR", "A319": "ISR",
    # maritime patrol
    "P1": "patrol", "AT3": "patrol", "AT72": "patrol", "ATP": "patrol",
    "CL60": "patrol", "F406": "patrol",
    # trainers
    "TEX2": "trainer", "T6": "trainer", "T38": "trainer", "T1": "trainer",
    "PC21": "trainer", "PC9": "trainer", "PC7": "trainer", "HAWK": "trainer",
    "M345": "trainer", "M346": "trainer", "T45": "trainer", "BE9L": "trainer",
    "SF26": "trainer", "G120": "trainer", "DA42": "trainer", "T2": "trainer",
    "PC12": "transport",
    # helicopters
    "H60": "helicopter", "UH60": "helicopter", "H64": "helicopter",
    "AH64": "helicopter", "H47": "helicopter", "CH47": "helicopter",
    "H53": "helicopter", "CH53": "helicopter", "H1": "helicopter",
    "UH1": "helicopter", "H72": "helicopter", "H6K": "helicopter",
    "A119": "helicopter", "A109": "helicopter", "A139": "helicopter",
    "AS65": "helicopter", "B212": "helicopter", "B412": "helicopter",
    "EC35": "helicopter", "EC45": "helicopter", "H145": "helicopter",
    "NH90": "helicopter", "EH10": "helicopter", "MERL": "helicopter",
    "LYNX": "helicopter", "PUMA": "helicopter", "TIGR": "helicopter",
    "MI8": "helicopter", "MI17": "helicopter", "MI24": "helicopter",
    "MI28": "helicopter", "KA52": "helicopter", "AW39": "helicopter",
    "H500": "helicopter", "R44": "helicopter", "S70": "helicopter",
    # drones / UAS
    "RQ4": "drone", "MQ9": "drone", "MQ4": "drone", "MQ1": "drone",
    "GLOB": "drone", "REAP": "drone", "BAYR": "drone", "TB2": "drone",
    "Q4": "drone", "UAV": "drone",
}
# Prefix match (covers families: F1x fighters, KC tankers, CH/UH helos, ...).
_MIL_ROLE_PREFIXES = (
    ("MQ", "drone"), ("RQ", "drone"),
    ("KC", "tanker"),
    ("CH", "helicopter"), ("UH", "helicopter"), ("AH", "helicopter"),
    ("H6", "bomber"),  # H6 bombers; H60/H64 caught by exact set above
)


def mil_air_role(type_code):
    """Classify a military aircraft into a role from its ICAO type designator.
    Returns one of fighter/bomber/tanker/transport/ISR/helicopter/drone/
    trainer/patrol, or None if the type is unknown/blank."""
    if not type_code:
        return None
    t = str(type_code).upper().strip()
    if not t:
        return None
    if t in _MIL_ROLE_TYPES:
        return _MIL_ROLE_TYPES[t]
    # exact set may miss family variants -> prefix heuristics
    if t.startswith(("F1", "F2", "F3", "F5", "MIG", "SU", "JAS", "MIR", "RFAL")):
        return "fighter"
    if t.startswith(("KC", "K35")):
        return "tanker"
    if t.startswith(("CH", "UH", "AH", "MH", "HH", "MI", "KA", "EC", "AS", "AW",
                     "NH", "EH")):
        return "helicopter"
    if t.startswith(("RQ", "MQ")):
        return "drone"
    if t.startswith(("C1", "C2", "C3", "C4", "A40", "IL7", "AN")):
        return "transport"
    if t.startswith(("RC", "EC", "E3", "E7", "E8")):
        return "ISR"
    if t.startswith(("P3", "P8", "P1")):
        return "patrol"
    if t.startswith(("T1", "T2", "T6", "T38", "T45", "PC", "M34", "HAWK")):
        return "trainer"
    if t.startswith(("B5", "TU")):
        return "bomber"
    return None


def normalize_military_air(raw):
    """raw is {"ac": [...]} of adsb v2 military aircraft. Every item is a point;
    all are category=military (the feed is the authoritative mil membership).
    Each carries country_code (ISO2) + flag (country name) derived from the ICAO
    hex (registration fallback), and a `role` from the type designator."""
    items = []
    for a in (raw.get("ac") or raw.get("aircraft") or []):
        lat, lng = a.get("lat"), a.get("lon")
        if lat is None or lng is None:
            continue
        hexaddr = a.get("hex") or a.get("icao")
        reg = a.get("r")
        code, name = country_from_aircraft(hexaddr, reg)
        items.append({"id": a.get("hex"), "lat": lat, "lng": lng,
                      "label": (a.get("flight") or "").strip() or a.get("r")
                      or a.get("hex"),
                      "callsign": (a.get("flight") or "").strip() or None,
                      "type": a.get("t"), "reg": a.get("r"),
                      "role": mil_air_role(a.get("t")),
                      "country_code": code, "flag": name,
                      "alt": a.get("alt_baro"), "speed": a.get("gs"),
                      "track": a.get("track"), "squawk": a.get("squawk"),
                      "category": "military", "color": "#ff5a5a"})
    return _payload("military_air", items)


async def _mil_air_one(url):
    """Fetch one mil mirror; return its ac[] list ([] on failure)."""
    try:
        data = await _aget_json(url, headers=MIL_AIR_HDR, timeout=30, tries=2)
    except Exception:
        return []
    return data.get("ac") or data.get("aircraft") or []


async def fetch_military_air():
    """Union ALL keyless mil mirrors by hex for maximum coverage. Each mirror is
    fetched concurrently; a failed mirror simply contributes nothing. The union
    is deduped by hex (keeping the first/richest record). Errors only if EVERY
    mirror failed/empty."""
    results = await asyncio.gather(*(_mil_air_one(u) for u in MIL_AIR_URLS))
    by_hex = {}
    contributing = 0
    for ac_list in results:
        if ac_list:
            contributing += 1
        for a in ac_list:
            h = (a.get("hex") or "").lower()
            if not h:
                continue
            if h not in by_hex:
                by_hex[h] = a
            else:
                # merge a missing type/reg from a later mirror (richer record)
                cur = by_hex[h]
                if not cur.get("t") and a.get("t"):
                    cur["t"] = a["t"]
                if not cur.get("r") and a.get("r"):
                    cur["r"] = a["r"]
    p = normalize_military_air({"ac": list(by_hex.values())})
    p["source"] = "union(%d feeds)" % contributing
    p["sources"] = [u.split("/v2/")[0].split("//")[-1] for u in MIL_AIR_URLS]
    if not p["count"] and contributing == 0:
        p["error"] = "all military mirrors failed/empty"
    return p


# ===========================================================================
# events — GDELT 2.0 global conflict/military events.
# The GEO 2.0 API (/api/v2/geo/geo) currently 404s at GDELT's edge (verified
# 2026-05-21 across hosts/formats — Apache 404, NOT a proxy block; the DOC API
# on the same host works fine), so we use the DOC 2.0 artlist API and geolocate
# each article by its `sourcecountry` to a country centroid. This yields
# geolocated conflict/military news points worldwide with a headline + a tone
# (risk) score reused from the news keyword scorer. GDELT rate-limits to 1
# request / 5s and returns a PLAINTEXT 429 ("Please limit requests...") rather
# than JSON, so the fetch tolerates non-JSON bodies and issues ONE successful
# request per cycle (15-min interval => well within the limit).
#
# Fix (2026-05-21): the layer was intermittently returning 0 items. Root cause
# was NOT the query or geolocation — it was the cold-tunnel proxy flake (the
# first CONNECT of a cycle often returns status -1/0). The old fetch only tried
# twice with no backoff, so two cold connects in a row produced a silent 0. The
# fetch now retries up to 4x with backoff and surfaces an `error` on total
# failure; the query was also broadened (more volume) and given a browser UA.
# Verified live through ProxyRack: ~230-250 articles geolocated across 20+
# reporting countries per cycle.
# ===========================================================================
# Broad conflict/military terms restricted to English sources. This returns far
# more volume than the previous narrow set (~250 articles/cycle vs. flaky low
# counts) and `sourcelang:eng` keeps the headlines readable. GDELT's DOC API has
# no per-article geocoding, so we still place each article at its
# `sourcecountry` centroid (see normalize_events).
EVENTS_QUERY = ("(military OR strike OR attack OR invasion OR troops OR missile "
                "OR airstrike OR clash OR offensive OR deploy) sourcelang:eng")
EVENTS_URL = ("https://api.gdeltproject.org/api/v2/doc/doc?query={q}"
              "&mode=artlist&format=json&timespan=24h&maxrecords=250&sort=datedesc")
# A real browser UA — GDELT's edge is friendlier to it than a bare bot UA, and it
# avoids occasional empty/HTML responses seen with the minimal globe-recon UA.
EVENTS_HDR = {
    "Accept": "application/json",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/146.0.0.0 Safari/537.36"),
}

# Country name (GDELT `sourcecountry`) -> (lat, lng) centroid. GDELT reports the
# country of the news SOURCE, so this places each event at its reporting nation.
# Covers the common conflict-reporting nations; unknown names are skipped.
_COUNTRY_CENTROID = {
    "United States": (39.8, -98.6), "United Kingdom": (54.0, -2.0),
    "Ukraine": (49.0, 32.0), "Russia": (61.5, 90.0), "Germany": (51.2, 10.4),
    "France": (46.6, 2.2), "Italy": (42.8, 12.6), "Spain": (40.2, -3.7),
    "Poland": (52.1, 19.4), "Romania": (45.9, 25.0), "Turkey": (39.0, 35.2),
    "Greece": (39.1, 22.0), "Israel": (31.4, 35.0), "Palestine": (31.9, 35.2),
    "Lebanon": (33.9, 35.9), "Syria": (35.0, 38.0), "Iran": (32.4, 53.7),
    "Iraq": (33.2, 43.7), "Saudi Arabia": (24.0, 45.0), "Yemen": (15.6, 47.6),
    "Qatar": (25.3, 51.2), "United Arab Emirates": (24.0, 54.0),
    "Egypt": (26.8, 30.8), "Libya": (27.0, 17.3), "Sudan": (15.5, 30.2),
    "South Sudan": (7.3, 30.3), "Somalia": (5.2, 46.2), "Ethiopia": (9.1, 40.5),
    "Nigeria": (9.1, 8.7), "Mali": (17.6, -4.0), "Mauritania": (21.0, -10.9),
    "Niger": (17.6, 8.1), "Chad": (15.5, 18.7), "Burkina Faso": (12.2, -1.6),
    "Democratic Republic of the Congo": (-4.0, 21.8), "DR Congo": (-4.0, 21.8),
    "China": (35.9, 104.2), "Taiwan": (23.7, 121.0), "Japan": (36.2, 138.3),
    "South Korea": (36.5, 127.8), "North Korea": (40.3, 127.5),
    "India": (22.4, 78.9), "Pakistan": (30.4, 69.3), "Afghanistan": (33.9, 67.7),
    "Myanmar": (21.9, 95.9), "Philippines": (12.9, 121.8), "Indonesia": (-2.5, 118.0),
    "Brazil": (-10.8, -52.9), "Mexico": (23.6, -102.5), "Colombia": (4.6, -74.3),
    "Venezuela": (6.4, -66.6), "Cuba": (21.5, -79.0), "Haiti": (19.1, -72.3),
    "Canada": (56.1, -106.3), "Australia": (-25.3, 133.8),
    "Azerbaijan": (40.1, 47.6), "Armenia": (40.1, 45.0), "Georgia": (42.3, 43.4),
    "Belarus": (53.7, 28.0), "Moldova": (47.4, 28.4), "Serbia": (44.0, 21.0),
    "Kosovo": (42.6, 20.9), "Bosnia and Herzegovina": (44.0, 18.0),
    "Netherlands": (52.1, 5.3), "Belgium": (50.5, 4.5), "Switzerland": (46.8, 8.2),
    "Austria": (47.6, 14.1), "Sweden": (62.0, 15.0), "Norway": (60.5, 8.5),
    "Finland": (64.0, 26.0), "Denmark": (56.0, 9.5), "Ireland": (53.2, -8.0),
    "Portugal": (39.6, -8.0), "Czech Republic": (49.8, 15.5), "Hungary": (47.2, 19.5),
    "Bulgaria": (42.7, 25.5), "Slovakia": (48.7, 19.7), "Croatia": (45.1, 15.2),
    "Lithuania": (55.2, 23.9), "Latvia": (56.9, 24.6), "Estonia": (58.6, 25.0),
    "South Africa": (-30.6, 22.9), "Kenya": (0.0, 37.9), "Algeria": (28.0, 1.7),
    "Morocco": (31.8, -7.1), "Tunisia": (33.9, 9.6), "Jordan": (31.3, 36.2),
    "Kuwait": (29.3, 47.5), "Bahrain": (26.0, 50.6), "Oman": (21.5, 55.9),
    "Bangladesh": (23.7, 90.4), "Sri Lanka": (7.9, 80.8), "Nepal": (28.4, 84.1),
    "Thailand": (15.9, 101.0), "Vietnam": (16.2, 107.8), "Malaysia": (4.2, 101.9),
    "Singapore": (1.35, 103.8), "Cambodia": (12.6, 104.9), "Laos": (19.9, 102.5),
    "Argentina": (-38.4, -63.6), "Chile": (-35.7, -71.5), "Peru": (-9.2, -75.0),
    "Ecuador": (-1.8, -78.2), "New Zealand": (-41.0, 174.0),
    # Added 2026-05-21 — small/island and additional reporting nations seen in
    # the GDELT `sourcecountry` field that were previously dropped.
    "Grenada": (12.1, -61.7), "Jamaica": (18.1, -77.3), "Trinidad and Tobago": (10.5, -61.3),
    "Bahamas": (24.5, -76.5), "Barbados": (13.2, -59.5), "Guyana": (4.9, -58.9),
    "Panama": (8.5, -80.0), "Costa Rica": (9.7, -83.8), "Guatemala": (15.5, -90.3),
    "Honduras": (15.0, -86.5), "Nicaragua": (12.9, -85.2), "El Salvador": (13.8, -88.9),
    "Dominican Republic": (18.7, -70.2), "Puerto Rico": (18.2, -66.5),
    "Bolivia": (-16.3, -64.0), "Paraguay": (-23.4, -58.4), "Uruguay": (-32.5, -55.8),
    "Iceland": (64.9, -19.0), "Luxembourg": (49.8, 6.1), "Slovenia": (46.1, 14.8),
    "North Macedonia": (41.6, 21.7), "Albania": (41.2, 20.2), "Montenegro": (42.7, 19.4),
    "Cyprus": (35.1, 33.4), "Malta": (35.9, 14.4),
    "Kazakhstan": (48.0, 67.0), "Uzbekistan": (41.4, 64.6), "Turkmenistan": (38.9, 59.5),
    "Kyrgyzstan": (41.2, 74.8), "Tajikistan": (38.9, 71.3), "Mongolia": (46.9, 103.8),
    "Bhutan": (27.5, 90.4), "Maldives": (3.2, 73.2), "Brunei": (4.5, 114.7),
    "Papua New Guinea": (-6.3, 143.9), "Fiji": (-17.7, 178.0),
    "Tanzania": (-6.4, 34.9), "Uganda": (1.4, 32.3), "Rwanda": (-1.9, 29.9),
    "Burundi": (-3.4, 29.9), "Ghana": (7.9, -1.0), "Ivory Coast": (7.5, -5.5),
    "Cote D'Ivoire": (7.5, -5.5), "Senegal": (14.5, -14.5), "Guinea": (9.9, -11.3),
    "Cameroon": (5.7, 12.7), "Central African Republic": (6.6, 20.9),
    "Mozambique": (-18.7, 35.5), "Zimbabwe": (-19.0, 29.9), "Zambia": (-13.1, 27.8),
    "Angola": (-11.2, 17.9), "Namibia": (-22.0, 18.5), "Botswana": (-22.3, 24.7),
    "Madagascar": (-18.8, 46.9), "Eritrea": (15.2, 39.8), "Djibouti": (11.8, 42.6),
    "Republic of the Congo": (-0.7, 15.8), "Gabon": (-0.8, 11.6),
}


def normalize_events(raw, error=None):
    """raw is {"articles": [...]} from GDELT DOC artlist. Geolocate each article
    to its source-country centroid; drop ungeolocatable ones. Headline = title;
    tone = the conflict-keyword risk score (0-10) over the title.

    `error` (optional) is surfaced on the payload so a real upstream outage is
    visible instead of silently looking like a quiet news cycle."""
    items = []
    for a in (raw.get("articles") or []):
        cc = (a.get("sourcecountry") or "").strip()
        centroid = _COUNTRY_CENTROID.get(cc)
        if not centroid:
            continue
        title = a.get("title") or ""
        tone, hits = _risk_score(title, "")
        items.append({"id": a.get("url"), "lat": centroid[0], "lng": centroid[1],
                      "label": title.strip() or a.get("domain"),
                      "headline": title.strip() or None,
                      "tone": tone, "risk_keywords": hits,
                      "source_country": cc, "domain": a.get("domain"),
                      "language": a.get("language"),
                      "seendate": a.get("seendate"),
                      "url": a.get("url"), "image": a.get("socialimage"),
                      "color": "#ff793f"})
    return _payload("events", items, error=error)


async def fetch_events():
    url = EVENTS_URL.format(q=quote(EVENTS_QUERY))
    # GDELT returns a plaintext 429 ("Please limit requests...") under load and
    # the FIRST proxy CONNECT of a cycle often fails with status -1/0 (the known
    # cold-tunnel flake — see CLAUDE.md). The old code only tried twice with no
    # backoff, so two cold connects in a row silently produced 0 items. We now
    # retry up to 4 times with a short backoff, treating -1/0/429 as transient,
    # and surface an `error` if every attempt fails (so a real outage is visible
    # rather than masquerading as "0 events"). One real request per cycle still
    # respects the 1 req / 5s cap; retries only fire when the prior attempt did
    # not actually reach GDELT (cold tunnel) or got a 429.
    r = None
    last_status = None
    tries = 5
    for attempt in range(tries):
        r = await F.aget(url, headers=EVENTS_HDR, timeout=40)
        if r.tier == "refused_no_proxy":
            raise RuntimeError("proxy not configured (refused_no_proxy)")
        last_status = r.status
        body = (getattr(r, "body", b"") or b"").lstrip()
        # Success: HTTP 200 with a JSON object/array body.
        if r.status == 200 and body[:1] in (b"{", b"["):
            try:
                return normalize_events(json.loads(body))
            except Exception:
                pass  # malformed JSON -> treat as transient, retry
        # Retry on cold-tunnel failure (-1/0), rate-limit (429), or 5xx. GDELT
        # rate-limits to ~1 req / 5s and returns a PLAINTEXT 429, so on 429 we
        # wait the full 5s+ before retrying; cold-tunnel flakes just need a fresh
        # CONNECT, so a shorter wait suffices. The 15-min layer interval gives us
        # plenty of room for these in-cycle retries.
        if attempt < tries - 1:
            await asyncio.sleep(5.5 if r.status in (429, 503) else 1.0)
    # Every attempt failed — report the count as 0 but make the failure visible.
    return normalize_events({"articles": []},
                            error=f"GDELT DOC unavailable (last status {last_status})")


# ===========================================================================
# nav_warnings — NGA Maritime Safety Information broadcast warnings (NAVAREA).
# Active danger zones, naval exercises, missile/gunnery firings, MODU positions
# across NAVAREA IV (Caribbean/W.Atlantic), XII (E.Pacific), and the I/P/C
# regions. Keyless JSON. Coordinates are embedded in the free-text `text` field
# as degrees-decimalminutes (e.g. "10-41.9N 061-45.1W"), NOT structured fields,
# so we regex them out and plot the FIRST coordinate as the warning's point (the
# full set of points is carried in `coords` for area rendering). Verified live
# through ProxyRack (2026-05-21): 386 active warnings, 365 with parseable coords.
# 1h interval (warnings change slowly).
# ===========================================================================
NAV_WARN_URL = ("https://msi.nga.mil/api/publications/broadcast-warn"
                "?status=active&output=json")
NAV_WARN_HDR = {"Accept": "application/json", "User-Agent": "globe-recon/1.0"}

# DD-MM.mN  DDD-MM.mW  (degrees + decimal-minutes, hemisphere suffix).
_NAV_COORD_RE = re.compile(
    r'(\d{1,3})-(\d{1,2}(?:\.\d+)?)\s*([NS])\s+(\d{1,3})-(\d{1,2}(?:\.\d+)?)\s*([EW])')
# Human label per NAVAREA code (NGA's `navArea`).
_NAV_AREA_NAME = {
    "4": "NAVAREA IV (W. North Atlantic / Caribbean)",
    "12": "NAVAREA XII (E. North Pacific)",
    "A": "NAVAREA I region", "P": "Pacific (HYDROPAC)", "C": "NAVAREA region C",
}


def _nav_parse_coords(text):
    """All (lat, lng) decimal pairs embedded in an NGA warning's text body."""
    pts = []
    for la_d, la_m, la_h, lo_d, lo_m, lo_h in _NAV_COORD_RE.findall(text or ""):
        try:
            lat = int(la_d) + float(la_m) / 60.0
            lng = int(lo_d) + float(lo_m) / 60.0
        except ValueError:
            continue
        if la_h == "S":
            lat = -lat
        if lo_h == "W":
            lng = -lng
        if -90 <= lat <= 90 and -180 <= lng <= 180:
            pts.append([round(lat, 4), round(lng, 4)])
    return pts


def normalize_nav_warnings(raw):
    """raw is {"broadcast-warn": [...]}. Emit a point per warning that carries
    at least one parseable coordinate (plotted at its first coord); all coords
    are kept in `coords` so the UI can draw the danger area/track."""
    warns = raw.get("broadcast-warn") or raw.get("data") or (
        raw if isinstance(raw, list) else [])
    items = []
    for w in warns:
        if not isinstance(w, dict):
            continue
        pts = _nav_parse_coords(w.get("text"))
        if not pts:
            continue
        text = (w.get("text") or "").replace("\n", " ").strip()
        nav = str(w.get("navArea") or w.get("area") or "")
        items.append({
            "id": "nav-%s-%s-%s" % (nav, w.get("msgYear"), w.get("msgNumber")),
            "lat": pts[0][0], "lng": pts[0][1],
            "label": text[:120] or ("NAVAREA %s warning" % nav),
            "nav_area": nav, "nav_area_name": _NAV_AREA_NAME.get(nav),
            "subregion": w.get("subregion"),
            "msg": "%s/%s" % (w.get("msgNumber"), w.get("msgYear")),
            "issued": w.get("issueDate"), "authority": w.get("authority"),
            "text": text[:1000], "coords": pts, "color": "#f7b731"})
    return _payload("nav_warnings", items)


async def fetch_nav_warnings():
    data = await _aget_json(NAV_WARN_URL, headers=NAV_WARN_HDR, timeout=60, tries=3)
    return normalize_nav_warnings(data)


# ===========================================================================
# NOTAMs — FAA NOTAM API (https://external-api.faa.gov/notamapi/v1/notams) is
# GATEWAY-GATED: it requires a registered free client_id/client_secret and
# returns 404 "No context-path matches the request URI" for any unauthenticated
# request (verified live 2026-05-21, with and without a dummy key). We do NOT
# have a key, so this source is SKIPPED per the task's fallback rule. The NGA
# NAVAREA warnings layer above already captures the maritime danger-zone /
# missile-test / exercise pre-strike signal; FAA TFRs (tfr.faa.gov, keyless HTML)
# are a possible future keyless airspace source but are unstructured and out of
# scope here. No `notams` layer is registered.
# ===========================================================================


# ===========================================================================
# acled — ACLED conflict events worldwide (https://api.acleddata.com/acled/read)
# is SKIPPED. It is gated TWO ways: (1) it requires a free registered API key
# (email + key params) which we do NOT have, and (2) the api.acleddata.com host
# rejects ProxyRack at the CONNECT layer (565 / "connection closed abruptly",
# verified live 2026-05-21 across curl + curl_cffi). Per CLAUDE.md we do NOT
# carve a host out of the proxy pool or fall back to the home IP. The GDELT
# `events` layer above already provides worldwide conflict/military event
# coverage, so no `acled` layer is registered. (acleddata.com — the public site,
# not the API — does return 200 through the proxy, but exposes no bulk JSON.)
# ===========================================================================


# ===========================================================================
# chokepoints — strategic maritime straits/canals (curated static).
# Stable, well-documented public-knowledge geography; no upstream fetch (so no
# proxy use and never empty/flaky). Directly feeds the escalation picture — a
# carrier group repositioning toward Hormuz, the Taiwan Strait, or (for the Cuba
# scenario) the Florida Straits / Windward Passage is a kinetic tell. `weight`
# (1-3) drives marker prominence. `note` is qualitative on purpose so the figure
# never goes stale.
# ===========================================================================
_CHOKEPOINTS = [
    ("hormuz", "Strait of Hormuz", 26.57, 56.25, 3,
     "~1/5 of global seaborne oil; Persian Gulf's only outlet."),
    ("malacca", "Strait of Malacca", 1.43, 102.89, 3,
     "~1/4 of all traded goods by sea; the Asia-Europe artery."),
    ("suez", "Suez Canal", 30.42, 32.35, 3,
     "Med↔Red Sea shortcut; ~12% of global trade transits."),
    ("bab_el_mandeb", "Bab-el-Mandeb", 12.58, 43.33, 3,
     "Red Sea↔Gulf of Aden gate; Houthi missile/drone range."),
    ("panama", "Panama Canal", 9.08, -79.68, 3,
     "Atlantic↔Pacific shortcut; ~5% of maritime trade."),
    ("bosphorus", "Bosphorus / Istanbul Strait", 41.12, 29.07, 2,
     "Black Sea↔Med; Russia/Ukraine grain & naval transit."),
    ("dardanelles", "Dardanelles", 40.20, 26.40, 2,
     "Aegean↔Marmara; the outer Turkish Strait."),
    ("gibraltar", "Strait of Gibraltar", 35.95, -5.50, 2,
     "Atlantic↔Med; NATO/US 6th Fleet ingress."),
    ("danish", "Danish Straits", 55.50, 12.70, 2,
     "Baltic↔North Sea; Russian Baltic Fleet & energy exports."),
    ("kerch", "Kerch Strait", 45.30, 36.50, 2,
     "Sea of Azov access; flashpoint of the Russia-Ukraine war."),
    ("good_hope", "Cape of Good Hope", -34.36, 18.47, 2,
     "Suez-bypass route; traffic surged amid Red Sea attacks."),
    ("taiwan_strait", "Taiwan Strait", 24.50, 119.50, 3,
     "~PRC-Taiwan; PLA crossings & a top global flashpoint."),
    ("luzon", "Luzon Strait / Bashi Channel", 20.50, 121.50, 2,
     "S. China Sea↔Pacific; PLAN/USN submarine corridor."),
    ("dover", "Strait of Dover", 51.00, 1.50, 1,
     "Channel's narrowest point; busiest shipping lane on earth."),
    ("lombok", "Lombok Strait", -8.70, 115.90, 1,
     "Deep-draft Malacca alternative through Indonesia."),
    ("sunda", "Sunda Strait", -6.00, 105.90, 1,
     "Java↔Sumatra; secondary Indonesian passage."),
    ("korea_strait", "Korea Strait", 34.00, 129.00, 2,
     "Japan↔Korea; Russian/Chinese/DPRK naval transit."),
    ("mozambique", "Mozambique Channel", -17.00, 41.00, 1,
     "SW Indian Ocean lane; LNG & a re-routing corridor."),
    ("tiran", "Strait of Tiran", 28.00, 34.45, 1,
     "Gulf of Aqaba access; Israel/Jordan/Saudi/Egypt nexus."),
    ("florida_straits", "Florida Straits", 24.00, -80.50, 3,
     "Cuba↔Florida (~90 mi); core of any US-Cuba naval scenario."),
    ("windward", "Windward Passage", 20.00, -73.70, 2,
     "Cuba↔Hispaniola; Caribbean approach to Guantánamo."),
    ("yucatan", "Yucatán Channel", 21.50, -85.50, 2,
     "Cuba↔Mexico; western Gulf-of-Mexico approach."),
    ("magellan", "Strait of Magellan", -53.50, -70.50, 1,
     "S. American Atlantic↔Pacific passage south of the Andes."),
    ("bering", "Bering Strait", 65.90, -169.00, 1,
     "US (Alaska)↔Russia; Arctic route chokepoint."),
]


async def fetch_chokepoints():
    items = [{
        "id": cid, "lat": lat, "lng": lng,
        "label": name, "name": name,
        "weight": w, "note": note,
        "kind": "chokepoint",
        "color": "#22d3ee",
    } for cid, name, lat, lng, w, note in _CHOKEPOINTS]
    return _payload("chokepoints", items)


# ===========================================================================
# nuclear — globally significant nuclear sites (curated static).
# Widely-documented open-source facilities: power reactors, fuel-cycle/enrichment
# sites, weapons labs/production, and test sites. Coordinates are public knowledge
# (~0.1° precision). `kind` ∈ {power, fuel_cycle, weapons, test, accident}; many
# tie directly into hotspots (Natanz/Fordow→Iran, Yongbyon→Korea, Zaporizhzhia→
# Ukraine, Dimona→Israel). Static — no fetch, never empty.
# ===========================================================================
_NUCLEAR = [
    # (id, name, lat, lng, country, kind, note)
    ("natanz", "Natanz Enrichment", 33.72, 51.73, "Iran", "fuel_cycle",
     "Iran's main uranium-enrichment complex."),
    ("fordow", "Fordow Fuel Enrichment", 34.88, 50.99, "Iran", "fuel_cycle",
     "Deeply-buried enrichment site near Qom."),
    ("bushehr", "Bushehr NPP", 28.83, 50.89, "Iran", "power",
     "Iran's sole operating power reactor (Russian-built)."),
    ("arak", "Arak (Khondab) Reactor", 34.38, 49.24, "Iran", "fuel_cycle",
     "Heavy-water reactor & plant."),
    ("yongbyon", "Yongbyon Scientific Centre", 39.80, 125.75, "North Korea",
     "weapons", "DPRK plutonium reactor & reprocessing."),
    ("punggye_ri", "Punggye-ri Test Site", 41.28, 129.09, "North Korea", "test",
     "All six DPRK nuclear tests detonated here."),
    ("dimona", "Negev (Dimona) Centre", 31.00, 35.14, "Israel", "weapons",
     "Reactor widely linked to Israel's undeclared arsenal."),
    ("lop_nur", "Lop Nur Test Site", 40.80, 89.60, "China", "test",
     "China's historic atmospheric/underground test range."),
    ("zaporizhzhia", "Zaporizhzhia NPP", 47.51, 34.59, "Ukraine", "power",
     "Europe's largest NPP; occupied — IAEA safety concern."),
    ("chernobyl", "Chornobyl", 51.39, 30.10, "Ukraine", "accident",
     "Site of the 1986 disaster; exclusion zone."),
    ("fukushima", "Fukushima Daiichi", 37.42, 141.03, "Japan", "accident",
     "2011 meltdown site; ongoing decommissioning."),
    ("kashiwazaki", "Kashiwazaki-Kariwa", 37.43, 138.60, "Japan", "power",
     "World's largest nuclear power station by capacity."),
    ("bruce", "Bruce NGS", 44.32, -81.60, "Canada", "power",
     "One of the world's largest operating NPPs."),
    ("palo_verde", "Palo Verde", 33.39, -112.86, "USA", "power",
     "Largest US power plant by generation."),
    ("vogtle", "Vogtle", 33.14, -81.76, "USA", "power",
     "Newest US reactors (units 3 & 4)."),
    ("los_alamos", "Los Alamos NL", 35.84, -106.29, "USA", "weapons",
     "Original Manhattan Project weapons-design lab."),
    ("oak_ridge", "Oak Ridge (Y-12)", 35.99, -84.25, "USA", "weapons",
     "Uranium processing & weapons-component plant."),
    ("pantex", "Pantex Plant", 35.31, -101.56, "USA", "weapons",
     "US warhead assembly/disassembly facility."),
    ("nnss", "Nevada National Security Site", 37.12, -116.05, "USA", "test",
     "Primary US nuclear test site (1951-1992)."),
    ("savannah_river", "Savannah River Site", 33.34, -81.66, "USA", "fuel_cycle",
     "Tritium & nuclear-materials complex."),
    ("sarov", "Sarov (VNIIEF)", 54.93, 43.32, "Russia", "weapons",
     "Russia's lead nuclear-weapons design centre."),
    ("mayak", "Mayak", 55.71, 60.80, "Russia", "fuel_cycle",
     "Plutonium production & reprocessing complex."),
    ("sellafield", "Sellafield", 54.42, -3.50, "United Kingdom", "fuel_cycle",
     "UK reprocessing & nuclear-decommissioning site."),
    ("gravelines", "Gravelines", 51.01, 2.14, "France", "power",
     "One of Western Europe's largest NPPs."),
    ("la_hague", "La Hague", 49.68, -1.88, "France", "fuel_cycle",
     "Major spent-fuel reprocessing plant."),
    ("kahuta", "Kahuta (KRL)", 33.65, 73.40, "Pakistan", "fuel_cycle",
     "Pakistan's principal enrichment facility."),
    ("trombay", "Bhabha (Trombay)", 19.01, 72.93, "India", "weapons",
     "India's core nuclear research & weapons site."),
    ("kudankulam", "Kudankulam", 8.17, 77.71, "India", "power",
     "India's largest NPP (Russian-built)."),
    ("barakah", "Barakah", 23.97, 52.20, "UAE", "power",
     "Arab world's first nuclear power plant."),
    ("hanul", "Hanul (Uljin)", 37.09, 129.38, "South Korea", "power",
     "Large ROK reactor complex on the east coast."),
]


async def fetch_nuclear():
    items = [{
        "id": nid, "lat": lat, "lng": lng,
        "label": name, "name": name,
        "country": country, "kind": kind, "note": note,
        "color": "#a3e635",
    } for nid, name, lat, lng, country, kind, note in _NUCLEAR]
    return _payload("nuclear", items)


# ===========================================================================
# cell-towers — global cell-tower DENSITY from the local OpenCelliD DB
# (~/Desktop/cellphone-data/opencellid/towers.sqlite, built by opencellid.py).
# 40M+ raw towers can't render, so we H3-bin (res 2 ≈ 5,882 cells worldwide) to
# one point per cell colored by log-density and cap to the densest 5,000 — same
# order as military_bases, default-OFF. Pure LOCAL read, NO network (interval is
# weekly; towers change slowly). Missing DB -> empty payload + error, like any
# upstream-down layer.
# ===========================================================================
TOWERS_DB = os.environ.get(
    "OPENCELLID_DB",
    str(Path.home() / "Desktop" / "cellphone-data" / "opencellid" / "towers.sqlite"))
TOWERS_H3_RES = 2
TOWERS_CAP = 5000


# Generation palette (cool/bright = modern). `color` defaults to the tech lens —
# the strongest GLOBAL signal. `color_recency` is also emitted, but note it's
# OpenCelliD's first-LOGGED date (contributor recency), not true build date — a
# real buildout lens needs regulator commissioning dates (e.g. ANFR for FR).
_GEN_COLOR = {"NR": "#00e5ff", "LTE": "#76ff03", "UMTS": "#ffb300",
              "GSM": "#ff3d3d", "CDMA": "#ff3d3d"}


def _recency_color(yr):
    t = max(0.0, min(1.0, (yr - 2018) / 8)) if yr else 0.0
    if t > 0.5:
        r, g, b = int((1 - (t - 0.5) * 2) * 255), 255, int((t - 0.5) * 2 * 255)
    else:
        r, g, b = 255, int(t * 2 * 255), 0
    return f"#{r:02x}{g:02x}{b:02x}"


def bin_towers(rows, res=TOWERS_H3_RES, cap=TOWERS_CAP):
    """rows: (lat, lng, radio, operator, created) -> enriched globe items, one
    per H3 cell: count + dominant tech + %5G + top operator + newest year, with
    a tech-lens `color` (default) and `color_recency` for the recency lens.
    Capped to the densest `cap` cells."""
    import collections
    import datetime
    import h3
    cells = collections.defaultdict(
        lambda: {"n": 0, "tech": collections.Counter(),
                 "op": collections.Counter(), "newest": 0})
    for row in rows:
        lat, lng = row[0], row[1]
        radio = row[2] if len(row) > 2 else None
        operator = row[3] if len(row) > 3 else None
        created = row[4] if len(row) > 4 else None
        if lat is None or lng is None or not (-90 <= lat <= 90 and -180 <= lng <= 180):
            continue
        try:
            k = h3.latlng_to_cell(lat, lng, res)
        except Exception:
            continue
        d = cells[k]
        d["n"] += 1
        if radio:
            d["tech"][radio] += 1
        if operator:
            d["op"][operator] += 1
        if created and created > d["newest"]:
            d["newest"] = created
    top = sorted(cells.items(), key=lambda kv: -kv[1]["n"])[:cap]
    out = []
    for cell, d in top:
        lat, lng = h3.cell_to_latlng(cell)
        n = d["n"]
        dom = d["tech"].most_common(1)[0][0] if d["tech"] else "?"
        pct5g = round(100 * d["tech"].get("NR", 0) / n) if n else 0
        topop = d["op"].most_common(1)[0][0] if d["op"] else None
        yr = datetime.datetime.fromtimestamp(d["newest"], datetime.timezone.utc).year \
            if d["newest"] else 0
        out.append({"id": cell, "lat": round(lat, 4), "lng": round(lng, 4),
                    "count": n, "dominant_tech": dom, "pct_5g": pct5g,
                    "top_operator": topop, "newest_year": yr,
                    "category": "cell_infrastructure",
                    "color": _GEN_COLOR.get(dom, "#888888"),
                    "color_recency": _recency_color(yr),
                    "label": f"{n:,} towers · {topop or '?'} · {dom} · {pct5g}% 5G"})
    return out


async def fetch_cell_towers():
    """Publish a density-proportional random sample of REAL tower coordinates,
    each colored by network generation. NOT the H3 roll-up (which reads as a
    fake hex grid); these are actual lat/lng so the globe shows true coverage
    density — dense Europe/US, empty oceans, irregular clustering.

    Also flags STALE towers (no contributor update in >180d) — closest free
    proxy for "tower has gone dark"; carrier-grade up/down isn't public.
    """
    import sqlite3
    import time
    if not os.path.exists(TOWERS_DB):
        return _payload("cell-towers", [],
                        error=f"towers DB not found at {TOWERS_DB} (run opencellid.py)")
    con = sqlite3.connect(TOWERS_DB)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(tower)")}
        sel = "lat, lon, radio"
        sel += ", operator" if "operator" in cols else ", NULL"
        sel += ", country" if "country" in cols else ", NULL"
        sel += ", updated" if "updated" in cols else ", NULL"
        sel += ", created" if "created" in cols else ", NULL"
        sample = list(con.execute(f"SELECT {sel} FROM tower ORDER BY RANDOM() LIMIT 20000"))
    finally:
        con.close()

    GEN = {"NR": "#00e5ff", "LTE": "#76ff03", "UMTS": "#ffb300",
           "GSM": "#ff3d3d", "CDMA": "#ff3d3d"}
    # No-contributor-update >1yr → likely actually dark (>180d is too noisy:
    # 56% of towers, mostly just unsampled). Magenta so they pop on the globe.
    # Closest free proxy for "down" — real per-tower up/down isn't public.
    DARK = "#ff00aa"
    STALE_S = 365 * 86400
    now = int(time.time())

    items = []
    for la, lo, radio, op, country, updated, created in sample:
        if la is None or lo is None or not (-90 <= la <= 90 and -180 <= lo <= 180):
            continue
        stale = bool(updated) and (now - int(updated)) > STALE_S
        items.append({
            "lat": round(la, 5), "lng": round(lo, 5),
            "radio": radio, "operator": op, "country": country,
            "year": (time.gmtime(int(created)).tm_year if created else None),
            "last_seen_year": (time.gmtime(int(updated)).tm_year if updated else None),
            "stale": stale,
            "color": DARK if stale else GEN.get(radio, "#888888"),
            "label": (radio or "?") + (f" · {op}" if op else "")
                     + (" · NO SIGNAL >1yr" if stale else ""),
        })
    return _payload("cell-towers", items)


# ===========================================================================
# power_plants — global power plants (all types) via OSM Overpass.
# Returns ~150k elements globally; uses single Overpass call with bumped
# timeout + maxsize. Falls through alternate Overpass endpoints on failure.
# ===========================================================================
POWER_PLANTS_QL = (
    '[out:json][timeout:120][maxsize:268435456];'
    '('
    'node["power"="plant"];'
    'way["power"="plant"];'
    'relation["power"="plant"];'
    ');'
    'out center tags;'
)

_POWER_SRC_LABEL = {
    "solar": "Solar", "photovoltaic": "Solar",
    "wind": "Wind",
    "hydro": "Hydro", "water": "Hydro", "tidal": "Hydro",
    "gas": "Natural Gas", "natural_gas": "Natural Gas",
    "coal": "Coal", "lignite": "Coal",
    "oil": "Oil", "diesel": "Oil",
    "nuclear": "Nuclear",
    "biomass": "Biomass", "biogas": "Biomass",
    "geothermal": "Geothermal",
    "battery": "Battery", "energy_storage": "Battery",
    "waste": "Waste",
}


def normalize_power_plants(raw):
    items = []
    for el in raw.get("elements", []):
        tags = el.get("tags") or {}
        lat, lng = el.get("lat"), el.get("lon")
        center = el.get("center")
        if (lat is None or lng is None) and isinstance(center, dict):
            lat, lng = center.get("lat"), center.get("lon")
        if lat is None or lng is None:
            continue
        src = (tags.get("plant:source") or "").lower().split(";")[0]
        items.append({
            "id": f"{el.get('type')}-{el.get('id')}",
            "lat": lat, "lng": lng,
            "label": tags.get("name") or tags.get("name:en") or _POWER_SRC_LABEL.get(src, "Power Plant"),
            "category": _POWER_SRC_LABEL.get(src, "Other"),
            "source_type": src or None,
            "output": tags.get("plant:output:electricity"),
            "operator": tags.get("operator"),
            "country": tags.get("addr:country") or tags.get("country"),
            "color": "#F5A623",
        })
    return _payload("power_plants", items)


async def fetch_power_plants():
    last_err = None
    for base in OVERPASS_ENDPOINTS:
        try:
            url = base + "?data=" + quote(POWER_PLANTS_QL, safe="")
            data = await _aget_json(url, headers=OVERPASS_HDR, timeout=130, tries=2)
            p = normalize_power_plants(data)
            if p["count"]:
                return p
        except Exception as e:
            last_err = e
    if last_err:
        raise last_err
    return _payload("power_plants", [])


# ===========================================================================
# hospitals — global hospitals via OSM Overpass (amenity=hospital).
# ===========================================================================
HOSPITALS_QL = (
    '[out:json][timeout:120][maxsize:268435456];'
    '('
    'node["amenity"="hospital"];'
    'way["amenity"="hospital"];'
    'relation["amenity"="hospital"];'
    ');'
    'out center tags;'
)


def normalize_hospitals(raw):
    items = []
    for el in raw.get("elements", []):
        tags = el.get("tags") or {}
        lat, lng = el.get("lat"), el.get("lon")
        center = el.get("center")
        if (lat is None or lng is None) and isinstance(center, dict):
            lat, lng = center.get("lat"), center.get("lon")
        if lat is None or lng is None:
            continue
        items.append({
            "id": f"{el.get('type')}-{el.get('id')}",
            "lat": lat, "lng": lng,
            "label": tags.get("name") or tags.get("name:en") or "Hospital",
            "operator": tags.get("operator"),
            "emergency": tags.get("emergency"),
            "healthcare": tags.get("healthcare"),
            "country": tags.get("addr:country"),
            "city": tags.get("addr:city"),
            "color": "#FF1744",
        })
    return _payload("hospitals", items)


async def fetch_hospitals():
    last_err = None
    for base in OVERPASS_ENDPOINTS:
        try:
            url = base + "?data=" + quote(HOSPITALS_QL, safe="")
            data = await _aget_json(url, headers=OVERPASS_HDR, timeout=130, tries=2)
            p = normalize_hospitals(data)
            if p["count"]:
                return p
        except Exception as e:
            last_err = e
    if last_err:
        raise last_err
    return _payload("hospitals", [])


# ===========================================================================
# tornado_warnings — NWS active alerts (US-only). GeoJSON polygons reduced
# to centroids + raw polygon preserved for future polygon rendering.
# ===========================================================================
NWS_UA = "InspoGlobe/1.0 (+zac@zacstern.co)"
NWS_TORNADO_URL = (
    "https://api.weather.gov/alerts/active"
    "?event=Tornado%20Warning&event=Tornado%20Watch"
    "&event=Severe%20Thunderstorm%20Warning&status=actual"
)


def _polygon_centroid(coords):
    minLat = 90.0; maxLat = -90.0; minLng = 180.0; maxLng = -180.0; n = 0
    def visit(c):
        nonlocal minLat, maxLat, minLng, maxLng, n
        if isinstance(c, list) and len(c) >= 2 and isinstance(c[0], (int, float)):
            lng, lat = c[0], c[1]
            if lng < minLng: minLng = lng
            if lng > maxLng: maxLng = lng
            if lat < minLat: minLat = lat
            if lat > maxLat: maxLat = lat
            n += 1
        elif isinstance(c, list):
            for x in c:
                visit(x)
    visit(coords)
    if not n:
        return None, None
    return (minLat + maxLat) / 2.0, (minLng + maxLng) / 2.0


def normalize_tornado(raw):
    items = []
    for feat in raw.get("features", []) or []:
        props = feat.get("properties") or {}
        geom = feat.get("geometry") or {}
        gtype = geom.get("type")
        if gtype not in ("Polygon", "MultiPolygon"):
            continue
        lat, lng = _polygon_centroid(geom.get("coordinates"))
        if lat is None:
            continue
        event = props.get("event") or "Alert"
        is_warning = "Warning" in event
        items.append({
            "id": props.get("id") or f"nws-{len(items)}",
            "lat": lat, "lng": lng,
            "label": event,
            "headline": props.get("headline"),
            "severity": props.get("severity"),
            "effective": props.get("effective"),
            "expires": props.get("expires"),
            "areaDesc": props.get("areaDesc"),
            "category": event,
            "polygon": geom,
            "color": "#FF1744" if is_warning else "#FFC400",
        })
    return _payload("tornado_warnings", items)


async def fetch_tornado_warnings():
    data = await _aget_json(NWS_TORNADO_URL,
                            headers={"User-Agent": NWS_UA, "Accept": "application/geo+json"},
                            timeout=30, tries=2)
    return normalize_tornado(data)


# ===========================================================================
# hurricanes — NOAA NHC active tropical cyclones (Atlantic + E/C Pacific).
# ===========================================================================
NHC_URL = "https://www.nhc.noaa.gov/CurrentStorms.json"


def _saffir_simpson(intensity_kts):
    try:
        v = int(intensity_kts)
    except Exception:
        return None
    if v < 34: return "TD"
    if v < 64: return "TS"
    if v <= 82: return "Cat 1"
    if v <= 95: return "Cat 2"
    if v <= 112: return "Cat 3"
    if v <= 136: return "Cat 4"
    return "Cat 5"


def normalize_hurricanes(raw):
    items = []
    storms = raw.get("activeStorms") or raw.get("storms") or []
    for s in storms:
        lat = s.get("latitudeNumeric")
        lng = s.get("longitudeNumeric")
        if lat is None or lng is None:
            continue
        intensity = s.get("intensity")
        items.append({
            "id": s.get("id") or s.get("binNumber") or s.get("name"),
            "lat": lat, "lng": lng,
            "label": s.get("name"),
            "classification": s.get("classification"),
            "category": _saffir_simpson(intensity),
            "intensity_kts": intensity,
            "pressure": s.get("pressure"),
            "movement_dir": s.get("movementDir"),
            "movement_speed": s.get("movementSpeed"),
            "last_update": s.get("lastUpdate"),
            "color": "#7B1FA2",
        })
    return _payload("hurricanes", items)


async def fetch_hurricanes():
    data = await _aget_json(NHC_URL, headers={"User-Agent": NWS_UA, "Accept": "application/json"},
                            timeout=30, tries=2)
    return normalize_hurricanes(data)


# ===========================================================================
# wind + aqi — Open-Meteo bulk-coords (KEYLESS). See deploy copy for design.
# ===========================================================================
import math

_FIB_GRID_CACHE = None


def _fibonacci_sphere(n=400):
    global _FIB_GRID_CACHE
    if _FIB_GRID_CACHE and len(_FIB_GRID_CACHE) == n:
        return _FIB_GRID_CACHE
    pts = []
    golden = math.pi * (3 - math.sqrt(5))
    for i in range(n):
        y = 1 - (i / float(n - 1)) * 2
        radius = math.sqrt(1 - y * y)
        theta = golden * i
        x = math.cos(theta) * radius
        z = math.sin(theta) * radius
        lat = math.degrees(math.asin(y))
        lng = math.degrees(math.atan2(z, x))
        pts.append((round(lat, 3), round(lng, 3)))
    _FIB_GRID_CACHE = pts
    return pts


def _wind_color(speed_ms):
    if speed_ms is None: return "#888"
    s = float(speed_ms)
    if s < 3:   return "#4FC3F7"
    if s < 7:   return "#26C6DA"
    if s < 12:  return "#26A69A"
    if s < 17:  return "#FFB300"
    if s < 24:  return "#FF6F00"
    if s < 32:  return "#E53935"
    return "#7B1FA2"


def _aqi_color(aqi):
    if aqi is None: return "#888"
    a = float(aqi)
    if a <= 50:  return "#00E676"
    if a <= 100: return "#FFEB3B"
    if a <= 150: return "#FF9800"
    if a <= 200: return "#F44336"
    if a <= 300: return "#7B1FA2"
    return "#7F1D1D"


def _aqi_label(aqi):
    if aqi is None: return None
    a = float(aqi)
    if a <= 50:  return "Good"
    if a <= 100: return "Moderate"
    if a <= 150: return "Unhealthy (sensitive)"
    if a <= 200: return "Unhealthy"
    if a <= 300: return "Very Unhealthy"
    return "Hazardous"


async def fetch_wind():
    pts = _fibonacci_sphere(400)
    lats = ",".join(str(p[0]) for p in pts)
    lngs = ",".join(str(p[1]) for p in pts)
    url = (f"https://api.open-meteo.com/v1/forecast"
           f"?latitude={lats}&longitude={lngs}"
           f"&current=wind_speed_10m,wind_direction_10m&wind_speed_unit=ms")
    data = await _aget_json(url, headers={"Accept": "application/json"}, timeout=30, tries=2)
    items = []
    if not isinstance(data, list):
        data = [data]
    for i, d in enumerate(data):
        cur = (d or {}).get("current") or {}
        speed = cur.get("wind_speed_10m"); direction = cur.get("wind_direction_10m")
        if speed is None or direction is None: continue
        lat = d.get("latitude"); lng = d.get("longitude")
        if lat is None or lng is None: continue
        items.append({"id": f"wind-{i}", "lat": lat, "lng": lng,
                      "label": f"{speed} m/s", "speed_ms": speed,
                      "direction_deg": direction, "track": direction,
                      "color": _wind_color(speed)})
    return _payload("wind", items)


async def fetch_aqi():
    pts = _fibonacci_sphere(400)
    lats = ",".join(str(p[0]) for p in pts)
    lngs = ",".join(str(p[1]) for p in pts)
    url = (f"https://air-quality-api.open-meteo.com/v1/air-quality"
           f"?latitude={lats}&longitude={lngs}"
           f"&current=us_aqi,european_aqi,pm10,pm2_5")
    data = await _aget_json(url, headers={"Accept": "application/json"}, timeout=30, tries=2)
    items = []
    if not isinstance(data, list):
        data = [data]
    for i, d in enumerate(data):
        cur = (d or {}).get("current") or {}
        aqi = cur.get("us_aqi")
        if aqi is None: continue
        lat = d.get("latitude"); lng = d.get("longitude")
        if lat is None or lng is None: continue
        cat = _aqi_label(aqi)
        items.append({"id": f"aqi-{i}", "lat": lat, "lng": lng,
                      "label": f"AQI {int(aqi)} · {cat}",
                      "us_aqi": aqi, "european_aqi": cur.get("european_aqi"),
                      "pm2_5": cur.get("pm2_5"), "pm10": cur.get("pm10"),
                      "category": cat, "color": _aqi_color(aqi)})
    return _payload("aqi", items)


# ===========================================================================
# Registry — cadences from the design spec's interval table.
# ===========================================================================
LAYERS = [
    {"id": "flights",        "interval_s": 15,    "fetch": fetch_flights},
    {"id": "military_air",   "interval_s": 15,    "fetch": fetch_military_air},
    {"id": "satellites",     "interval_s": 10,    "fetch": fetch_satellites},
    {"id": "markets",        "interval_s": 30,    "fetch": fetch_markets},
    # 2026-05-23: migrated to Vercel Cron (src/app/api/globe/<layer>/cron/route.ts).
    # Disabled here to avoid double-writes to the same blob path. Re-enable if
    # the Vercel Cron path ever fails for an extended window.
    # {"id": "earthquakes",    "interval_s": 60,    "fetch": fetch_earthquakes},
    {"id": "news",           "interval_s": 300,   "fetch": fetch_news},
    {"id": "events",         "interval_s": 900,   "fetch": fetch_events},
    # {"id": "natural-events", "interval_s": 900,   "fetch": fetch_natural_events},
    # {"id": "wildfire",       "interval_s": 900,   "fetch": fetch_wildfire},
    # {"id": "cyber",          "interval_s": 1800,  "fetch": fetch_cyber},
    {"id": "frontlines",     "interval_s": 1800,  "fetch": fetch_frontlines},
    # 2026-05-23 Phase 2: migrated to Vercel Cron.
    # {"id": "nav_warnings",   "interval_s": 3600,  "fetch": fetch_nav_warnings},
    {"id": "military_naval", "interval_s": 300,   "fetch": fetch_military_naval},
    {"id": "cctv",           "interval_s": 21600, "fetch": fetch_cctv},
    {"id": "infrastructure", "interval_s": 86400, "fetch": fetch_infrastructure},
    {"id": "military_bases", "interval_s": 86400, "fetch": fetch_military_bases},
    {"id": "chokepoints",    "interval_s": 86400, "fetch": fetch_chokepoints},
    {"id": "nuclear",        "interval_s": 86400, "fetch": fetch_nuclear},
    # Satellite-AIS-derived vessel events (Global Fishing Watch). Free with a
    # registration-required API key — no-op until GFW_API_KEY is set in env, so
    # safe to ship dark. 900s cadence is well within GFW's free rate budget.
    {"id": "vessel_events",  "interval_s": 900,    "fetch": _gfw_fetch_async},
    # FAA Temporary Flight Restrictions — airspace closures, military exercises,
    # missile tests, VIP movements, space-launch windows. Keyless public WFS +
    # tfrapi join (US + territories only). Pre-kinetic indicator.
    {"id": "notams",         "interval_s": 900,    "fetch": _notams_fetch_async},
    # Geopolitical context overlays — EEZs (Marine Regions, CC-BY), sanctions
    # zones (OFAC/UN/EU/UK shipping advisories), maritime chokepoint corridors.
    # Curated, daily refresh sufficient. Each feature carries top-level centroid
    # lat/lng so it renders as a clickable zone marker on the globe.
    {"id": "geo_zones",      "interval_s": 86400,  "fetch": _geo_zones_fetch_async},
    {"id": "cell-towers",    "interval_s": 604800, "fetch": fetch_cell_towers},
    # Weather warnings & cyclones — short cadence, blob-empty-safe.
    # 2026-05-23 Phase 2: migrated to Vercel Cron.
    # {"id": "tornado_warnings","interval_s": 300,   "fetch": fetch_tornado_warnings},
    # 2026-05-23: hurricanes migrated to Vercel Cron.
    # {"id": "hurricanes",     "interval_s": 1800,   "fetch": fetch_hurricanes},
    # 2026-05-23 Phase 2: wind + aqi migrated to Vercel Cron.
    # {"id": "wind",           "interval_s": 1800,   "fetch": fetch_wind},
    # {"id": "aqi",            "interval_s": 1800,   "fetch": fetch_aqi},
    # power_plants + hospitals fetchers defined but NOT registered: both fail at
    # ~17s in the ProxyRack residential CONNECT tunnel (Overpass computes
    # 60-180s server-side on 150k/217k element queries before bytes flow).
    # Wave-2 fix mirrors military_bases: split per plant:source for power, and
    # sub-continent bbox for hospitals so each sub-query returns inside 15s.
]
