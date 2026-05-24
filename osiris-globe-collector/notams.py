"""FAA TFR (Temporary Flight Restrictions) adapter — the "airspace closure"
channel for the globe.

Why TFRs not "true" NOTAMs: the canonical FAA NOTAM API
(https://external-api.faa.gov/notamapi/v1/notams) is gateway-gated — every
request needs a registered client_id/client_secret and an unauthenticated probe
returns 404 "No context-path matches the request URI" (verified live
2026-05-21, see the comment block in layers.py). The FAA's TFR system
(https://tfr.faa.gov/tfr3/) is a strict SUBSET of NOTAMs — it is the
FDC-NOTAM-issued, geometry-bearing, currently-in-force restrictions: VIP
movements, hazard areas (rocket launches, military exercises, missile-test
range hot-times), security closures (over military bases, summits, nuclear
sites), space operations (every SpaceX / ULA launch window), air shows, and
UAS public gatherings. That covers the high-signal, pre-kinetic indicators the
"airspace closure" layer is meant to surface, without needing a key.

The Nuxt frontend at tfr.faa.gov/tfr3/ talks to two open endpoints under
tfr.faa.gov: a GeoServer WFS that returns one polygon per active TFR with the
`LEGAL` classification (SECURITY / HAZARDS / SPACE OPERATIONS / VIP / AIR
SHOWS/SPORTS / UAS PUBLIC GATHERING), and a tfrapi endpoint `getTfrList` that
returns a parallel list with the human-readable description + mod_date keyed
by the same notam_id. We join the two on notam_id, compute the polygon
centroid for the point marker, and parse the date span out of the description.

Endpoints (both keyless, no auth, JSON):
  https://tfr.faa.gov/geoserver/TFR/ows?service=WFS&version=1.1.0
      &request=GetFeature&typeName=TFR:V_TFR_LOC&maxFeatures=500
      &outputFormat=application/json
  https://tfr.faa.gov/tfrapi/getTfrList

Coverage caveat: this is US AIRSPACE ONLY (FAA jurisdiction = US + a few US
territories like Guam / PR). It does NOT include international NOTAMs
(EUROCONTROL, ICAO NOF feeds), military airspace activations outside the US,
nor pre-strike airspace closures in conflict zones — but the NGA NAVAREA
warnings layer already covers the maritime danger-zone / missile-test
pre-strike signal globally, and this layer is the airspace-side complement.

Schema (one item per active TFR):
    { id, lat, lng, label, type, classification, country, issued,
      effective_start, effective_end, body, color }

Where `type` is normalized to our vocabulary:
    airspace_closure  (LEGAL=SECURITY)
    space_operation   (LEGAL=SPACE OPERATIONS)
    hazard            (LEGAL=HAZARDS)
    vip_movement      (LEGAL=VIP)
    air_show          (LEGAL=AIR SHOWS/SPORTS)
    uas_event         (LEGAL=UAS PUBLIC GATHERING)
"""
import json
import re
from datetime import datetime, timezone
from urllib import request as _rq
from urllib.error import URLError, HTTPError

WFS_URL = ("https://tfr.faa.gov/geoserver/TFR/ows?service=WFS&version=1.1.0"
           "&request=GetFeature&typeName=TFR:V_TFR_LOC&maxFeatures=500"
           "&outputFormat=application/json")
LIST_URL = "https://tfr.faa.gov/tfrapi/getTfrList"

HTTP_TIMEOUT = 30
RETRY_STATUS = {429, 502, 503, 504, 524}
MAX_RETRIES = 3
MAX_ITEMS = 500

# Map FAA LEGAL classification -> our normalized type vocabulary +
# per-type marker color (warmer = higher pre-kinetic significance).
_LEGAL_TO_TYPE = {
    "SECURITY":             "airspace_closure",
    "HAZARDS":              "hazard",
    "SPACE OPERATIONS":     "space_operation",
    "VIP":                  "vip_movement",
    "AIR SHOWS/SPORTS":     "air_show",
    "UAS PUBLIC GATHERING": "uas_event",
}
_TYPE_COLOR = {
    "airspace_closure": "#ff3d3d",  # red       - over military bases / nuclear / summits
    "hazard":           "#ff7043",  # orange    - rocket range / missile test / exercise area
    "space_operation":  "#ab47bc",  # purple    - launch windows
    "vip_movement":     "#ffd54f",  # amber     - POTUS / VP / dignitary
    "air_show":         "#26c6da",  # cyan      - airshow / sport event
    "uas_event":        "#9ccc65",  # green     - drone-restricted public gathering
}
# Significance ranking — when we trim to MAX_ITEMS we keep the highest-signal
# types first. Lower number = higher priority.
_TYPE_PRIORITY = {
    "airspace_closure": 0,
    "hazard":           1,
    "space_operation":  2,
    "vip_movement":     3,
    "air_show":         4,
    "uas_event":        5,
}

# Description date span parser. FAA TFR descriptions use a fairly regular
# English form: "<LOCATION>, <Weekday>, <Month DD, YYYY> [through <Weekday>,
# <Month DD, YYYY>] [UTC|Local]". We parse the first date as effective_start
# and the optional "through" date as effective_end. If only one date is
# present, effective_end = effective_start (single-day TFR). Anything that
# doesn't parse cleanly stays None.
_DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+(\d{1,2}),\s+(\d{4})",
    re.IGNORECASE,
)
_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], start=1)}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _payload(items, *, error=None):
    p = {"layer": "notams", "updatedAt": _now(),
         "count": len(items), "items": items}
    if error:
        p["error"] = error
    return p


def _get_json(url, *, timeout=HTTP_TIMEOUT):
    """Open GET with retry on 429/5xx. Returns parsed JSON or None on failure
    (the caller never raises — a dead upstream just yields an empty payload).
    Honors adaptive budget: skips when the budget module flags faa_tfr as red,
    and records every actual call so the budget tier can re-evaluate."""
    try:
        from budget import _budget
        from quota import _tracker
        if _budget.should_skip("faa_tfr"):
            return None
        _tracker.record("faa_tfr")
    except Exception:
        pass
    import time as _t
    headers = {
        "Accept": "application/json",
        "User-Agent": "osiris-globe-collector/1.0 (+faa-tfr)",
    }
    for attempt in range(MAX_RETRIES + 1):
        req = _rq.Request(url, headers=headers)
        try:
            with _rq.urlopen(req, timeout=timeout) as resp:
                if resp.status == 200:
                    return json.loads(resp.read())
                if resp.status not in RETRY_STATUS:
                    return None
        except HTTPError as e:
            if e.code not in RETRY_STATUS:
                return None
        except (URLError, TimeoutError, ValueError, OSError):
            pass
        if attempt < MAX_RETRIES:
            _t.sleep(2 ** attempt)
    return None


def _shoelace(coords):
    """Signed shoelace area (lat-lng degrees², sign indicates winding). Used
    both for the centroid math and for picking the largest of multiple
    concentric rings on the same TFR."""
    a = 0.0
    n = len(coords)
    for i in range(n):
        x0, y0 = coords[i][0], coords[i][1]
        x1, y1 = coords[(i + 1) % n][0], coords[(i + 1) % n][1]
        a += x0 * y1 - x1 * y0
    return 0.5 * a


def _centroid(coords):
    """Area-weighted polygon centroid. coords is a polygon ring (list of
    [lng, lat] pairs, first==last). Returns (lat, lng) or None if the polygon
    is degenerate."""
    if not coords or len(coords) < 3:
        return None
    a = 0.0
    cx = 0.0
    cy = 0.0
    n = len(coords)
    for i in range(n):
        x0, y0 = coords[i][0], coords[i][1]
        x1, y1 = coords[(i + 1) % n][0], coords[(i + 1) % n][1]
        cross = x0 * y1 - x1 * y0
        a += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    a *= 0.5
    if abs(a) < 1e-12:
        xs = [p[0] for p in coords]
        ys = [p[1] for p in coords]
        return (sum(ys) / len(ys), sum(xs) / len(xs))
    cx /= (6 * a)
    cy /= (6 * a)
    # GeoJSON order is [lng, lat] -> return (lat, lng).
    return (cy, cx)


def _polygon_centroid_and_area(geometry):
    """Geometry -> ((lat, lng), |area|) or (None, 0). Handles Polygon and
    MultiPolygon. Area is in degrees² and is only used for ranking concentric
    rings — exact units don't matter, just monotonicity."""
    if not isinstance(geometry, dict):
        return None, 0.0
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if gtype == "Polygon" and coords:
        ring = coords[0]
        c = _centroid(ring)
        return c, abs(_shoelace(ring)) if c else 0.0
    if gtype == "MultiPolygon" and coords and coords[0]:
        ring = coords[0][0]
        c = _centroid(ring)
        return c, abs(_shoelace(ring)) if c else 0.0
    return None, 0.0


def _parse_dates(description):
    """Best-effort parse of (effective_start, effective_end) ISO dates from
    a TFR description like 'Atlantic City, NJ, Friday, May 29, 2026 through
    Sunday, May 31, 2026 UTC'. Returns (start_iso, end_iso) — either can be
    None if not present. Both are date-only (no time-of-day component; the
    description carries broad dates, not precise activation windows)."""
    if not description:
        return None, None
    matches = _DATE_RE.findall(description)
    if not matches:
        return None, None

    def _iso(m):
        month, day, year = m
        try:
            mi = _MONTHS[month.lower()]
            return datetime(int(year), mi, int(day),
                            tzinfo=timezone.utc).date().isoformat()
        except (KeyError, ValueError):
            return None

    start = _iso(matches[0])
    end = _iso(matches[1]) if len(matches) > 1 else start
    return start, end


def _mod_date_to_iso(mod_abs_time):
    """getTfrList carries mod_abs_time as YYYYMMDDHHMM (UTC-ish; the FAA
    treats this as the NOTAM modification timestamp). Returns ISO 8601 or None."""
    if not mod_abs_time:
        return None
    s = str(mod_abs_time).strip()
    if len(s) < 12 or not s.isdigit():
        return None
    try:
        return datetime(int(s[0:4]), int(s[4:6]), int(s[6:8]),
                        int(s[8:10]), int(s[10:12]),
                        tzinfo=timezone.utc).isoformat()
    except ValueError:
        return None


def _notam_from_key(notam_key):
    """WFS NOTAM_KEY is e.g. '6/0092-1-FDC-F' — strip the trailing suffix to
    get the bare notam_id ('6/0092') that getTfrList uses as its key."""
    if not notam_key:
        return None
    return notam_key.split("-", 1)[0]


def _build_items(features, list_by_id):
    """Join WFS features (geometry + LEGAL) with getTfrList rows (description
    + mod_date) by notam_id; emit normalized point items. A single TFR can
    appear in the WFS as multiple concentric rings (one feature per ring) —
    we dedupe by notam_id and keep the largest-area ring (outermost), which
    best represents the closure footprint as a single point."""
    # First pass: pick the largest-area ring per notam_id.
    by_id = {}  # notam_id -> (area, feat, centroid)
    for feat in features:
        if not isinstance(feat, dict):
            continue
        props = feat.get("properties") or {}
        notam_id = _notam_from_key(props.get("NOTAM_KEY"))
        if not notam_id:
            continue
        centroid, area = _polygon_centroid_and_area(feat.get("geometry"))
        if not centroid:
            continue
        lat, lng = centroid
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            continue
        prev = by_id.get(notam_id)
        if prev is None or area > prev[0]:
            by_id[notam_id] = (area, feat, centroid)

    items = []
    for notam_id, (_area, feat, centroid) in by_id.items():
        props = feat.get("properties") or {}
        lat, lng = centroid
        legal = props.get("LEGAL") or ""
        kind = _LEGAL_TO_TYPE.get(legal, "airspace_closure")
        list_row = list_by_id.get(notam_id) or {}
        description = (list_row.get("description")
                       or props.get("TITLE") or "").strip()
        eff_start, eff_end = _parse_dates(description)
        issued = _mod_date_to_iso(list_row.get("mod_abs_time"))
        state = props.get("STATE") or list_row.get("state")
        # Compact one-line label: type tag + location.
        type_label = {
            "airspace_closure": "Airspace Closure",
            "hazard":           "Hazard Area",
            "space_operation":  "Space Operation",
            "vip_movement":     "VIP Movement",
            "air_show":         "Air Show",
            "uas_event":        "UAS Event",
        }.get(kind, "TFR")
        label_loc = description.split(",")[0].strip() if description else (state or "")
        label = f"{type_label} — {label_loc}" if label_loc else type_label
        items.append({
            "id": f"tfr-{notam_id}",
            "lat": round(lat, 5), "lng": round(lng, 5),
            "label": label,
            "type": kind,
            "classification": legal or None,
            "country": "US",
            "state": state or None,
            "facility": props.get("CNS_LOCATION_ID") or list_row.get("facility"),
            "notam_id": notam_id,
            "notam_key": props.get("NOTAM_KEY"),
            "issued": issued,
            "effective_start": eff_start,
            "effective_end": eff_end,
            "body": description[:500] or None,
            "source": "FAA TFR",
            "source_url": f"https://tfr.faa.gov/save_pages/detail_{notam_id.replace('/', '_')}.html",
            "color": _TYPE_COLOR.get(kind, "#ff7043"),
            # The actual GeoJSON polygon of the closure — kept so the UI can
            # highlight the real restricted-airspace shape on click (instead of
            # just dropping a centroid dot). One Feature per outer ring per
            # TFR; the inner ring(s) of concentric TFRs are intentionally
            # dropped at the dedupe step.
            "geometry": feat.get("geometry"),
        })
    # Stable sort: high-priority types first, then most-recently issued.
    items.sort(key=lambda x: (
        _TYPE_PRIORITY.get(x["type"], 99),
        # reverse-order by issued; None sorts last
        -(int((x["issued"] or "").replace("-", "").replace(":", "")
              .replace("T", "").replace("+", "").split(".")[0][:14] or 0)
          if x.get("issued") else 0),
    ))
    return items[:MAX_ITEMS]


def _fetch_sync():
    """Synchronous worker — both fetches via stdlib urllib. Returns the
    normalized payload (never raises). If WFS is unreachable we still emit an
    empty payload with an `error` describing the failure; if only the list
    endpoint fails we degrade gracefully (items still get geometry + LEGAL,
    just no human description / issued date)."""
    wfs = _get_json(WFS_URL, timeout=HTTP_TIMEOUT)
    if not wfs or not isinstance(wfs, dict):
        return _payload([], error="FAA TFR WFS endpoint unreachable")
    features = wfs.get("features") or []
    if not isinstance(features, list):
        return _payload([], error="FAA TFR WFS returned unexpected shape")

    lst = _get_json(LIST_URL, timeout=HTTP_TIMEOUT) or []
    list_by_id = {}
    if isinstance(lst, list):
        for row in lst:
            if isinstance(row, dict) and row.get("notam_id"):
                list_by_id[row["notam_id"]] = row

    try:
        items = _build_items(features, list_by_id)
    except Exception as e:
        return _payload([], error=f"normalize failed: {type(e).__name__}: {e}")

    err = None
    if not list_by_id:
        err = "getTfrList unreachable (geometry only, no descriptions)"
    return _payload(items, error=err)


async def fetch_notams():
    """Async entrypoint matching the LAYERS contract. Runs the sync urllib
    worker in a thread so it doesn't block the asyncio loop."""
    import asyncio
    try:
        return await asyncio.to_thread(_fetch_sync)
    except Exception as e:
        # Last-resort guard — _fetch_sync already catches its own errors, but
        # the contract says "never raises".
        return _payload([], error=f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    # Local smoke test: python notams.py
    import asyncio as _a
    p = _a.run(fetch_notams())
    print(json.dumps({
        "layer": p["layer"],
        "updatedAt": p["updatedAt"],
        "count": p["count"],
        "error": p.get("error"),
        "sample": p["items"][:5],
    }, indent=2))
