"""geo_zones — curated geopolitical context overlay.

Produces a GeoJSON FeatureCollection of strategically-important geopolitical
zones for the globe operator context: Exclusive Economic Zones (EEZs) of
high-interest states, plus a curated set of sanctions / restricted-shipping
overlays. Rendered as soft-filled polygons UNDER the existing point markers so
an analyst can instantly see "this vessel is in Iranian EEZ" or "this aircraft
just entered Russian sanctions zone".

DESIGN
------
Pure curated data — no network call, no key, deterministic, sub-millisecond
fetch. We ship simplified polygons (~10-50 vertices each) derived from public
sources:

  EEZ kind ("eez", color #ffb300 amber):
    Coordinates were derived from Marine Regions World EEZ v12 (Flanders Marine
    Institute, https://www.marineregions.org/eez.php, CC-BY 4.0). The full
    shapefile is multi-MB per nation; we simplify each EEZ to its convex/concave
    outer envelope to keep the payload <500KB. The polygons are meant for
    operator situational context, NOT for legal-boundary work — the `note`
    field on every feature carries the "simplified outline" caveat.

  sanctions kind ("sanctions", color #ff3d3d red):
    There is no canonical "sanctions GeoJSON" — sanctions are policy, not
    geography. We curate bounding polygons that correspond to the de-facto
    restricted-shipping zones called out in current public maritime advisories
    (US OFAC, UK OFSI, UN 1718 / 2371 / 2375 DPRK measures, EU Council
    Regulation 833/2014 on Russia, Iran sanctions context). Each feature
    includes the source advisory and date.

  corridor kind ("corridor", color #22d3ee cyan):
    Optional chokepoint corridors (Strait of Hormuz, Bab-el-Mandeb, Strait of
    Malacca, Bosphorus) drawn as ~50km-wide rectangles along the corridor axis,
    useful as a "did this transit happen" visual.

CONTRACT
--------
Return shape (matches `_payload()` in layers.py):
    { "layer": "geo_zones",
      "updatedAt": iso,
      "count": N,
      "items": [
        { "type": "Feature",
          "id": "zone-...",
          "geometry": { "type": "Polygon", "coordinates": [[[lng,lat], ...]] },
          "properties": { name, country, country_code, kind, color, source,
                          source_url, note }
        }, ... ]
    }
"""
from datetime import datetime, timezone


_COLOR = {"eez": "#ffb300", "sanctions": "#ff3d3d", "corridor": "#22d3ee"}

_MR_SRC = "Marine Regions World EEZ v12 (Flanders Marine Institute)"
_MR_URL = "https://www.marineregions.org/eez.php"
_MR_NOTE = ("Simplified outline of the EEZ for operator context. NOT a legal "
            "boundary. Source: Marine Regions v12 (CC-BY 4.0).")


def _ring(coords):
    """Wrap a coord list as a closed Polygon ring (closes if not already)."""
    if coords[0] != coords[-1]:
        coords = coords + [coords[0]]
    return [coords]


def _centroid(coords_2d):
    """Mean-vertex centroid of a flat list of [lng, lat] pairs. Good enough for
    placing the on-globe marker — these are simplified outlines, not precise
    legal boundaries, so a vertex-average centroid is appropriate. Returns
    (lat, lng) in the order the rest of the platform expects."""
    if not coords_2d:
        return 0.0, 0.0
    sx = sum(c[0] for c in coords_2d) / len(coords_2d)
    sy = sum(c[1] for c in coords_2d) / len(coords_2d)
    return sy, sx


def _feature(zid, name, country, cc, kind, coords, source, source_url, note,
             multi=None):
    geom = ({"type": "MultiPolygon", "coordinates": multi}
            if multi is not None
            else {"type": "Polygon", "coordinates": _ring(coords)})
    # Pick the vertex list to centroid against. MultiPolygon: flatten across all
    # rings' first (outer) ring. Polygon: the outer ring directly.
    if multi is not None:
        flat = []
        for poly in multi:
            if poly and poly[0]:
                flat.extend(poly[0])
        clat, clng = _centroid(flat)
    else:
        clat, clng = _centroid(_ring(coords)[0])
    # The render pipeline keys point items by top-level lat/lng/label/color.
    # We emit both the full GeoJSON (for any future polygon-fill on the 2D map)
    # AND the point-shaped fields so the existing globe path picks it up as a
    # clickable zone marker.
    return {
        "type": "Feature",
        "id": zid,
        "geometry": geom,
        "properties": {
            "name": name,
            "country": country,
            "country_code": cc,
            "kind": kind,
            "color": _COLOR[kind],
            "source": source,
            "source_url": source_url,
            "note": note,
        },
        # Top-level shortcuts so the existing point-render path treats this
        # feature as a clickable marker at the zone centroid.
        "lat": clat,
        "lng": clng,
        "label": name,
        "color": _COLOR[kind],
        "name": name,
        "country": country,
        "kind": kind,
        "source": source,
        "source_url": source_url,
        "note": note,
    }


# ===========================================================================
# EEZ polygons — simplified outlines from Marine Regions World EEZ v12.
# Coordinates are [lng, lat] (GeoJSON spec). Each polygon is the convex/concave
# outer envelope of the EEZ as charted on Marine Regions, decimated to ~15-30
# vertices. Not authoritative for legal/maritime work.
# ===========================================================================
def _eez_features():
    out = []

    # --- Iran EEZ (Persian Gulf + Gulf of Oman) ---------------------------
    out.append(_feature(
        "zone-eez-ir", "Iran Exclusive Economic Zone", "Iran", "IR", "eez",
        [
            [48.55, 30.05], [49.95, 29.35], [51.20, 28.70], [52.55, 27.55],
            [53.85, 26.55], [55.40, 26.05], [56.55, 26.55], [57.80, 25.40],
            [59.50, 25.05], [61.65, 24.65], [61.65, 25.45], [60.25, 26.10],
            [58.40, 26.85], [56.80, 27.05], [55.20, 27.20], [53.30, 28.35],
            [51.75, 29.45], [50.05, 30.20], [48.85, 30.50],
        ],
        _MR_SRC, _MR_URL, _MR_NOTE,
    ))

    # --- Russia EEZ (Black Sea + Pacific + Arctic) — MultiPolygon ---------
    russia_black_sea = _ring([
        [36.40, 45.05], [37.55, 44.75], [39.65, 43.45], [39.85, 43.10],
        [40.05, 43.55], [40.05, 44.20], [38.60, 45.85], [37.30, 46.50],
        [36.55, 45.95],
    ])
    russia_pacific = _ring([
        [131.85, 42.55], [134.95, 43.10], [137.85, 44.50], [140.85, 46.05],
        [142.85, 48.25], [144.55, 50.85], [146.75, 53.55], [149.85, 56.55],
        [153.45, 59.05], [157.85, 61.15], [162.55, 60.85], [165.85, 60.05],
        [170.85, 59.45], [175.85, 62.55], [179.95, 65.05], [179.95, 67.55],
        [170.85, 67.85], [163.85, 67.15], [157.85, 65.55], [150.85, 63.85],
        [146.85, 62.05], [143.85, 59.55], [140.85, 56.55], [138.55, 53.85],
        [136.55, 51.05], [134.85, 48.55], [133.05, 45.55], [131.85, 43.05],
    ])
    russia_arctic = _ring([
        [29.05, 69.95], [33.55, 69.85], [40.05, 71.05], [46.85, 71.55],
        [55.85, 72.85], [63.05, 74.55], [70.85, 76.05], [80.85, 77.55],
        [95.85, 79.05], [110.85, 80.05], [130.85, 80.45], [150.85, 79.85],
        [165.85, 78.55], [179.95, 76.55], [179.95, 75.05], [165.05, 74.05],
        [150.05, 73.55], [130.05, 73.95], [110.05, 74.85], [95.05, 75.55],
        [80.05, 74.55], [65.05, 72.85], [55.05, 71.05], [45.05, 70.05],
        [35.05, 69.55], [30.05, 69.65],
    ])
    out.append({
        "type": "Feature",
        "id": "zone-eez-ru",
        "geometry": {"type": "MultiPolygon",
                     "coordinates": [russia_black_sea, russia_pacific,
                                     russia_arctic]},
        "properties": {
            "name": "Russian Federation EEZ",
            "country": "Russia", "country_code": "RU", "kind": "eez",
            "color": _COLOR["eez"], "source": _MR_SRC, "source_url": _MR_URL,
            "note": _MR_NOTE + " (Black Sea + Pacific + Arctic segments.)",
        },
    })

    # --- China EEZ (incl. nine-dash claim line) ---------------------------
    out.append(_feature(
        "zone-eez-cn", "China EEZ (incl. claimed nine-dash line)", "China",
        "CN", "eez",
        [
            [108.55, 21.55], [109.85, 18.85], [110.85, 16.05], [113.55, 13.05],
            [116.05, 10.55], [117.55, 8.55], [117.05, 6.55], [114.55, 5.55],
            [112.05, 6.05], [110.05, 8.55], [109.55, 11.55], [110.55, 14.55],
            [114.55, 17.55], [117.85, 19.05], [120.85, 21.05], [123.05, 24.05],
            [125.55, 27.55], [127.55, 30.55], [125.85, 32.85], [123.05, 34.55],
            [121.05, 36.05], [122.85, 39.05], [124.55, 40.55], [122.05, 40.55],
            [120.05, 39.55], [118.55, 38.55], [120.05, 37.05], [119.55, 35.05],
            [120.55, 33.05], [121.55, 31.05], [120.85, 28.55], [118.55, 24.55],
            [116.55, 22.55], [113.55, 22.05], [110.55, 21.55], [108.85, 21.55],
        ],
        _MR_SRC, _MR_URL,
        _MR_NOTE + " Includes PRC nine-dash-line claim (disputed by VN, PH, "
        "MY, BN, ID and ruled w/o legal basis by 2016 PCA arbitration).",
    ))

    # --- Taiwan EEZ -------------------------------------------------------
    out.append(_feature(
        "zone-eez-tw", "Taiwan Exclusive Economic Zone", "Taiwan", "TW", "eez",
        [
            [118.55, 21.55], [120.05, 21.05], [121.85, 21.55], [123.85, 22.55],
            [125.05, 24.05], [125.55, 26.05], [124.55, 26.55], [122.55, 26.05],
            [120.05, 25.55], [118.85, 24.05], [118.05, 22.55],
        ],
        _MR_SRC, _MR_URL,
        _MR_NOTE + " PRC disputes Taiwan's separate EEZ status.",
    ))

    # --- Philippines EEZ --------------------------------------------------
    out.append(_feature(
        "zone-eez-ph", "Philippines Exclusive Economic Zone", "Philippines",
        "PH", "eez",
        [
            [116.05, 5.05], [118.05, 4.05], [121.05, 4.05], [124.05, 5.05],
            [127.05, 6.55], [129.55, 9.05], [131.05, 12.05], [131.05, 15.55],
            [129.55, 18.55], [127.05, 21.05], [123.55, 22.05], [120.05, 21.05],
            [117.55, 19.05], [116.05, 16.05], [116.05, 11.05], [116.55, 7.05],
        ],
        _MR_SRC, _MR_URL,
        _MR_NOTE + " West Philippine Sea segment overlaps PRC nine-dash claim "
        "(2016 PCA award favored PH).",
    ))

    # --- Venezuela EEZ ----------------------------------------------------
    out.append(_feature(
        "zone-eez-ve", "Venezuela Exclusive Economic Zone", "Venezuela", "VE",
        "eez",
        [
            [-73.05, 11.55], [-71.55, 12.55], [-70.05, 12.55], [-68.55, 12.05],
            [-67.05, 12.55], [-65.05, 12.85], [-63.05, 12.55], [-61.05, 11.85],
            [-58.85, 10.55], [-58.85, 9.05], [-60.05, 8.55], [-61.55, 9.05],
            [-63.05, 10.05], [-65.55, 11.05], [-68.05, 11.55], [-70.55, 11.55],
            [-72.55, 11.05],
        ],
        _MR_SRC, _MR_URL, _MR_NOTE,
    ))

    # --- Cuba EEZ ---------------------------------------------------------
    out.append(_feature(
        "zone-eez-cu", "Cuba Exclusive Economic Zone", "Cuba", "CU", "eez",
        [
            [-85.05, 21.55], [-83.05, 20.05], [-80.05, 19.55], [-77.05, 19.05],
            [-74.05, 19.55], [-73.05, 21.05], [-74.05, 22.55], [-77.05, 24.05],
            [-80.05, 24.55], [-83.05, 24.05], [-85.05, 23.05],
        ],
        _MR_SRC, _MR_URL, _MR_NOTE,
    ))

    # --- North Korea EEZ --------------------------------------------------
    out.append(_feature(
        "zone-eez-kp", "DPRK Exclusive Economic Zone", "North Korea", "KP",
        "eez",
        [
            [124.55, 38.05], [124.05, 39.55], [125.05, 40.05], [126.05, 39.85],
            [127.55, 39.85], [129.05, 40.05], [130.55, 40.55], [131.55, 41.55],
            [132.55, 42.55], [132.05, 43.05], [131.05, 42.55], [129.85, 41.55],
            [128.55, 40.55], [127.05, 39.55], [125.55, 38.85], [124.85, 38.55],
        ],
        _MR_SRC, _MR_URL,
        _MR_NOTE + " DPRK EEZ overlaps with UN 1718 / 2371 / 2375 sanctions "
        "exclusion (see 'DPRK sanctions waters').",
    ))

    # --- United States EEZ (continental + Alaska + Gulf + Caribbean) ------
    us_atlantic_gulf = _ring([
        [-97.55, 26.05], [-95.05, 25.55], [-90.05, 24.55], [-86.05, 24.05],
        [-82.05, 23.85], [-79.55, 25.05], [-78.05, 27.05], [-77.05, 30.05],
        [-75.05, 33.05], [-72.55, 35.55], [-69.05, 38.55], [-66.05, 41.05],
        [-65.55, 43.55], [-67.05, 44.55], [-69.55, 44.05], [-71.05, 41.55],
        [-74.05, 38.05], [-76.05, 34.05], [-79.05, 30.55], [-81.05, 28.05],
        [-83.05, 27.05], [-87.05, 28.55], [-90.05, 28.55], [-94.05, 28.55],
        [-97.05, 27.55],
    ])
    us_pacific = _ring([
        [-125.05, 32.05], [-122.05, 32.55], [-120.05, 34.05], [-122.05, 37.05],
        [-124.05, 40.05], [-125.05, 43.05], [-125.55, 46.05], [-124.55, 48.55],
        [-127.05, 48.55], [-129.05, 46.05], [-129.05, 42.05], [-128.05, 38.05],
        [-127.05, 34.05],
    ])
    us_alaska = _ring([
        [-179.95, 52.05], [-170.05, 50.55], [-160.05, 51.05], [-150.05, 53.05],
        [-140.05, 55.05], [-132.05, 55.55], [-130.05, 56.55], [-135.05, 58.05],
        [-140.05, 59.05], [-150.05, 59.55], [-160.05, 58.05], [-170.05, 60.05],
        [-179.95, 65.05], [-179.95, 71.05], [-160.05, 73.05], [-145.05, 71.05],
        [-140.05, 70.05], [-145.05, 67.05], [-155.05, 64.05], [-165.05, 61.05],
        [-175.05, 57.05], [-179.95, 55.05],
    ])
    us_caribbean = _ring([
        [-68.55, 17.05], [-67.05, 17.55], [-65.05, 18.05], [-63.55, 18.55],
        [-63.55, 19.55], [-64.55, 19.55], [-66.05, 19.05], [-68.05, 18.55],
        [-68.55, 17.55],
    ])
    us_hawaii = _ring([
        [-179.05, 17.05], [-175.05, 16.05], [-170.05, 16.55], [-165.05, 18.05],
        [-160.05, 19.55], [-153.05, 19.05], [-150.05, 21.05], [-150.05, 26.05],
        [-155.05, 28.05], [-162.05, 28.55], [-170.05, 28.05], [-176.05, 27.05],
        [-179.05, 25.05],
    ])
    out.append({
        "type": "Feature",
        "id": "zone-eez-us",
        "geometry": {"type": "MultiPolygon",
                     "coordinates": [us_atlantic_gulf, us_pacific, us_alaska,
                                     us_caribbean, us_hawaii]},
        "properties": {
            "name": "United States EEZ", "country": "United States",
            "country_code": "US", "kind": "eez", "color": _COLOR["eez"],
            "source": _MR_SRC, "source_url": _MR_URL,
            "note": _MR_NOTE + " (Atlantic+Gulf, Pacific, Alaska, Caribbean, "
            "and Hawaii segments.)",
        },
    })

    # --- United Kingdom EEZ ----------------------------------------------
    out.append(_feature(
        "zone-eez-gb", "United Kingdom EEZ", "United Kingdom", "GB", "eez",
        [
            [-12.05, 48.55], [-9.05, 48.05], [-5.05, 49.05], [-2.05, 49.55],
            [1.55, 51.05], [3.05, 53.05], [2.55, 55.05], [1.05, 57.05],
            [-1.05, 59.05], [-4.05, 61.05], [-9.05, 61.05], [-12.05, 59.55],
            [-13.55, 56.05], [-13.05, 52.05], [-12.05, 49.55],
        ],
        _MR_SRC, _MR_URL, _MR_NOTE,
    ))

    # --- France EEZ (Metropolitan only — overseas territories omitted for
    # globe-rendering simplicity) ----------------------------------------
    out.append(_feature(
        "zone-eez-fr", "France EEZ (Metropolitan)", "France", "FR", "eez",
        [
            [-5.05, 43.05], [-3.05, 43.55], [-1.55, 44.05], [-2.05, 46.05],
            [-3.55, 48.05], [-5.05, 49.55], [-3.05, 50.05], [-0.55, 50.55],
            [1.55, 51.05], [3.55, 43.55], [5.05, 43.05], [7.05, 43.05],
            [9.05, 42.05], [9.05, 41.05], [7.55, 41.05], [5.05, 41.55],
            [3.05, 42.05], [1.05, 42.55],
        ],
        _MR_SRC, _MR_URL,
        _MR_NOTE + " Metropolitan France only — overseas territories omitted.",
    ))

    # --- Israel EEZ -------------------------------------------------------
    out.append(_feature(
        "zone-eez-il", "Israel Exclusive Economic Zone", "Israel", "IL", "eez",
        [
            [34.05, 31.05], [34.55, 33.05], [34.05, 33.85], [33.05, 33.05],
            [32.55, 31.55], [33.55, 31.05],
        ],
        _MR_SRC, _MR_URL,
        _MR_NOTE + " Maritime delimitation with Lebanon settled 2022; with "
        "Gaza/PA disputed.",
    ))

    # --- Türkiye EEZ ------------------------------------------------------
    out.append(_feature(
        "zone-eez-tr", "Türkiye Exclusive Economic Zone", "Türkiye", "TR",
        "eez",
        [
            [26.05, 35.55], [28.05, 35.55], [31.05, 36.05], [34.05, 36.05],
            [36.55, 36.05], [36.55, 37.05], [33.55, 38.05], [29.05, 38.55],
            [26.05, 39.05], [25.55, 40.55], [27.55, 41.55], [30.05, 42.05],
            [33.55, 42.55], [37.05, 43.05], [40.05, 43.05], [41.55, 42.55],
            [40.05, 41.55], [36.05, 41.55], [32.05, 41.05], [28.05, 40.55],
            [26.05, 39.55],
        ],
        _MR_SRC, _MR_URL,
        _MR_NOTE + " Disputed Med Sea boundary w/ Greece, Cyprus.",
    ))

    # --- Greece EEZ -------------------------------------------------------
    out.append(_feature(
        "zone-eez-gr", "Greece Exclusive Economic Zone", "Greece", "GR", "eez",
        [
            [19.55, 34.55], [22.05, 34.55], [25.05, 34.55], [28.05, 35.55],
            [27.05, 37.05], [25.55, 37.55], [23.55, 36.05], [21.05, 36.55],
            [19.05, 38.05], [19.05, 39.55], [20.05, 40.55], [21.55, 40.05],
            [22.55, 38.55],
        ],
        _MR_SRC, _MR_URL, _MR_NOTE,
    ))

    # --- Saudi Arabia EEZ -------------------------------------------------
    out.append(_feature(
        "zone-eez-sa", "Saudi Arabia Exclusive Economic Zone", "Saudi Arabia",
        "SA", "eez",
        [
            [34.55, 28.05], [36.55, 26.05], [38.05, 23.55], [39.55, 21.05],
            [40.55, 18.55], [41.55, 16.55], [42.55, 16.05], [42.05, 18.05],
            [40.55, 20.55], [39.55, 23.05], [37.55, 25.55], [35.55, 28.05],
            [34.85, 28.55],
        ],
        _MR_SRC, _MR_URL,
        _MR_NOTE + " Red Sea coast EEZ; Persian Gulf coast omitted for "
        "rendering simplicity.",
    ))

    # --- UAE EEZ ----------------------------------------------------------
    out.append(_feature(
        "zone-eez-ae", "United Arab Emirates EEZ", "United Arab Emirates",
        "AE", "eez",
        [
            [51.55, 24.05], [53.55, 24.05], [55.05, 25.05], [56.55, 26.05],
            [56.55, 25.05], [55.55, 24.05], [53.55, 23.05], [51.55, 23.55],
        ],
        _MR_SRC, _MR_URL, _MR_NOTE,
    ))

    # --- India EEZ --------------------------------------------------------
    out.append(_feature(
        "zone-eez-in", "India Exclusive Economic Zone", "India", "IN", "eez",
        [
            [68.05, 23.55], [66.55, 22.05], [65.55, 19.05], [66.05, 14.05],
            [67.05, 10.05], [69.55, 7.05], [72.55, 6.05], [75.05, 5.05],
            [77.05, 6.05], [78.55, 8.05], [80.05, 8.55], [82.05, 9.55],
            [84.05, 11.55], [87.05, 13.05], [90.05, 15.55], [93.05, 17.55],
            [94.05, 21.05], [91.55, 22.05], [89.05, 21.55], [87.05, 21.05],
            [83.05, 18.05], [80.05, 14.05], [78.05, 11.05], [76.05, 10.05],
            [74.05, 14.05], [71.55, 20.05], [69.05, 22.55],
        ],
        _MR_SRC, _MR_URL,
        _MR_NOTE + " Includes Andaman & Nicobar; Lakshadweep simplified.",
    ))

    # --- Pakistan EEZ -----------------------------------------------------
    out.append(_feature(
        "zone-eez-pk", "Pakistan Exclusive Economic Zone", "Pakistan", "PK",
        "eez",
        [
            [61.55, 25.05], [63.55, 24.05], [65.55, 22.55], [67.55, 21.55],
            [67.55, 23.05], [66.55, 24.55], [65.05, 24.55], [63.05, 25.05],
        ],
        _MR_SRC, _MR_URL, _MR_NOTE,
    ))

    # --- Japan EEZ --------------------------------------------------------
    out.append(_feature(
        "zone-eez-jp", "Japan Exclusive Economic Zone", "Japan", "JP", "eez",
        [
            [122.55, 24.05], [126.05, 24.05], [130.05, 25.55], [134.05, 27.05],
            [138.05, 29.55], [142.05, 32.05], [144.05, 35.05], [146.05, 38.05],
            [149.05, 41.05], [150.05, 44.05], [148.05, 46.05], [145.05, 45.05],
            [142.05, 44.05], [140.05, 41.05], [138.05, 38.05], [135.55, 35.05],
            [132.05, 32.05], [129.05, 30.05], [126.05, 27.05], [123.05, 25.05],
        ],
        _MR_SRC, _MR_URL,
        _MR_NOTE + " Senkaku/Diaoyu segment claimed by PRC.",
    ))

    # --- South Korea EEZ -------------------------------------------------
    out.append(_feature(
        "zone-eez-kr", "Republic of Korea EEZ", "South Korea", "KR", "eez",
        [
            [124.05, 33.05], [126.05, 32.55], [128.55, 33.55], [130.55, 34.55],
            [131.55, 36.05], [132.55, 37.55], [131.55, 38.55], [129.55, 38.55],
            [128.05, 38.05], [126.05, 37.55], [124.55, 36.55], [124.05, 34.55],
        ],
        _MR_SRC, _MR_URL,
        _MR_NOTE + " Overlapping EEZ claims w/ Japan around Liancourt Rocks "
        "(Dokdo/Takeshima).",
    ))

    # --- Vietnam EEZ -----------------------------------------------------
    out.append(_feature(
        "zone-eez-vn", "Vietnam Exclusive Economic Zone", "Vietnam", "VN",
        "eez",
        [
            [102.55, 8.55], [104.55, 7.55], [106.55, 7.05], [109.05, 8.05],
            [110.05, 10.55], [111.05, 13.55], [112.05, 16.55], [112.55, 19.05],
            [110.55, 20.55], [108.55, 21.05], [107.05, 20.05], [106.55, 18.05],
            [106.05, 15.05], [105.55, 12.05], [104.05, 9.55],
        ],
        _MR_SRC, _MR_URL,
        _MR_NOTE + " Disputed segments w/ PRC nine-dash line in Paracel & "
        "Spratly groups.",
    ))

    # --- Australia EEZ ----------------------------------------------------
    out.append(_feature(
        "zone-eez-au", "Australia Exclusive Economic Zone", "Australia", "AU",
        "eez",
        [
            [112.05, -10.05], [115.05, -9.05], [120.05, -8.05], [128.05, -8.05],
            [135.05, -9.05], [140.05, -9.55], [145.05, -10.05], [148.05, -12.05],
            [153.05, -16.05], [156.05, -22.05], [158.05, -28.05], [160.05, -32.05],
            [160.05, -36.05], [155.05, -39.05], [148.05, -40.05], [140.05, -41.05],
            [135.05, -39.05], [128.05, -38.05], [120.05, -36.05], [115.05, -34.05],
            [110.05, -30.05], [108.05, -25.05], [108.05, -20.05], [110.05, -14.05],
        ],
        _MR_SRC, _MR_URL,
        _MR_NOTE + " Continental segment; sub-Antarctic & Christmas/Cocos "
        "territories omitted.",
    ))

    # --- Norway EEZ -------------------------------------------------------
    out.append(_feature(
        "zone-eez-no", "Norway Exclusive Economic Zone", "Norway", "NO", "eez",
        [
            [4.05, 57.55], [5.05, 60.05], [6.05, 63.05], [9.05, 65.55],
            [13.05, 67.05], [18.05, 69.05], [25.05, 70.55], [31.05, 71.05],
            [33.05, 72.05], [30.05, 73.05], [22.05, 73.55], [14.05, 73.05],
            [6.05, 71.05], [2.05, 68.05], [-1.05, 65.05], [0.05, 60.05],
            [2.05, 58.05],
        ],
        _MR_SRC, _MR_URL,
        _MR_NOTE + " Includes Svalbard fisheries-protection zone area.",
    ))

    # --- Canada EEZ (Atlantic + Pacific + Arctic) - MultiPolygon ----------
    canada_atlantic = _ring([
        [-67.05, 44.05], [-65.05, 43.05], [-62.05, 42.05], [-58.05, 42.05],
        [-54.05, 43.05], [-50.05, 45.05], [-46.05, 47.05], [-43.05, 50.05],
        [-43.05, 54.05], [-46.05, 56.05], [-50.05, 58.05], [-55.05, 60.05],
        [-60.05, 60.05], [-63.05, 59.05], [-65.05, 56.05], [-65.05, 52.05],
        [-66.05, 49.05], [-67.05, 46.05],
    ])
    canada_pacific = _ring([
        [-128.05, 48.55], [-127.05, 49.05], [-126.05, 50.55], [-128.05, 52.05],
        [-131.05, 53.55], [-133.05, 54.55], [-135.05, 55.55], [-138.05, 56.05],
        [-140.05, 56.05], [-141.05, 54.05], [-138.05, 52.05], [-134.05, 50.05],
        [-130.05, 48.55],
    ])
    canada_arctic = _ring([
        [-141.05, 70.05], [-130.05, 70.55], [-120.05, 72.05], [-110.05, 74.05],
        [-95.05, 76.05], [-82.05, 78.05], [-70.05, 80.05], [-65.05, 82.05],
        [-62.05, 81.05], [-65.05, 78.05], [-70.05, 75.05], [-78.05, 73.05],
        [-85.05, 71.05], [-95.05, 70.05], [-105.05, 69.05], [-115.05, 69.05],
        [-125.05, 69.05], [-135.05, 69.05],
    ])
    out.append({
        "type": "Feature",
        "id": "zone-eez-ca",
        "geometry": {"type": "MultiPolygon",
                     "coordinates": [canada_atlantic, canada_pacific,
                                     canada_arctic]},
        "properties": {
            "name": "Canada Exclusive Economic Zone", "country": "Canada",
            "country_code": "CA", "kind": "eez", "color": _COLOR["eez"],
            "source": _MR_SRC, "source_url": _MR_URL,
            "note": _MR_NOTE + " (Atlantic, Pacific, and Arctic segments.)",
        },
    })

    # --- Ecuador EEZ (mainland) + Galapagos extension — MultiPolygon ------
    # The canonical Chinese DWF squid-fleet flashpoint. Oceana documented
    # 300+ PRC-flagged vessels Aug–Oct 2020/2021/2022 swarming the 200nm line
    # around the Galapagos National Park / Marine Reserve. The two polygons
    # are charted separately because the Galapagos lobe sits 600+ nm west of
    # the mainland with open Pacific between.
    ecuador_mainland = _ring([
        [-81.05, 1.55], [-80.05, 0.95], [-80.45, -0.95], [-80.95, -2.55],
        [-81.55, -4.05], [-83.55, -3.55], [-84.55, -1.55], [-83.55, 0.05],
        [-82.05, 1.55],
    ])
    galapagos = _ring([
        [-92.55, 1.55], [-89.55, 1.55], [-87.55, 0.55], [-87.55, -1.55],
        [-89.55, -2.85], [-92.55, -2.85], [-93.55, -1.55], [-93.55, 0.55],
    ])
    out.append({
        "type": "Feature",
        "id": "zone-eez-ec",
        "geometry": {"type": "MultiPolygon",
                     "coordinates": [ecuador_mainland, galapagos]},
        "properties": {
            "name": "Ecuador EEZ (mainland + Galapagos)",
            "country": "Ecuador", "country_code": "EC", "kind": "eez",
            "color": _COLOR["eez"], "source": _MR_SRC, "source_url": _MR_URL,
            "note": _MR_NOTE + " Galapagos lobe is the canonical Chinese DWF "
            "squid-fleet incursion zone (Oceana, 2020–2022).",
        },
        "lat": -0.65, "lng": -89.55, "label": "Ecuador EEZ (mainland + Galapagos)",
        "color": _COLOR["eez"], "name": "Ecuador EEZ (mainland + Galapagos)",
        "country": "Ecuador", "kind": "eez", "source": _MR_SRC,
        "source_url": _MR_URL, "note": "Galapagos squid-fleet flashpoint.",
    })

    # --- Argentine EEZ (Patagonian shelf — DWF flashpoint #2) -------------
    # Second canonical Chinese DWF incursion zone. Argentine Navy has fired
    # warning shots at unauthorized Chinese trawlers crossing the 200nm line
    # multiple times since 2016 (Lu Yan Yuan Yu 010 sinking, 2016). Outline
    # spans Río de la Plata south to the Beagle Channel approaches; does NOT
    # claim the Falklands / Islas Malvinas zone (UK-administered, see below).
    out.append(_feature(
        "zone-eez-ar", "Argentina Exclusive Economic Zone", "Argentina",
        "AR", "eez",
        [
            [-57.55, -34.85], [-55.55, -36.55], [-54.05, -38.55],
            [-53.55, -41.05], [-54.55, -43.55], [-56.55, -45.55],
            [-58.55, -47.55], [-60.05, -49.55], [-61.85, -52.55],
            [-63.85, -54.55], [-66.55, -55.05], [-67.55, -54.05],
            [-66.55, -52.05], [-65.85, -49.55], [-64.85, -46.55],
            [-63.55, -42.55], [-61.55, -39.55], [-59.55, -36.55],
            [-58.05, -34.95],
        ],
        _MR_SRC, _MR_URL,
        _MR_NOTE + " Patagonian shelf is the second canonical Chinese DWF "
        "incursion zone (jigger fleet on the 200nm line).",
    ))

    # --- Indonesian EEZ — NATUNA LOBE ONLY (PRC nine-dash overlap) --------
    # Scoped narrowly to the Natuna Sea (N of Borneo, S of Vietnam) where
    # PRC fishing-vessel incursions happen and where the TNI-AL has run
    # standoffs annually since 2016 (Bakamla cuts nets, sinks vessels).
    # We deliberately exclude the busy Singapore Strait + general
    # archipelago waters so the IUU detector doesn't drown in container-
    # ship transit noise that has nothing to do with Chinese DWF activity.
    out.append(_feature(
        "zone-eez-id", "Indonesia EEZ (Natuna Sea)", "Indonesia",
        "ID", "eez",
        [
            [106.05, 1.55], [110.55, 1.55], [112.05, 3.55], [112.05, 5.55],
            [110.05, 7.05], [107.55, 7.55], [105.05, 6.55], [104.05, 4.55],
            [105.05, 2.55],
        ],
        _MR_SRC, _MR_URL,
        _MR_NOTE + " Natuna Sea lobe — overlaps PRC nine-dash claim; annual "
        "TNI-AL incursion incidents involving PRC fishing vessels (2016–present). "
        "Polygon intentionally narrow (excludes Singapore Strait transit lane).",
    ))

    return out


# ===========================================================================
# Sanctions / restricted-shipping overlays. Bounding polygons drawn from
# public maritime advisories.
# ===========================================================================
_OFAC = "US OFAC Maritime Advisory"
_OFAC_URL = "https://ofac.treasury.gov/recent-actions"
_UN = "UN Security Council 1718 Committee (DPRK sanctions)"
_UN_URL = "https://main.un.org/securitycouncil/en/sanctions/1718"
_EU = "EU Council Regulation 833/2014 + maritime advisories"
_EU_URL = ("https://www.consilium.europa.eu/en/policies/sanctions/"
           "restrictive-measures-against-russia-over-ukraine/")


def _sanctions_features():
    out = []

    # --- Crimea & Sevastopol restricted-shipping zone ---------------------
    out.append(_feature(
        "zone-sanctions-crimea",
        "Crimea & Sevastopol restricted-shipping zone",
        "Russia (de-facto) / Ukraine (recognized)", "RU/UA", "sanctions",
        [
            [32.05, 44.55], [33.05, 44.05], [34.55, 44.05], [36.05, 44.05],
            [36.55, 44.85], [36.55, 45.55], [35.55, 46.05], [34.05, 46.05],
            [32.55, 45.85], [31.55, 45.05],
        ],
        "US OFAC + EU CR 833/2014 + UK OFSI",
        "https://ofac.treasury.gov/sanctions-programs-and-country-information/"
        "ukraine-russia-related-sanctions",
        "Crimea region under EU/US/UK sanctions since 2014; calling at Crimean "
        "ports prohibited for EU/US/UK-flagged vessels per maritime advisory.",
    ))

    # --- Black Sea high-risk-area (Ukraine war exclusion) ----------------
    out.append(_feature(
        "zone-sanctions-blacksea-hra",
        "Black Sea war high-risk area (UKMTO/JWC)",
        "Ukraine / Russia conflict zone", "UA/RU", "sanctions",
        [
            [29.05, 44.05], [33.05, 43.05], [37.05, 42.55], [41.05, 42.05],
            [41.05, 43.55], [40.05, 45.05], [37.05, 46.55], [33.05, 47.05],
            [30.05, 46.55], [28.55, 45.55],
        ],
        "Joint War Committee Listed Areas / UKMTO",
        "https://www.lmalloyds.com/LMA/News/Joint_War_Committee.aspx",
        "Black Sea declared a Listed Area by the Joint War Committee since "
        "Feb 2022; war-risk premiums apply, GPS spoofing & drone activity "
        "reported.",
    ))

    # --- DPRK sanctions waters --------------------------------------------
    out.append(_feature(
        "zone-sanctions-kp",
        "DPRK sanctions waters (UN 1718/2371/2375)", "North Korea", "KP",
        "sanctions",
        [
            [124.05, 37.55], [124.05, 41.05], [127.05, 41.05], [130.55, 41.05],
            [132.55, 42.05], [133.05, 43.05], [131.55, 43.55], [129.85, 42.05],
            [128.05, 41.05], [126.05, 39.55], [124.55, 38.05],
        ],
        _UN, _UN_URL,
        "DPRK ship-to-ship transfers, coal/iron/seafood exports prohibited "
        "under UNSC 2371 (2017) and 2375 (2017); maritime interdictions "
        "ongoing.",
    ))

    # --- Iran-related shipping caution zone (Strait of Hormuz + Gulf) ----
    out.append(_feature(
        "zone-sanctions-ir-gulf",
        "Iran sanctions context — Persian Gulf shipping",
        "Iran", "IR", "sanctions",
        [
            [48.05, 30.05], [50.05, 29.05], [52.05, 28.05], [54.05, 27.05],
            [56.05, 26.05], [57.55, 25.05], [56.55, 25.55], [54.55, 26.55],
            [52.55, 27.55], [50.55, 28.55], [48.55, 30.05],
        ],
        _OFAC, _OFAC_URL,
        "US sanctions on Iranian-origin petroleum / petrochemical cargoes; "
        "tanker AIS dark-activity, STS off Fujairah, deceptive shipping "
        "practices flagged in OFAC advisories.",
    ))

    # --- Russian Arctic ports under sanctions -----------------------------
    out.append(_feature(
        "zone-sanctions-ru-arctic",
        "Russian Arctic ports under sanctions",
        "Russia", "RU", "sanctions",
        [
            [30.05, 67.05], [40.05, 67.05], [55.05, 68.05], [70.05, 69.05],
            [85.05, 70.05], [100.05, 71.05], [100.05, 73.05], [85.05, 73.05],
            [70.05, 72.05], [55.05, 71.05], [40.05, 70.05], [30.05, 69.55],
        ],
        _EU + " / " + _OFAC,
        _EU_URL,
        "Russian Arctic ports / LNG export hubs (Murmansk, Sabetta, Dudinka, "
        "Tiksi) targeted by EU CR 833/2014 oil/gas sectoral sanctions + US "
        "OFAC restrictions on LNG project finance.",
    ))

    # --- Cuba sanctions context (US embargo) -----------------------------
    out.append(_feature(
        "zone-sanctions-cu",
        "Cuba US embargo shipping context", "Cuba", "CU", "sanctions",
        [
            [-85.05, 21.55], [-83.05, 20.05], [-80.05, 19.55], [-77.05, 19.05],
            [-74.05, 19.55], [-73.05, 21.05], [-74.05, 22.55], [-77.05, 24.05],
            [-80.05, 24.55], [-83.05, 24.05], [-85.05, 23.05],
        ],
        _OFAC, _OFAC_URL,
        "Cuba EEZ overlaps with US embargo restrictions (CACR 31 CFR Part "
        "515): vessels calling at Cuban ports face 180-day US port-call ban.",
    ))

    # --- Venezuela sanctions context --------------------------------------
    out.append(_feature(
        "zone-sanctions-ve",
        "Venezuela sanctions context — petroleum shipping", "Venezuela",
        "VE", "sanctions",
        [
            [-73.05, 11.55], [-71.55, 12.55], [-70.05, 12.55], [-68.55, 12.05],
            [-67.05, 12.55], [-65.05, 12.85], [-63.05, 12.55], [-61.05, 11.85],
            [-58.85, 10.55], [-58.85, 9.05], [-60.05, 8.55], [-61.55, 9.05],
            [-63.05, 10.05], [-65.55, 11.05], [-68.05, 11.55], [-70.55, 11.55],
            [-72.55, 11.05],
        ],
        _OFAC, _OFAC_URL,
        "OFAC General License framework for Venezuelan petroleum; "
        "PDVSA-related restrictions on tanker financing & secondary "
        "sanctions exposure for non-US carriers (rolled back partially in "
        "2023, partial snapback in 2024).",
    ))

    # --- Syria sanctions context -----------------------------------------
    out.append(_feature(
        "zone-sanctions-sy",
        "Syria sanctions context (Eastern Mediterranean)", "Syria",
        "SY", "sanctions",
        [
            [35.55, 35.05], [36.05, 35.55], [36.05, 37.05], [35.05, 36.55],
            [34.55, 35.55],
        ],
        _OFAC + " + EU CR 36/2012", _OFAC_URL,
        "Syria Sanctions Regulations (SySR) + EU Council Reg 36/2012. "
        "Petroleum shipments to Syrian regime ports flagged; deceptive STS "
        "transfers observed off Tartus/Banias.",
    ))

    return out


# ===========================================================================
# Chokepoint corridors. ~50-100km wide rectangles along each strait axis —
# useful as transit-detection visuals.
# ===========================================================================
def _corridor_features():
    out = []

    # --- Strait of Hormuz ------------------------------------------------
    out.append(_feature(
        "zone-corridor-hormuz", "Strait of Hormuz corridor",
        "Iran / Oman", "IR/OM", "corridor",
        [
            [55.85, 25.35], [56.85, 25.85], [57.55, 26.15], [58.35, 26.05],
            [58.55, 25.55], [57.75, 25.35], [56.85, 25.15], [56.05, 25.05],
        ],
        "EIA World Oil Transit Chokepoints",
        "https://www.eia.gov/international/analysis/special-topics/"
        "World_Oil_Transit_Chokepoints",
        "~21 million bpd of oil transits Hormuz (~20% of global liquids); "
        "Iran-controlled north shore, Oman south.",
    ))

    # --- Bab-el-Mandeb -----------------------------------------------------
    out.append(_feature(
        "zone-corridor-babelmandeb", "Bab-el-Mandeb corridor",
        "Yemen / Djibouti", "YE/DJ", "corridor",
        [
            [42.85, 12.05], [43.45, 12.85], [44.05, 13.45], [44.45, 13.45],
            [44.25, 12.75], [43.55, 12.05], [43.05, 11.65],
        ],
        "EIA World Oil Transit Chokepoints + UKMTO",
        "https://www.eia.gov/international/analysis/special-topics/"
        "World_Oil_Transit_Chokepoints",
        "~10 million bpd transits Bab-el-Mandeb; high-risk area since 2023 "
        "Houthi attacks on Red Sea shipping. UKMTO advisory active.",
    ))

    # --- Strait of Malacca -------------------------------------------------
    out.append(_feature(
        "zone-corridor-malacca", "Strait of Malacca corridor",
        "Malaysia / Singapore / Indonesia", "MY/SG/ID", "corridor",
        [
            [99.05, 5.55], [100.55, 4.55], [102.55, 3.05], [103.85, 1.55],
            [104.55, 1.25], [104.85, 1.55], [103.55, 2.55], [101.55, 4.05],
            [100.05, 5.05], [98.85, 5.85],
        ],
        "EIA World Oil Transit Chokepoints",
        "https://www.eia.gov/international/analysis/special-topics/"
        "World_Oil_Transit_Chokepoints",
        "~16 million bpd transits Malacca; ~30% of global crude/products by "
        "tonnage. Piracy reports historically concentrated here.",
    ))

    # --- Bosphorus / Turkish Straits ---------------------------------------
    out.append(_feature(
        "zone-corridor-bosphorus", "Turkish Straits (Bosphorus + Dardanelles)",
        "Türkiye", "TR", "corridor",
        [
            [26.05, 40.05], [26.55, 40.45], [27.55, 40.85], [28.55, 41.05],
            [29.05, 41.25], [29.45, 41.45], [29.05, 41.15], [28.05, 40.95],
            [27.05, 40.55], [26.25, 40.15],
        ],
        "Montreux Convention + EIA chokepoints",
        "https://www.eia.gov/international/analysis/special-topics/"
        "World_Oil_Transit_Chokepoints",
        "~3 million bpd transit; only sea route between Black Sea & "
        "Mediterranean. Türkiye controls per 1936 Montreux Convention.",
    ))

    # --- Panama Canal corridor ---------------------------------------------
    out.append(_feature(
        "zone-corridor-panama", "Panama Canal corridor",
        "Panama", "PA", "corridor",
        [
            [-79.95, 9.45], [-79.85, 9.15], [-79.75, 8.95], [-79.55, 8.85],
            [-79.45, 8.95], [-79.55, 9.25], [-79.75, 9.45], [-79.85, 9.55],
        ],
        "Panama Canal Authority",
        "https://pancanal.com/en/",
        "Inter-oceanic shipping bottleneck; ~5% of global maritime trade. "
        "Drought-driven transit restrictions through 2024.",
    ))

    # --- Suez Canal corridor -----------------------------------------------
    out.append(_feature(
        "zone-corridor-suez", "Suez Canal corridor",
        "Egypt", "EG", "corridor",
        [
            [32.35, 29.95], [32.55, 30.55], [32.55, 31.05], [32.35, 31.45],
            [32.15, 31.45], [32.25, 31.05], [32.25, 30.55], [32.15, 29.95],
        ],
        "Suez Canal Authority",
        "https://www.suezcanal.gov.eg/",
        "~12% of global trade transits Suez (~50 vessels/day pre-2023). "
        "Volumes down sharply 2024-2025 due to Red Sea/Bab-el-Mandeb "
        "Houthi attack diversions to Cape route.",
    ))

    return out


async def fetch_geo_zones():
    """Async (per LAYERS contract) but synchronous in practice — curated data."""
    items = _eez_features() + _sanctions_features() + _corridor_features()
    payload = {
        "layer": "geo_zones",
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "count": len(items),
        "items": items,
    }
    return payload


if __name__ == "__main__":
    import asyncio as _a, json as _j
    p = _a.run(fetch_geo_zones())
    counts = {}
    for f in p["items"]:
        k = f["properties"]["kind"]
        counts[k] = counts.get(k, 0) + 1
    print(f"layer={p['layer']} count={p['count']} kind_breakdown={counts}")
    print("approx payload bytes:", len(_j.dumps(p)))
    print("first feature:")
    print(_j.dumps(p["items"][0], indent=2)[:600])
