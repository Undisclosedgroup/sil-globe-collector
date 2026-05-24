"""World Events Forecast — per-hotspot escalation-score engine for the globe.

Computes a 0-100 escalation score for each of ~12 geopolitical hotspots from our
OWN live globe signals, then publishes a single `forecast` blob the dashboard
panel renders. It is DECOUPLED from the collector's per-layer loops: it reads the
already-published production API (https://www.socialintelligencelabs.com/api/
globe/<layer>) — our OWN infrastructure, so a plain stdlib (urllib) direct fetch
is correct here; this is NOT third-party scraping and needs no ProxyRack.

Per hotspot we score six signals, each normalized to 0-100, then combine them
with kinetic-weighted weights into a single 0-100 escalation score and a
human-readable level + one-sentence summary.

    military_air   military aircraft inside the box (kinetic — heavy weight)
    naval          military-relevant vessels inside the box (kinetic — heavy)
    events         GDELT geolocated conflict-event volume in the box
    nav_warnings   NGA NAVAREA maritime warnings whose coords fall in the box
    news           news headlines mentioning the hotspot's keywords
    frontlines     active DeepStateMap frontline polygons intersecting the box

The collector imports `build_forecast` and runs it on a ~300s cadence, writing
the result via the existing `put_blob("forecast", payload)`. Every signal fetch
is independently guarded: a failed/empty layer contributes a 0 sub-score with a
note, never an exception that aborts the whole forecast.

Standalone dry-run (prints Cuba/Ukraine/Iran breakdowns against the live API):
    python forecast.py
"""
import json
import math
from datetime import datetime, timezone
from urllib import request as _rq
from urllib.error import URLError, HTTPError

# Our own production globe API. Direct stdlib fetch — this is OUR infrastructure
# (not a third-party anti-bot target), so no proxy is needed or wanted.
API_BASE = "https://www.socialintelligencelabs.com/api/globe"


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Hotspot registry. Each entry: id, display name, center (lat/lng) for the globe
# marker, a bounding box (lat_min, lat_max, lng_min, lng_max), and a per-hotspot
# keyword list used to attribute news/event headlines to the region.
# Boxes are taken from the task spec. Longitudes are signed (E +, W -).
# ---------------------------------------------------------------------------
HOTSPOTS = [
    {
        "id": "cuba", "name": "Cuba / Caribbean", "lat": 22.0, "lng": -79.5,
        # 19.5-31N, 85-60W
        "box": (19.5, 31.0, -85.0, -60.0),
        "keywords": ["cuba", "caribbean", "guantanamo", "havana", "bahamas",
                     "haiti", "dominican"],
    },
    {
        "id": "ukraine", "name": "Ukraine", "lat": 49.0, "lng": 32.0,
        # 44-53N, 22-41E
        "box": (44.0, 53.0, 22.0, 41.0),
        "keywords": ["ukrain", "kyiv", "kiev", "kharkiv", "donbas", "donetsk",
                     "luhansk", "zaporizh", "crimea", "kherson", "russian forces",
                     "moscow"],
    },
    {
        "id": "iran", "name": "Iran / Persian Gulf", "lat": 32.0, "lng": 53.5,
        # 24-40N, 44-63E
        "box": (24.0, 40.0, 44.0, 63.0),
        "keywords": ["iran", "tehran", "persian gulf", "strait of hormuz",
                     "hormuz", "irgc", "revolutionary guard", "natanz"],
    },
    {
        "id": "israel", "name": "Israel / Levant", "lat": 32.5, "lng": 38.0,
        # 29-37N, 33-43E
        "box": (29.0, 37.0, 33.0, 43.0),
        "keywords": ["israel", "gaza", "hamas", "hezbollah", "lebanon",
                     "west bank", "idf", "tel aviv", "jerusalem", "syria",
                     "damascus", "golan"],
    },
    {
        "id": "taiwan", "name": "Taiwan Strait", "lat": 24.0, "lng": 120.0,
        # 21-27N, 117-123E
        "box": (21.0, 27.0, 117.0, 123.0),
        "keywords": ["taiwan", "taipei", "taiwan strait", "pla", "cross-strait",
                     "chinese military"],
    },
    {
        "id": "korea", "name": "Korean Peninsula", "lat": 38.0, "lng": 127.5,
        # 33-43N, 124-131E
        "box": (33.0, 43.0, 124.0, 131.0),
        "keywords": ["north korea", "south korea", "pyongyang", "seoul",
                     "dprk", "kim jong", "korean peninsula", "dmz"],
    },
    {
        "id": "south_china_sea", "name": "South China Sea", "lat": 13.5,
        "lng": 114.0,
        # 5-22N, 108-120E
        "box": (5.0, 22.0, 108.0, 120.0),
        "keywords": ["south china sea", "spratly", "paracel", "scarborough",
                     "philippine", "manila", "vietnam", "chinese coast guard"],
    },
    {
        "id": "kashmir", "name": "Kashmir / India-Pakistan", "lat": 32.5,
        "lng": 75.0,
        # 28-37N, 70-80E
        "box": (28.0, 37.0, 70.0, 80.0),
        "keywords": ["kashmir", "india", "pakistan", "islamabad", "new delhi",
                     "line of control", "loc", "pakistani", "indian army"],
    },
    {
        "id": "venezuela", "name": "Venezuela", "lat": 7.0, "lng": -66.5,
        # 1-13N, 73-60W
        "box": (1.0, 13.0, -73.0, -60.0),
        "keywords": ["venezuela", "caracas", "maduro", "guyana", "essequibo"],
    },
    {
        "id": "red_sea", "name": "Red Sea / Yemen", "lat": 16.0, "lng": 41.5,
        # 12-20N, 38-45E
        "box": (12.0, 20.0, 38.0, 45.0),
        "keywords": ["red sea", "yemen", "houthi", "bab el-mandeb", "bab-el-mandeb",
                     "sanaa", "gulf of aden", "aden"],
    },
    {
        "id": "russia_baltic", "name": "Russia / Baltic", "lat": 57.0,
        "lng": 24.5,
        # 54-60N, 19-30E
        "box": (54.0, 60.0, 19.0, 30.0),
        "keywords": ["baltic", "kaliningrad", "estonia", "latvia", "lithuania",
                     "gotland", "nato baltic", "suwalki"],
    },
    {
        "id": "sahel", "name": "Sahel", "lat": 14.0, "lng": 12.5,
        # 10-18N, 0-25E
        "box": (10.0, 18.0, 0.0, 25.0),
        "keywords": ["sahel", "mali", "niger", "burkina faso", "chad", "wagner",
                     "jihadist", "boko haram", "junta"],
    },
]


# ---------------------------------------------------------------------------
# Scoring weights. The total score is a weighted average of the six sub-scores
# (each already 0-100), so the result is naturally clamped to 0-100. Kinetic,
# hard-to-fake indicators (military aircraft, naval movement, maritime danger
# warnings) carry the most weight; ambient signals (news, events) carry less.
# Weights sum to 1.0.
# ---------------------------------------------------------------------------
WEIGHTS = {
    "military_air": 0.26,   # kinetic — tankers/recon/heavy lift telegraph ops
    "naval":        0.22,   # kinetic — fleet movement / carrier presence
    "nav_warnings": 0.18,   # kinetic — declared danger/exercise/missile zones
    "frontlines":   0.14,   # active ground combat polygons in the box
    "events":       0.10,   # GDELT conflict-event volume
    "news":         0.10,   # headline attention
}

# High-value military aircraft type designators (adsb `t` field). Their presence
# is a strong escalation tell: aerial refueling, ISR, and strategic lift precede
# and sustain kinetic operations. Used as a bonus on the military_air sub-score.
_HIGH_VALUE_AIR = {
    # tankers
    "KC135", "K35R", "KC10", "KC46", "A332", "A310", "VC10", "TANK",
    # recon / ISR / C2
    "RC135", "P8", "P3", "E8", "E3CF", "E3TF", "E3", "E6", "U2", "RQ4",
    "MQ9", "MQ4", "EP3", "C30J", "GLF5", "CL60",
    # heavy / strategic lift
    "C17", "C5M", "C5", "A400", "C130", "C30J", "AN12", "IL76", "A124",
    # bombers
    "B52", "B1", "B2", "TU95", "TU22", "TU160",
}


def _bbox_contains(box, lat, lng):
    """True if (lat, lng) falls inside the bounding box (lat_min, lat_max,
    lng_min, lng_max). Boxes here never cross the antimeridian, so a plain
    min/max test is correct."""
    if lat is None or lng is None:
        return False
    lat_min, lat_max, lng_min, lng_max = box
    return lat_min <= lat <= lat_max and lng_min <= lng <= lng_max


def _ring_intersects_box(ring, box):
    """True if a polygon ring (list of [lng, lat, ...] coords) either has a
    vertex inside the box or its bounding box overlaps the hotspot box. A cheap
    bbox-overlap test is enough here — frontline polygons are small relative to
    the hotspot boxes and we only need a boolean 'active combat in region'."""
    lat_min, lat_max, lng_min, lng_max = box
    rlat_min = rlng_min = float("inf")
    rlat_max = rlng_max = float("-inf")
    saw = False
    for c in ring:
        if not isinstance(c, (list, tuple)) or len(c) < 2:
            continue
        lng, lat = c[0], c[1]   # GeoJSON order
        if not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
            continue
        saw = True
        if _bbox_contains(box, lat, lng):
            return True
        rlat_min, rlat_max = min(rlat_min, lat), max(rlat_max, lat)
        rlng_min, rlng_max = min(rlng_min, lng), max(rlng_max, lng)
    if not saw:
        return False
    # bbox-overlap fallback (polygon may straddle the box without a vertex inside)
    return not (rlat_max < lat_min or rlat_min > lat_max
                or rlng_max < lng_min or rlng_min > lng_max)


def _iter_polygon_rings(geometry):
    """Yield each linear ring (list of coords) from a Polygon/MultiPolygon."""
    if not isinstance(geometry, dict):
        return
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if gtype == "Polygon" and isinstance(coords, list):
        for ring in coords:
            if isinstance(ring, list):
                yield ring
    elif gtype == "MultiPolygon" and isinstance(coords, list):
        for poly in coords:
            if isinstance(poly, list):
                for ring in poly:
                    if isinstance(ring, list):
                        yield ring


# ---------------------------------------------------------------------------
# Sub-score curves. Each returns a 0-100 int. They are intentionally concrete
# and data-grounded: a single asset is a low score, a handful is moderate, a
# concentration/cluster saturates toward the top. Log-shaped so the curve is
# steep at low counts (where the marginal signal matters most) and flattens.
# ---------------------------------------------------------------------------
def _log_curve(n, full):
    """Map a count `n` to 0-100 on a log curve that reaches ~100 at `full`.
    n=0 -> 0; n=1 is already a meaningful bump; growth flattens past `full`."""
    if n <= 0:
        return 0
    score = 100.0 * math.log1p(n) / math.log1p(full)
    return int(max(0, min(100, round(score))))


# ===========================================================================
# Live API reader (our own infra; plain urllib direct fetch — no proxy).
# ===========================================================================
def fetch_layer(layer, *, timeout=30):
    """GET our production /api/globe/<layer> and return the parsed payload dict.
    Returns None on any failure so the caller can treat the signal as absent.
    Direct fetch is correct: socialintelligencelabs.com is OUR infrastructure."""
    url = f"{API_BASE}/{layer}"
    req = _rq.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "osiris-globe-forecast/1.0",
    })
    try:
        with _rq.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read())
    except (URLError, HTTPError, TimeoutError, ValueError, OSError):
        return None


def fetch_all_layers(layers, *, timeout=30):
    """Fetch every needed layer once. A failed layer maps to None (its signal
    is skipped per-hotspot rather than crashing the whole forecast)."""
    return {lid: fetch_layer(lid, timeout=timeout) for lid in layers}


def _items(payload):
    """Safe item-list extraction from a layer payload (or None)."""
    if not isinstance(payload, dict):
        return []
    items = payload.get("items")
    return items if isinstance(items, list) else []


# ===========================================================================
# Per-signal scorers. Each takes (hotspot, layers) and returns a signal dict
# {key,label,value,score,note,evidence}. None layer -> value 0, score 0, note.
#
# `evidence` is the list of ACTUAL records the sub-score is computed from (capped
# at EVIDENCE_CAP, most-significant first) so the forecast can show its work: the
# specific aircraft / vessels / warnings / headlines behind a hotspot's number,
# not just an aggregate count. Each item is {label, sub?, lat?, lng?, url?} — the
# coords let the UI fly to the contributor; the url opens the source.
# ===========================================================================
EVIDENCE_CAP = 8


def _ev(label, *, sub=None, lat=None, lng=None, url=None):
    """Build one compact evidence item; omit empty fields to keep the blob small."""
    item = {"label": str(label)[:120]}
    if sub:
        item["sub"] = str(sub)[:120]
    if isinstance(lat, (int, float)) and isinstance(lng, (int, float)):
        item["lat"], item["lng"] = lat, lng
    if url:
        item["url"] = url
    return item


def _score_military_air(hs, layers):
    box = hs["box"]
    payload = layers.get("military_air")
    if payload is None:
        return {"key": "military_air", "label": "Military aircraft",
                "value": 0, "score": 0, "note": "feed unavailable"}
    in_box, high_value = [], []
    for a in _items(payload):
        if _bbox_contains(box, a.get("lat"), a.get("lng")):
            t = (a.get("type") or "").upper().replace("-", "").replace(" ", "")
            hv = bool(t and (t in _HIGH_VALUE_AIR or any(t.startswith(p) for p in (
                    "KC", "RC", "E3", "E6", "C17", "C5", "B52", "B1", "B2",
                    "TU95", "TU160", "P8", "RQ4", "MQ9"))))
            in_box.append((a, hv))
            if hv:
                high_value.append(t)
    n = len(in_box)
    # A cluster of ~12 military aircraft in one hotspot saturates the curve.
    base = _log_curve(n, full=12)
    # High-value-asset bonus: up to +30 for tankers/ISR/heavy lift/bombers.
    bonus = min(30, 10 * len(set(high_value)))
    score = int(max(0, min(100, base + (bonus if n else 0))))
    note = f"{n} military aircraft in box"
    if high_value:
        note += f"; high-value: {', '.join(sorted(set(high_value))[:4])}"
    # Evidence: high-value assets first, then the rest.
    in_box.sort(key=lambda x: not x[1])
    evidence = []
    for a, hv in in_box[:EVIDENCE_CAP]:
        flag = (a.get("flag") or a.get("country_code") or "").strip()
        bits = [b for b in (a.get("type"), a.get("reg"),
                            (f"{flag}" if flag else None),
                            (f"{a.get('alt')} ft" if a.get("alt") not in (None, "") else None))
                if b]
        evidence.append(_ev(
            (a.get("callsign") or a.get("reg") or a.get("type") or a.get("id") or "aircraft"),
            sub=("★ " if hv else "") + " · ".join(str(b) for b in bits),
            lat=a.get("lat"), lng=a.get("lng")))
    return {"key": "military_air", "label": "Military aircraft",
            "value": n, "score": score, "note": note, "evidence": evidence}


# AIS ship-type labels (from layers.normalize_boats) that read as military or
# military-relevant. AIS has no "warship" type, so naval combatants usually
# broadcast as these (or go dark); we count them as elevated-interest vessels.
_NAVAL_TYPES = {"Military Ops", "Law Enforcement", "Tanker", "SAR",
                "Port Tender", "Pilot Vessel", "Towing"}


def _score_naval(hs, layers):
    box = hs["box"]
    # There is no military_naval layer (FAA/NGA notes: not registered). We use
    # the AIS `boats` layer and count military-relevant ship types in the box.
    payload = layers.get("boats")
    if payload is None:
        return {"key": "naval", "label": "Naval/vessel activity",
                "value": 0, "score": 0, "note": "feed unavailable"}
    total = mil = 0
    mil_vessels = []
    for v in _items(payload):
        if _bbox_contains(box, v.get("lat"), v.get("lng")):
            total += 1
            if (v.get("ship_type") or "") in _NAVAL_TYPES:
                mil += 1
                mil_vessels.append(v)
    # Score is driven by military-relevant vessels (a fleet ~10 saturates), with
    # a small floor from sheer total traffic density (a busy contested chokepoint).
    base = _log_curve(mil, full=10)
    density = _log_curve(total, full=600) // 3   # mild traffic-density floor
    score = int(max(0, min(100, max(base, density))))
    evidence = []
    for v in mil_vessels[:EVIDENCE_CAP]:
        flag = (v.get("flag") or v.get("country") or "").strip()
        bits = [b for b in (v.get("ship_type"), flag or None,
                            v.get("destination")) if b]
        evidence.append(_ev(
            (v.get("name") or v.get("callsign") or v.get("ship_type") or "vessel"),
            sub=" · ".join(str(b) for b in bits),
            lat=v.get("lat"), lng=v.get("lng")))
    return {"key": "naval", "label": "Naval/vessel activity",
            "value": mil, "score": score,
            "note": f"{mil} military-relevant of {total} vessels in box",
            "evidence": evidence}


def _score_events(hs, layers):
    box = hs["box"]
    payload = layers.get("events")
    if payload is None:
        return {"key": "events", "label": "Event volume",
                "value": 0, "score": 0, "note": "feed unavailable"}
    in_box = [e for e in _items(payload)
              if _bbox_contains(box, e.get("lat"), e.get("lng"))]
    n = len(in_box)
    # GDELT geolocates to source-country centroids, so counts are coarse; ~8
    # geolocated conflict events near a hotspot is a strong cluster.
    score = _log_curve(n, full=8)
    # Most-conflictual headlines first (higher tone = more risk keywords).
    in_box.sort(key=lambda e: e.get("tone") or 0, reverse=True)
    evidence = [
        _ev((e.get("headline") or e.get("label") or "event"),
            sub=e.get("source_country") or e.get("domain"),
            lat=e.get("lat"), lng=e.get("lng"), url=e.get("url"))
        for e in in_box[:EVIDENCE_CAP]
    ]
    return {"key": "events", "label": "Event volume",
            "value": n, "score": score, "note": f"{n} GDELT events in box",
            "evidence": evidence}


# NAVAREA warning text keywords that escalate a maritime warning's weight.
_NAV_DANGER_KW = ("missile", "gunnery", "firing", "exercise", "rocket",
                  "live fire", "ordnance", "naval", "warship", "danger",
                  "military operation", "torpedo")


def _score_nav_warnings(hs, layers):
    box = hs["box"]
    payload = layers.get("nav_warnings")
    if payload is None:
        return {"key": "nav_warnings", "label": "Maritime warnings",
                "value": 0, "score": 0, "note": "feed unavailable"}
    n = danger = 0
    hits = []
    for w in _items(payload):
        # A warning may carry several coords; count it if any falls in the box.
        coords = w.get("coords") or []
        hit = _bbox_contains(box, w.get("lat"), w.get("lng")) or any(
            _bbox_contains(box, c[0], c[1])
            for c in coords if isinstance(c, (list, tuple)) and len(c) >= 2)
        if not hit:
            continue
        n += 1
        text = (w.get("text") or w.get("label") or "").lower()
        is_danger = any(kw in text for kw in _NAV_DANGER_KW)
        if is_danger:
            danger += 1
        hits.append((w, is_danger))
    # Danger/exercise/missile warnings count double toward the curve.
    weighted = n + danger
    score = _log_curve(weighted, full=6)
    note = f"{n} maritime warnings in box"
    if danger:
        note += f" ({danger} danger/exercise/missile)"
    # Evidence: danger/exercise/missile zones first.
    hits.sort(key=lambda x: not x[1])
    evidence = []
    for w, is_danger in hits[:EVIDENCE_CAP]:
        body = (w.get("text") or w.get("msg") or "").strip().replace("\n", " ")
        evidence.append(_ev(
            ("⚠ " if is_danger else "") + (w.get("nav_area_name")
                or w.get("nav_area") or "NAVAREA warning"),
            sub=body or w.get("authority"),
            lat=w.get("lat"), lng=w.get("lng")))
    return {"key": "nav_warnings", "label": "Maritime warnings",
            "value": n, "score": score, "note": note, "evidence": evidence}


def _score_news(hs, layers):
    payload = layers.get("news")
    if payload is None:
        return {"key": "news", "label": "News mentions",
                "value": 0, "score": 0, "note": "feed unavailable"}
    kws = hs["keywords"]
    n = risk_hits = 0
    matched = []
    for art in _items(payload):
        text = f"{art.get('title') or ''} {art.get('summary') or ''}".lower()
        if any(kw in text for kw in kws):
            n += 1
            risk = art.get("risk_score") or 0
            if risk >= 3:
                risk_hits += 1
            matched.append((art, risk))
    # News is ambient attention; ~6 region-matched headlines saturates, with a
    # bump when matched headlines also score high on the conflict-keyword scale.
    score = _log_curve(n + risk_hits, full=6)
    note = f"{n} news items mention region"
    if risk_hits:
        note += f" ({risk_hits} high-conflict)"
    # Evidence: highest-conflict headlines first (no coords — link out instead).
    matched.sort(key=lambda x: x[1], reverse=True)
    evidence = [
        _ev(art.get("title") or "headline",
            sub=art.get("source_feed"), url=art.get("link"))
        for art, _ in matched[:EVIDENCE_CAP]
    ]
    return {"key": "news", "label": "News mentions",
            "value": n, "score": score, "note": note, "evidence": evidence}


def _score_frontlines(hs, layers):
    box = hs["box"]
    payload = layers.get("frontlines")
    if payload is None:
        return {"key": "frontlines", "label": "Active frontlines",
                "value": 0, "score": 0, "note": "feed unavailable"}
    n = 0
    matched = []
    for feat in _items(payload):
        geom = feat.get("geometry") if isinstance(feat, dict) else None
        for ring in _iter_polygon_rings(geom):
            if _ring_intersects_box(ring, box):
                n += 1
                matched.append(feat)
                break   # count the feature once
    # Active ground combat is binary-ish but more polygons = a wider/hotter
    # front; a handful intersecting the box is already a crisis-level kinetic tell.
    if n == 0:
        score = 0
    elif n == 1:
        score = 55
    else:
        score = int(min(100, 55 + _log_curve(n, full=10) // 2 + 20))
    evidence = []
    for feat in matched[:EVIDENCE_CAP]:
        props = feat.get("properties") if isinstance(feat, dict) else None
        props = props if isinstance(props, dict) else {}
        name = props.get("name") or props.get("title") or "frontline zone"
        evidence.append(_ev(name, sub=props.get("description") or props.get("status")))
    return {"key": "frontlines", "label": "Active frontlines",
            "value": n, "score": score,
            "note": f"{n} active frontline polygons intersect box",
            "evidence": evidence}


_SIGNAL_FNS = [
    _score_military_air, _score_naval, _score_events,
    _score_nav_warnings, _score_news, _score_frontlines,
]


def _level(score):
    if score >= 75:
        return "crisis"
    if score >= 50:
        return "high"
    if score >= 25:
        return "elevated"
    return "routine"


def _summary(hs, signals, total, level):
    """One-sentence plain-language read naming the top contributing signals."""
    contributing = sorted(
        (s for s in signals if s["score"] > 0),
        key=lambda s: s["score"] * WEIGHTS.get(s["key"], 0), reverse=True)
    name = hs["name"]
    if not contributing:
        return f"{name}: routine — no notable signals across our live feeds."
    top = contributing[:2]
    drivers = " and ".join(s["label"].lower() for s in top)
    lead = {
        "crisis": "Crisis-level escalation",
        "high": "Elevated escalation",
        "elevated": "Above-baseline activity",
        "routine": "Routine activity",
    }[level]
    return (f"{name}: {lead} (score {total}/100) driven mainly by {drivers}"
            f"; {top[0]['note']}.")


def score_hotspot(hs, layers):
    """Compute the full forecast entry for one hotspot from the fetched layers."""
    signals = []
    for fn in _SIGNAL_FNS:
        try:
            signals.append(fn(hs, layers))
        except Exception as e:   # one signal must never break the hotspot
            key = fn.__name__.replace("_score_", "")
            signals.append({"key": key, "label": key, "value": 0, "score": 0,
                            "note": f"error: {type(e).__name__}"})
    # Weighted average of sub-scores (weights sum to 1.0 -> already 0-100).
    total = sum(s["score"] * WEIGHTS.get(s["key"], 0) for s in signals)
    total = int(max(0, min(100, round(total))))
    level = _level(total)
    return {
        "id": hs["id"], "name": hs["name"], "lat": hs["lat"], "lng": hs["lng"],
        "score": total, "level": level,
        "signals": signals,
        "summary": _summary(hs, signals, total, level),
    }


# Ordered list of the layers the forecast reads (used by fetch_all_layers).
NEEDED_LAYERS = ["military_air", "boats", "events", "nav_warnings", "news",
                 "frontlines"]


def build_forecast(*, timeout=30):
    """Fetch every needed layer once, score all hotspots, return the blob payload
    in the exact output schema. Pure orchestration — never raises on a bad layer
    (each missing layer is treated as an absent signal)."""
    payload, _layers = build_forecast_full(timeout=timeout)
    return payload


def build_forecast_full(*, timeout=30):
    """Like build_forecast but also returns the raw `layers` dict it fetched, so
    callers that derive further products (intel brief, alerts, history) can reuse
    the same single API-read pass instead of fetching every layer again."""
    layers = fetch_all_layers(NEEDED_LAYERS, timeout=timeout)
    hotspots = [score_hotspot(hs, layers) for hs in HOTSPOTS]
    hotspots.sort(key=lambda h: h["score"], reverse=True)
    return {"updatedAt": _now_iso(), "hotspots": hotspots}, layers


# ===========================================================================
# Standalone dry-run: compute against the live API and print Cuba/Ukraine/Iran.
# ===========================================================================
def _print_breakdown(entry):
    print(f"\n=== {entry['name']}  score={entry['score']}/100  "
          f"level={entry['level']} ===")
    for s in entry["signals"]:
        w = WEIGHTS.get(s["key"], 0)
        print(f"  {s['label']:<26} value={s['value']:<5} "
              f"score={s['score']:>3}  (w={w:>4})  {s['note']}")
    print(f"  summary: {entry['summary']}")


if __name__ == "__main__":
    print("Building forecast against live production API ...")
    fc = build_forecast()
    print(f"updatedAt: {fc['updatedAt']}   hotspots: {len(fc['hotspots'])}")
    by_id = {h["id"]: h for h in fc["hotspots"]}
    for hid in ("cuba", "ukraine", "iran"):
        if hid in by_id:
            _print_breakdown(by_id[hid])
    print("\n--- all hotspot scores (desc) ---")
    for h in fc["hotspots"]:
        print(f"  {h['score']:>3}  {h['level']:<9} {h['name']}")
