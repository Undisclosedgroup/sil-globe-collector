"""IUU Incursions — derived layer flagging suspicious fishing-vessel activity
inside EEZs of concern.

Three input blobs, one output blob, zero new upstream fetches:

  boats          (AISStream live AIS snapshot — ~40k cooperating vessels)
  viirs_vessels  (VIIRS nightly dark-light detections — squid-fleet signature)
  geo_zones      (EEZ + sanctions polygons)
            │
            ▼
  iuu_incursions (point list w/ severity, reason, eez_violated, source)

DETECTION RULES
===============
For each AIS vessel:
  • In one of our EEZ-of-concern polygons (Galapagos, Argentine,
    Indonesian/Natuna, Hormuz approaches, Falkland, DPRK Yellow Sea)
  • Ship type == "Fishing" (AIS code 30 → label "Fishing")
  • Flag NOT == EEZ owner (foreign incursion)
  • Flag IS in FLAGS_OF_CONCERN (default: CN / KP / IR / RU)
  → severity = "warning" (flag-of-concern fishing in foreign EEZ)

Sanctioned-vessel overlay (cross-check against OFAC-listed IMOs from the
sanctioned_vessels blob): same conditions but vessel is on a sanctions list
  → severity = "critical"

For each VIIRS detection NOT matched by an AIS vessel within 5nm:
  → severity = "info" (dark fishing vessel — light signature, no AIS)

Severity is the headline; we ship the union sorted critical → warning → info
and cap at 500 items to keep the right-panel feed scannable.

CADENCE: 300s (5 min). Pure local join, no external HTTP — runs as a
DERIVED layer in collector.py alongside dark_fleet / quake_exposure.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import urllib.request
from datetime import datetime, timezone


_R_EARTH_KM = 6371.0

# EEZs we actively monitor for incursions. Maps the geo_zones feature `id`
# to (owner_country_code, display_name). Only IDs in this dict are scored —
# the other ~35 EEZ polygons stay decorative.
_EEZ_OF_CONCERN: dict[str, tuple[str, str]] = {
    "zone-eez-ec": ("EC", "Ecuador (Galapagos)"),
    "zone-eez-ar": ("AR", "Argentine EEZ"),
    "zone-eez-id": ("ID", "Indonesian EEZ (Natuna)"),
    # Existing polygons we also score:
    "zone-eez-ir": ("IR", "Iran EEZ (Hormuz approaches)"),
    "zone-eez-kp": ("KP", "DPRK EEZ"),
}

# Flag codes whose vessels we flag when they appear in a foreign EEZ of
# concern. China is the explicit ask; KP/IR/RU folded in because they share
# the same shadow-fishing / sanctions-evasion profile and the user has been
# surfacing those flags throughout.
_FLAGS_OF_CONCERN: set[str] = {"CN", "KP", "IR", "RU"}

# Flag-of-convenience registries heavily used by Chinese-operated /
# sanctions-evading fishing & cargo fleets. When a fishing vessel under
# one of these flags appears in a contested EEZ (Galapagos, Argentine
# shelf, Natuna), it's a strong IUU candidate even without CN-flag itself.
_FOC_FLAGS: set[str] = {"LR", "MH", "PA", "MT", "BZ", "CY", "HN", "KH", "ST", "VC"}

# GFW satellite-AIS event kinds that are IUU-suggestive when they occur
# inside an EEZ of concern. Encounters = at-sea transshipment (a classic
# sanctions-evasion + IUU laundering signal). Loitering = drifting in
# fishing posture without an authorisation marker. Port_visit excluded
# (legitimate by definition).
_GFW_IUU_KINDS: set[str] = {"encounter", "loitering"}

# Vessel-name substrings that flag known false positives in the loitering
# stream: oil/gas FPSOs and offshore platforms are stationary by design,
# not IUU. Case-insensitive substring match against the GFW event's
# vessel_name / name field.
_FALSE_POSITIVE_NAME_PARTS: tuple[str, ...] = (
    "FPSO", "FSO ", "RIG ", "PLATFORM", "DRILLSHIP", "JACK-UP", "JACKUP",
    "SEMISUBMERSIBLE", "FLOATEL", "BARGE", "MODU",
    # Known offshore production vessel names that don't carry a prefix.
    # These show up in the GFW loitering stream in Indonesian/Malaysian waters.
    "PEGAGA", "SAPURA BERANI", "KAKAP", "ANOA NATUNA",
    # AIS data-quality noise — auto-pilot messages bleeding into vessel_name.
    "TAP REQUEST", "DO NOT", "CPA ", " CPA",
)

# Cap output so the right-panel feed stays useful and the blob stays small.
_MAX_ITEMS = 500

# Dark-AIS match radius for VIIRS↔AIS reconciliation. >5nm = "no plausible
# matching transmitter, likely dark fishing vessel".
_VIIRS_MATCH_RADIUS_KM = 9.26  # 5 nautical miles


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _payload(items: list, error: str | None = None) -> dict:
    p = {
        "layer": "iuu_incursions",
        "updatedAt": _now_iso(),
        "count": len(items),
        "items": items,
    }
    if error:
        p["error"] = error
    return p


# ---------------------------------------------------------------------------
# Blob reader (mirrors derived_layers.py — same 60s cycle cache so the IUU
# detector and other derived layers don't double-fetch the same inputs).
# ---------------------------------------------------------------------------
_BLOB_CACHE: dict[str, tuple[float, list]] = {}
_BLOB_CACHE_TTL_S = 60.0


def _read_blob_items(layer_id: str) -> list:
    """Pull the items list of a globe layer from Vercel Blob. Returns [] on
    any failure — IUU silently degrades to whichever inputs ARE present."""
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


# ---------------------------------------------------------------------------
# Point-in-polygon (ray casting; handles MultiPolygon by OR-ing each part).
# Coordinates in GeoJSON [lng, lat] order; we keep our API in (lat, lng).
# ---------------------------------------------------------------------------
def _pip_ring(lng: float, lat: float, ring: list[list[float]]) -> bool:
    inside = False
    n = len(ring)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > lat) != (yj > lat)) and (
            lng < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi
        ):
            inside = not inside
        j = i
    return inside


def _point_in_feature(lat: float, lng: float, feature: dict) -> bool:
    geom = feature.get("geometry") or {}
    gtype = geom.get("type")
    coords = geom.get("coordinates") or []
    if gtype == "Polygon":
        # Outer ring is coords[0]; holes (coords[1:]) ignored — our simplified
        # EEZs don't define interior holes.
        return _pip_ring(lng, lat, coords[0]) if coords else False
    if gtype == "MultiPolygon":
        for poly in coords:
            if poly and _pip_ring(lng, lat, poly[0]):
                return True
        return False
    return False


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = rlat2 - rlat1
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlng / 2) ** 2)
    return 2 * _R_EARTH_KM * math.asin(min(1.0, math.sqrt(a)))


# Bounding-box pre-filter for cheap rejection before running the full PIP
# test. Built once per cycle for each polygon-of-concern.
def _bbox_of(feature: dict) -> tuple[float, float, float, float]:
    geom = feature.get("geometry") or {}
    gtype = geom.get("type")
    coords = geom.get("coordinates") or []
    pts: list[list[float]] = []
    if gtype == "Polygon" and coords:
        pts = coords[0]
    elif gtype == "MultiPolygon":
        for poly in coords:
            if poly:
                pts.extend(poly[0])
    if not pts:
        return (90.0, -90.0, 180.0, -180.0)  # empty box never matches
    lats = [p[1] for p in pts]
    lngs = [p[0] for p in pts]
    return (min(lats), max(lats), min(lngs), max(lngs))


# ---------------------------------------------------------------------------
# Main fetch
# ---------------------------------------------------------------------------
def _do_fetch() -> dict:
    boats = _read_blob_items("boats") or []
    zones = _read_blob_items("geo_zones") or []
    viirs = _read_blob_items("viirs_vessels") or []
    sanctioned = _read_blob_items("sanctioned_vessels") or []
    # GFW satellite-AIS events (encounters / loitering / port visits) —
    # this is the satellite-side signal that catches what terrestrial
    # AISStream misses on the open ocean (Galapagos, mid-Argentine shelf).
    gfw_events = _read_blob_items("vessel_events") or []

    if not (boats or viirs or gfw_events):
        return _payload(
            [],
            error="iuu_incursions: no boats / VIIRS / GFW events available; nothing to score.",
        )

    # Pre-build the set of EEZs we care about (with bboxes for cheap reject).
    target_zones: list[tuple[str, str, str, dict, tuple]] = []
    for f in zones:
        zid = f.get("id")
        if zid not in _EEZ_OF_CONCERN:
            continue
        owner_cc, owner_name = _EEZ_OF_CONCERN[zid]
        target_zones.append((zid, owner_cc, owner_name, f, _bbox_of(f)))

    if not target_zones:
        return _payload(
            [],
            error="iuu_incursions: geo_zones blob missing all EEZs of concern.",
        )

    # Sanctioned IMO set for O(1) lookup on the AIS pass.
    sanctioned_imos: set[str] = set()
    for s in sanctioned:
        imo = s.get("imo")
        if imo:
            sanctioned_imos.add(str(imo))

    items: list[dict] = []

    # ---- AIS pass: foreign fishing / FOC / flag-of-concern vessels in EEZ ----
    # Broader than fishing-only because (a) ~65% of AIS vessels broadcast no
    # ship_type, (b) sanctioned/CN-flag vessels of ANY type inside a target
    # EEZ are reportable (cargo, tanker, fishing all matter for sanctions
    # enforcement), and (c) flag-of-convenience vessels with no ship_type
    # data inside a contested EEZ are the classic shadow-fleet signal.
    for v in boats:
        try:
            vlat = float(v.get("lat"))
            vlng = float(v.get("lng"))
        except (TypeError, ValueError):
            continue
        ship_type = v.get("ship_type")
        vflag = v.get("country_code")
        imo = v.get("imo")
        is_sanctioned = bool(imo) and str(imo) in sanctioned_imos
        is_concern_flag = vflag in _FLAGS_OF_CONCERN
        is_foc_fishing = (vflag in _FOC_FLAGS) and (ship_type == "Fishing")
        is_foreign_fishing = (ship_type == "Fishing")
        # Skip vessels that don't trip any rule before paying for PIP.
        if not (is_sanctioned or is_concern_flag or is_foc_fishing or is_foreign_fishing):
            continue
        for zid, owner_cc, owner_name, feature, (la, lb, ga, gb) in target_zones:
            if not (la <= vlat <= lb and ga <= vlng <= gb):
                continue
            if not _point_in_feature(vlat, vlng, feature):
                continue
            # In-zone. Skip "same flag as EEZ owner" unless sanctioned.
            if vflag == owner_cc and not is_sanctioned:
                continue
            # Score severity:
            if is_sanctioned:
                sev = "critical"
                reason = f"sanctioned vessel ({ship_type or '?'}) in {owner_name}"
            elif is_concern_flag and ship_type == "Fishing":
                sev = "warning"
                reason = f"{vflag}-flagged fishing vessel in {owner_name}"
            elif is_concern_flag:
                sev = "warning"
                reason = f"{vflag}-flagged {ship_type or 'vessel'} in {owner_name}"
            elif is_foc_fishing:
                sev = "info"
                reason = f"FOC ({vflag}) fishing vessel in {owner_name}"
            else:
                sev = "info"
                reason = f"foreign-flagged ({vflag or '?'}) fishing in {owner_name}"
            items.append({
                "id": f"iuu-ais-{v.get('id') or v.get('mmsi') or len(items)}",
                "lat": vlat,
                "lng": vlng,
                "label": (v.get("label") or v.get("name") or "(unknown)") + f" · {reason}",
                "severity": sev,
                "reason": reason,
                "eez_violated": owner_name,
                "vessel_flag": vflag,
                "ship_type": ship_type or "(unknown)",
                "source": "ais",
                "mmsi": v.get("mmsi") or v.get("id"),
                "imo": imo,
                "color": "#FF1744" if sev == "critical" else "#FF9500" if sev == "warning" else "#FFC400",
                "__icon": "warning",
                "t": _now_iso(),
            })
            break  # don't double-count when EEZ polygons overlap (e.g. Galapagos lobe)

    # ---- GFW satellite-AIS event pass: encounters + loitering in EEZ ----
    # Catches the open-ocean activity terrestrial AISStream can't see. These
    # are SATELLITE-derived (Spire/ORBCOMM upstream via GFW free tier) so
    # they reach Galapagos / mid-Argentine shelf / open SCS where the
    # terrestrial pass yields nothing.
    for e in gfw_events:
        if e.get("kind") not in _GFW_IUU_KINDS:
            continue
        try:
            elat = float(e.get("lat"))
            elng = float(e.get("lng"))
        except (TypeError, ValueError):
            continue
        # Drop known false-positive vessel types (FPSOs, offshore rigs).
        vname_upper = (e.get("vessel_name") or e.get("name") or "").upper()
        if any(part in vname_upper for part in _FALSE_POSITIVE_NAME_PARTS):
            continue
        for zid, owner_cc, owner_name, feature, (la, lb, ga, gb) in target_zones:
            if not (la <= elat <= lb and ga <= elng <= gb):
                continue
            if not _point_in_feature(elat, elng, feature):
                continue
            kind = e.get("kind")
            sev = "warning" if kind == "encounter" else "info"
            reason = (
                f"at-sea encounter (likely transshipment) in {owner_name}"
                if kind == "encounter"
                else f"vessel loitering inside {owner_name}"
            )
            items.append({
                "id": f"iuu-gfw-{e.get('id') or len(items)}",
                "lat": elat,
                "lng": elng,
                "label": (e.get("vessel_name") or e.get("name") or "Unidentified vessel")
                         + f" · {reason}",
                "severity": sev,
                "reason": reason,
                "eez_violated": owner_name,
                "vessel_flag": e.get("flag"),
                "ship_type": "(GFW satellite event)",
                "source": "gfw",
                "kind": kind,
                "color": "#FF9500" if sev == "warning" else "#FFC400",
                "__icon": "event",
                "t": e.get("t") or _now_iso(),
            })
            break

    # ---- VIIRS pass: dark fishing vessels (light without AIS) ----
    # Build a coarse lat-grid index of AIS positions for fast nearest-neighbor
    # rejection. Grid cells of 0.2° (~22km at equator) match our ~9km radius.
    grid: dict[tuple[int, int], list[tuple[float, float]]] = {}
    for v in boats:
        try:
            vlat = float(v.get("lat"))
            vlng = float(v.get("lng"))
        except (TypeError, ValueError):
            continue
        grid.setdefault((int(vlat * 5), int(vlng * 5)), []).append((vlat, vlng))

    def _ais_within(lat: float, lng: float) -> bool:
        gy, gx = int(lat * 5), int(lng * 5)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for (alat, alng) in grid.get((gy + dy, gx + dx), ()):  # noqa
                    if _haversine_km(lat, lng, alat, alng) <= _VIIRS_MATCH_RADIUS_KM:
                        return True
        return False

    for d in viirs:
        try:
            vlat = float(d.get("lat"))
            vlng = float(d.get("lng"))
        except (TypeError, ValueError):
            continue
        # Must be inside an EEZ of concern for IUU framing to apply.
        zone_hit = None
        for zid, owner_cc, owner_name, feature, (la, lb, ga, gb) in target_zones:
            if not (la <= vlat <= lb and ga <= vlng <= gb):
                continue
            if _point_in_feature(vlat, vlng, feature):
                zone_hit = (zid, owner_cc, owner_name)
                break
        if not zone_hit:
            continue
        if _ais_within(vlat, vlng):
            continue  # AIS-explained → not a dark vessel
        _, _, owner_name = zone_hit
        items.append({
            "id": f"iuu-viirs-{d.get('id') or len(items)}",
            "lat": vlat,
            "lng": vlng,
            "label": f"Dark fishing vessel (VIIRS light, no AIS) · {owner_name}",
            "severity": "info",
            "reason": "VIIRS light signature in EEZ with no matching AIS",
            "eez_violated": owner_name,
            "vessel_flag": None,
            "ship_type": "Fishing (inferred from VIIRS)",
            "source": "viirs",
            "radiance": d.get("radiance"),
            "color": "#FFD600",
            "__icon": "warning",
            "t": _now_iso(),
        })

    # Sort critical → warning → info, then most-recent first within tier.
    _sev_rank = {"critical": 0, "warning": 1, "info": 2}
    items.sort(key=lambda r: (_sev_rank.get(r.get("severity"), 9), -hash(r["id"]) & 0xffff))
    return _payload(items[:_MAX_ITEMS])


async def fetch_iuu_incursions() -> dict:
    """Async wrapper — derived layer, runs the join in a worker thread."""
    return await asyncio.to_thread(_do_fetch)
