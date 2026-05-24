"""Derived layers — pure-Python joins over existing globe blobs.

Each fetcher reads N input blobs from Vercel Blob (via collector's
BLOB_READ_WRITE_TOKEN), performs an in-memory join, and emits a new blob.
No new upstream fetches — these are intel-multiplier layers built on data
we already have.

Layers shipped here:
  quake_exposure          — earthquakes × (power_plants, hospitals, military_bases)
                            with critical-infra counts inside each quake's
                            estimated shake radius
  dark_fleet              — boats × trails — MMSIs that vanished from AIS for
                            >2h in the last 12h (shadow-fleet behavior)
  outbreak_airline_risk   — who_outbreaks × flights — flights leaving/arriving
                            outbreak-affected countries
  conflict_energy_risk    — frontlines × (power_plants, submarine_cables,
                            military_bases) — critical infra inside conflict
                            polygons

All emit the standard {layer, updatedAt, count, items, error?} payload.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import urllib.request
from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _payload(layer: str, items: list, error: str | None = None) -> dict:
    p = {"layer": layer, "updatedAt": _now_iso(), "count": len(items), "items": items}
    if error:
        p["error"] = error
    return p


# Cache blob reads across one collector cycle so a derived layer that reads
# 3 inputs doesn't trigger 3 list-then-fetch round-trips when called shortly
# after another derived layer that already pulled them.
_BLOB_CACHE: dict[str, tuple[float, list]] = {}
_BLOB_CACHE_TTL_S = 60.0  # one minute — derived layers run sequentially in <60s


def _read_blob(layer_id: str) -> list:
    """Pull the items list of a globe layer from Vercel Blob. Returns [] on
    any failure (missing token, network error, JSON parse error)."""
    import time
    now = time.time()
    cached = _BLOB_CACHE.get(layer_id)
    if cached and now - cached[0] < _BLOB_CACHE_TTL_S:
        return cached[1]
    tok = os.environ.get("BLOB_READ_WRITE_TOKEN", "")
    if not tok:
        return []
    try:
        req = urllib.request.Request(
            f"https://blob.vercel-storage.com/?prefix=globe/{layer_id}.json&limit=1",
            headers={"Authorization": f"Bearer {tok}"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            blobs = json.load(resp).get("blobs") or []
        if not blobs:
            return []
        with urllib.request.urlopen(blobs[0]["url"], timeout=30) as resp:
            items = json.load(resp).get("items") or []
        _BLOB_CACHE[layer_id] = (now, items)
        return items
    except Exception:
        return []


# Earth-radius-aware great-circle distance in km between two lat/lng pairs.
_R_EARTH_KM = 6371.0


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = rlat2 - rlat1
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlng / 2) ** 2)
    return 2 * _R_EARTH_KM * math.asin(min(1.0, math.sqrt(a)))


# ===========================================================================
# QUAKE EXPOSURE
# ===========================================================================
def _shake_radius_km(magnitude: float) -> float:
    """Rough estimate of the felt-shaking radius for a given magnitude.

    Based on the (very approximate) felt-area-vs-magnitude curve: M3=~30 km,
    M5=~100 km, M6=~200 km, M7=~400 km. This is a sane proxy when ShakeMap
    polygon isn't available; the proper enrichment (USGS ShakeMap GeoJSON
    per-event) is a Wave 2 follow-up.
    """
    return max(10.0, 10 ** ((magnitude - 2.5) / 1.5))


async def fetch_quake_exposure():
    """For each significant quake (mag >= 4.5), count power plants, hospitals,
    and military bases within the estimated shake radius."""
    def _build():
        quakes = _read_blob("earthquakes")
        if not quakes:
            return []
        plants = _read_blob("power_plants")
        hospitals = _read_blob("hospitals")
        bases = _read_blob("military_bases")
        items = []
        for q in quakes:
            mag = q.get("mag") or q.get("magnitude") or q.get("category")
            try:
                mag_val = float(mag) if mag is not None else 0.0
            except (TypeError, ValueError):
                continue
            if mag_val < 4.5:
                continue
            qlat = q.get("lat"); qlng = q.get("lng")
            if not isinstance(qlat, (int, float)) or not isinstance(qlng, (int, float)):
                continue
            radius = _shake_radius_km(mag_val)
            # Pre-filter by lat box (saves cycles on the 18k-power-plant set)
            lat_margin = radius / 111.0  # km → deg lat
            def _within(infra_list, max_n=20):
                hits = []
                for x in infra_list:
                    xlat = x.get("lat"); xlng = x.get("lng")
                    if not isinstance(xlat, (int, float)) or not isinstance(xlng, (int, float)):
                        continue
                    if abs(xlat - qlat) > lat_margin:
                        continue
                    if _haversine_km(qlat, qlng, xlat, xlng) <= radius:
                        hits.append({"label": x.get("label") or x.get("name"),
                                     "lat": xlat, "lng": xlng})
                        if len(hits) >= max_n:
                            break
                return hits
            nearby_plants = _within(plants, 10)
            nearby_hosp = _within(hospitals, 10)
            nearby_bases = _within(bases, 10)
            total = len(nearby_plants) + len(nearby_hosp) + len(nearby_bases)
            # Color by severity: more exposed infra = redder
            color = ("#7B1FA2" if total >= 30 else "#FF1744" if total >= 10
                     else "#FF9500" if total >= 3 else "#FFB300")
            items.append({
                "id": f"qx-{q.get('id') or len(items)}",
                "lat": qlat, "lng": qlng,
                "label": (f"M{mag_val:.1f} · {total} critical infra in "
                          f"{radius:.0f}km radius"),
                "magnitude": mag_val,
                "shake_radius_km": round(radius, 0),
                "plants_exposed": len(nearby_plants),
                "hospitals_exposed": len(nearby_hosp),
                "bases_exposed": len(nearby_bases),
                "total_exposed": total,
                "nearby_plants": nearby_plants,
                "nearby_hospitals": nearby_hosp,
                "nearby_bases": nearby_bases,
                "place": q.get("place") or q.get("label"),
                "time": q.get("time"),
                "category": "Critical-Infra Exposure",
                "color": color,
            })
        items.sort(key=lambda x: -x["total_exposed"])
        return items[:200]
    return _payload("quake_exposure", await asyncio.to_thread(_build))


# ===========================================================================
# DARK FLEET — AIS-gap detection
# ===========================================================================
async def fetch_dark_fleet():
    """MMSIs present in our trails (last 6h history) but MISSING from the
    current boats snapshot — i.e., vessels that turned off AIS in the last
    12h. Sanctions-evasion / illegal-fishing signal."""
    def _build():
        boats = _read_blob("boats")
        trails = _read_blob("trails")
        if not boats or not trails:
            return []
        live_mmsis = {str(b.get("id") or b.get("mmsi") or "") for b in boats}
        live_mmsis.discard("")
        items = []
        for t in trails:
            mmsi = str(t.get("id") or t.get("mmsi") or "")
            if not mmsi or mmsi in live_mmsis:
                continue
            # Trail entity vanished — extract last known position
            track = t.get("track") or t.get("positions") or []
            if not track:
                continue
            last = track[-1] if isinstance(track, list) else None
            if not isinstance(last, dict):
                continue
            lat = last.get("lat"); lng = last.get("lng")
            if not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
                continue
            last_seen = last.get("t") or last.get("time")
            gap_min = None
            try:
                if last_seen:
                    # Parse ISO timestamp; trails record time as UTC seconds or ISO
                    if isinstance(last_seen, (int, float)):
                        gap_min = (datetime.now(timezone.utc).timestamp() - float(last_seen)) / 60.0
                    else:
                        ts = datetime.fromisoformat(str(last_seen).replace("Z", "+00:00"))
                        gap_min = (datetime.now(timezone.utc) - ts).total_seconds() / 60.0
            except Exception:
                gap_min = None
            # Only emit if gap is meaningful (>2h) — shorter gaps are noise
            if gap_min is not None and gap_min < 120:
                continue
            # Color by gap severity
            if gap_min is None:
                color = "#FFB300"
            elif gap_min < 360:
                color = "#FFC400"  # 2–6 h
            elif gap_min < 720:
                color = "#FF9500"  # 6–12 h
            else:
                color = "#FF1744"  # >12 h
            items.append({
                "id": f"df-{mmsi}",
                "lat": lat, "lng": lng,
                "label": (f"{t.get('name') or mmsi} · AIS gap "
                          f"{gap_min:.0f}min" if gap_min else f"{t.get('name') or mmsi} · AIS gap"),
                "mmsi": mmsi,
                "name": t.get("name"),
                "gap_minutes": round(gap_min, 0) if gap_min else None,
                "last_seen": last_seen,
                "category": "Dark Vessel (AIS gap)",
                "color": color,
            })
        items.sort(key=lambda x: -(x.get("gap_minutes") or 0))
        return items[:500]
    return _payload("dark_fleet", await asyncio.to_thread(_build))


# ===========================================================================
# OUTBREAK × AIRLINE RISK
# ===========================================================================
# Major-airport IATA → country lookup for the countries most likely to appear
# in WHO Disease Outbreak News. Covers all WHO-tracked outbreak regions; not
# exhaustive of every airport but hits every significant international gateway.
_IATA_COUNTRY = {
    # Democratic Republic of the Congo
    "FIH": "Democratic Republic Of The Congo", "FBM": "Democratic Republic Of The Congo",
    "MJM": "Democratic Republic Of The Congo", "KMN": "Democratic Republic Of The Congo",
    # Uganda
    "EBB": "Uganda", "ULU": "Uganda",
    # Sudan / South Sudan
    "KRT": "Sudan", "PZU": "Sudan", "JUB": "South Sudan",
    # Ethiopia
    "ADD": "Ethiopia", "DIR": "Ethiopia",
    # Kenya
    "NBO": "Kenya", "MBA": "Kenya",
    # Rwanda
    "KGL": "Rwanda",
    # Tanzania
    "DAR": "Tanzania", "ZNZ": "Tanzania",
    # Nigeria
    "LOS": "Nigeria", "ABV": "Nigeria", "KAN": "Nigeria",
    # Ghana
    "ACC": "Ghana",
    # Liberia / Sierra Leone (Ebola history)
    "ROB": "Liberia", "FNA": "Sierra Leone",
    # Guinea
    "CKY": "Guinea",
    # Madagascar
    "TNR": "Madagascar",
    # Yemen
    "SAH": "Yemen", "ADE": "Yemen",
    # Syria
    "DAM": "Syria", "ALP": "Syria",
    # Lebanon
    "BEY": "Lebanon",
    # Iran
    "IKA": "Iran", "THR": "Iran", "MHD": "Iran", "SYZ": "Iran",
    # Iraq
    "BGW": "Iraq", "BSR": "Iraq", "EBL": "Iraq",
    # Afghanistan
    "KBL": "Afghanistan", "KDH": "Afghanistan",
    # Pakistan
    "ISB": "Pakistan", "KHI": "Pakistan", "LHE": "Pakistan",
    # India
    "DEL": "India", "BOM": "India", "MAA": "India", "BLR": "India",
    "CCU": "India", "HYD": "India",
    # Bangladesh
    "DAC": "Bangladesh", "CGP": "Bangladesh",
    # Myanmar
    "RGN": "Myanmar", "MDL": "Myanmar",
    # Thailand
    "BKK": "Thailand", "DMK": "Thailand", "HKT": "Thailand",
    # Vietnam
    "SGN": "Vietnam", "HAN": "Vietnam",
    # Cambodia / Laos
    "PNH": "Cambodia", "VTE": "Laos",
    # Indonesia
    "CGK": "Indonesia", "DPS": "Indonesia", "SUB": "Indonesia",
    # Philippines
    "MNL": "Philippines", "CEB": "Philippines",
    # China
    "PEK": "China", "PVG": "China", "CAN": "China", "SHA": "China",
    "CTU": "China", "SZX": "China", "XIY": "China",
    # Russia
    "SVO": "Russia", "DME": "Russia", "VKO": "Russia", "LED": "Russia",
    "KZN": "Russia", "AER": "Russia",
    # Ukraine (frontline; outbreak coincidence)
    "KBP": "Ukraine", "ODS": "Ukraine",
    # Mozambique
    "MPM": "Mozambique",
    # South Africa
    "JNB": "South Africa", "CPT": "South Africa", "DUR": "South Africa",
    # Brazil (Mpox, dengue waves)
    "GRU": "Brazil", "GIG": "Brazil", "BSB": "Brazil", "REC": "Brazil",
    # US (Hantavirus, etc.)
    "JFK": "United States Of America", "LAX": "United States Of America",
    "ORD": "United States Of America", "ATL": "United States Of America",
    "DFW": "United States Of America", "SFO": "United States Of America",
    "MIA": "United States Of America", "SEA": "United States Of America",
    # UK
    "LHR": "United Kingdom", "LGW": "United Kingdom", "MAN": "United Kingdom",
    # UAE (transit hub for African gateways)
    "DXB": "United Arab Emirates", "AUH": "United Arab Emirates",
    # Saudi
    "JED": "Saudi Arabia", "RUH": "Saudi Arabia",
    # Singapore, HK (transit)
    "SIN": "Singapore", "HKG": "Hong Kong",
}


async def fetch_outbreak_airline_risk():
    """Flights leaving or arriving an airport in a country with an active WHO
    Disease Outbreak News alert. Uses IATA→country lookup since FR24's
    `origin_name`/`dest_name` are typically None. One marker per matching flight."""
    def _build():
        outbreaks = _read_blob("who_outbreaks")
        flights = _read_blob("flights")
        if not outbreaks or not flights:
            return []
        affected = set()
        for o in outbreaks:
            cn = (o.get("country_name") or "").strip()
            if cn:
                affected.add(cn)
        if not affected:
            return []
        items = []
        for f in flights:
            origin = (f.get("origin") or "").upper().strip()
            dest = (f.get("destination") or "").upper().strip()
            origin_country = _IATA_COUNTRY.get(origin)
            dest_country = _IATA_COUNTRY.get(dest)
            matched_country = None
            direction = ""
            if origin_country and origin_country in affected:
                matched_country = origin_country
                direction = "from"
            elif dest_country and dest_country in affected:
                matched_country = dest_country
                direction = "to"
            if not matched_country:
                continue
            lat = f.get("lat"); lng = f.get("lng")
            if not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
                continue
            items.append({
                "id": f"oar-{f.get('id') or len(items)}",
                "lat": lat, "lng": lng,
                "label": (f"{f.get('label') or f.get('callsign') or '?'}: "
                          f"{origin} → {dest} ({direction} {matched_country})"),
                "callsign": f.get("callsign"),
                "airline": f.get("airline"),
                "origin": origin, "destination": dest,
                "outbreak_country": matched_country,
                "direction": direction,
                "category": "Outbreak Risk Flight",
                "color": "#FF1744",
            })
        return items[:500]
    return _payload("outbreak_airline_risk", await asyncio.to_thread(_build))


# ===========================================================================
# CONFLICT × CRITICAL INFRA
# ===========================================================================
def _point_in_polygon(lat: float, lng: float, polygon) -> bool:
    """Ray-cast point-in-polygon. polygon: list of [lng,lat] coordinate pairs."""
    if not polygon or len(polygon) < 3:
        return False
    inside = False
    j = len(polygon) - 1
    for i in range(len(polygon)):
        xi, yi = polygon[i][0], polygon[i][1]
        xj, yj = polygon[j][0], polygon[j][1]
        intersect = ((yi > lat) != (yj > lat)) and \
                    (lng < (xj - xi) * (lat - yi) / (yj - yi + 1e-12) + xi)
        if intersect:
            inside = not inside
        j = i
    return inside


def _polygons_from_geom(geom):
    """Yield list[[lng,lat]] rings from a Polygon or MultiPolygon geometry."""
    if not geom:
        return
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    if gtype == "Polygon" and coords:
        if coords:
            yield coords[0]  # outer ring only
    elif gtype == "MultiPolygon" and coords:
        for poly in coords:
            if poly:
                yield poly[0]


async def fetch_conflict_energy_risk():
    """Critical infrastructure (power plants, submarine cable landings, military
    bases) whose lat/lng falls inside an active frontline polygon."""
    def _build():
        # Frontlines blob items are GeoJSON Feature[] (polygon control areas).
        frontlines = _read_blob("frontlines")
        if not frontlines:
            return []
        plants = _read_blob("power_plants")
        cables = _read_blob("submarine_cables")
        bases = _read_blob("military_bases")
        # Build a list of polygons + their name/properties
        polys = []
        for feat in frontlines:
            props = feat.get("properties") or {}
            geom = feat.get("geometry") or {}
            for ring in _polygons_from_geom(geom):
                polys.append((props.get("name") or props.get("title") or "Frontline",
                              ring))
        if not polys:
            return []
        items = []

        def _check(infra_list, kind, color):
            for x in infra_list[:5000]:  # cap input to keep this O(N) reasonable
                xlat = x.get("lat"); xlng = x.get("lng")
                if not isinstance(xlat, (int, float)) or not isinstance(xlng, (int, float)):
                    continue
                for poly_name, ring in polys:
                    if _point_in_polygon(xlat, xlng, ring):
                        items.append({
                            "id": f"cer-{kind}-{x.get('id') or len(items)}",
                            "lat": xlat, "lng": xlng,
                            "label": f"{x.get('label') or kind} (in {poly_name})",
                            "infra_type": kind,
                            "frontline": poly_name,
                            "original_label": x.get("label"),
                            "category": f"{kind} in conflict zone",
                            "color": color,
                        })
                        break  # one frontline match is enough per entity
        _check(plants, "Power Plant", "#FFB300")
        _check(cables, "Cable Landing", "#00BCD4")
        _check(bases, "Military Base", "#FF1744")
        return items[:500]
    return _payload("conflict_energy_risk", await asyncio.to_thread(_build))


# ===========================================================================
# PAGER QUAKES — USGS ShakeMap + PAGER casualty estimates for significant
# earthquakes. Per-event detail fetch (cached by event id) to extract:
#   - PAGER alert level (green/yellow/orange/red)
#   - Estimated fatalities / economic loss (from PAGER losses.json — secondary
#     fetch only for orange/red events to keep call volume down)
#   - ShakeMap intensity polygon URL (frontend can render the polygon)
# Emits ONLY events with magnitude >= 4.5 OR PAGER alert >= yellow.
# ===========================================================================
_PAGER_COLOR = {
    "green":  "#76FF03",
    "yellow": "#FFEB3B",
    "orange": "#FF9500",
    "red":    "#FF1744",
}

# Cache the detail JSON per event id since it changes infrequently.
_PAGER_CACHE: dict = {}


async def _usgs_event_detail(eventid: str, _F):
    """Fetch USGS event detail JSON. Cached in-memory per event id."""
    if eventid in _PAGER_CACHE:
        return _PAGER_CACHE[eventid]
    url = f"https://earthquake.usgs.gov/earthquakes/feed/v1.0/detail/{eventid}.geojson"
    try:
        r = await _F.aget(url, headers={"User-Agent": "globe-recon/1.0",
                                        "Accept": "application/json"}, timeout=15)
        if r.status == 200 and r.body:
            data = json.loads(r.body)
            _PAGER_CACHE[eventid] = data
            # Bound cache size — drop oldest if it grows
            if len(_PAGER_CACHE) > 1000:
                for k in list(_PAGER_CACHE.keys())[:200]:
                    del _PAGER_CACHE[k]
            return data
    except Exception:
        pass
    return None


async def _pager_losses(losses_url: str, _F):
    """Fetch the secondary losses.json for orange/red events."""
    if not losses_url:
        return None
    try:
        r = await _F.aget(losses_url, headers={"User-Agent": "globe-recon/1.0",
                                                "Accept": "application/json"}, timeout=15)
        if r.status == 200 and r.body:
            return json.loads(r.body)
    except Exception:
        pass
    return None


async def fetch_pager_quakes():
    """Significant earthquakes with USGS PAGER + ShakeMap enrichment."""
    try:
        from proxy_fetcher import ProxyFetcher
        _F = ProxyFetcher(impersonate="chrome146")
    except Exception:
        return _payload("pager_quakes", [])
    quakes = await asyncio.to_thread(_read_blob, "earthquakes")
    if not quakes:
        return _payload("pager_quakes", [])
    sem = asyncio.Semaphore(4)

    async def _enrich(q):
        async with sem:
            mag = q.get("mag") or q.get("magnitude")
            try:
                mag_val = float(mag) if mag is not None else 0.0
            except (TypeError, ValueError):
                mag_val = 0.0
            if mag_val < 4.5:
                return None
            event_id = (q.get("id") or "").lower()
            if not event_id:
                return None
            detail = await _usgs_event_detail(event_id, _F)
            if not detail:
                return None
            props = (detail.get("properties") or {})
            products = props.get("products") or {}
            pager = (products.get("losspager") or [{}])[0]
            shakemap = (products.get("shakemap") or [{}])[0]
            pager_props = pager.get("properties") or {}
            alert = (pager_props.get("alertlevel") or "").lower()
            if mag_val < 4.5 and alert not in ("yellow", "orange", "red"):
                return None
            # ShakeMap polygon URL (for frontend rendering, not embedded here)
            sm_contents = shakemap.get("contents") or {}
            mmi_polygon_url = (sm_contents.get("download/cont_mmi.json") or {}).get("url")
            # Losses.json — only fetch for orange/red events (saves API calls)
            losses = None
            if alert in ("orange", "red"):
                losses_url = (pager.get("contents") or {}).get("json/losses.json", {}).get("url")
                losses = await _pager_losses(losses_url, _F)
            fatalities = None
            economic = None
            if losses:
                emp_fat = losses.get("empirical_fatality") or {}
                emp_econ = losses.get("empirical_economic") or {}
                fatalities = emp_fat.get("total_fatalities")
                economic = emp_econ.get("total_dollars")
            return {
                "id": f"pager-{event_id}",
                "lat": q.get("lat"), "lng": q.get("lng"),
                "label": (f"M{mag_val:.1f} · {alert.upper() or 'pending'} · "
                          f"{q.get('place') or q.get('label') or ''}"),
                "magnitude": mag_val,
                "place": q.get("place") or q.get("label"),
                "time": q.get("time"),
                "pager_alert": alert or None,
                "fatalities_estimate": fatalities,
                "economic_loss_usd": economic,
                "mmi_polygon_url": mmi_polygon_url,
                "category": f"PAGER {alert.title()}" if alert else "Significant",
                "color": _PAGER_COLOR.get(alert, "#FFB300"),
            }

    results = await asyncio.gather(*(_enrich(q) for q in quakes[:100]))
    items = [r for r in results if r]
    items.sort(key=lambda x: -(x.get("magnitude") or 0))
    return _payload("pager_quakes", items)


# ===========================================================================
# REGISTRY
# ===========================================================================
DERIVED_LAYERS = [
    # Cross-layer joins — pure derivation, read from blob storage.
    {"id": "quake_exposure", "interval_s": 600, "fetch": fetch_quake_exposure},
    {"id": "dark_fleet", "interval_s": 600, "fetch": fetch_dark_fleet},
    {"id": "outbreak_airline_risk", "interval_s": 600, "fetch": fetch_outbreak_airline_risk},
    {"id": "conflict_energy_risk", "interval_s": 3600, "fetch": fetch_conflict_energy_risk},
    # Enriched: USGS PAGER + ShakeMap for significant quakes.
    {"id": "pager_quakes", "interval_s": 600, "fetch": fetch_pager_quakes},
]
