"""Cross-source corroboration scoring — multiple kinetic signals converging.

The base forecast scores each signal family (military_air, naval, nav_warnings,
frontlines, events, news) independently and weighted-averages them. That captures
overall pressure on a region but cannot distinguish "moderate signal on ONE
axis" (news only — could be a beat-up news cycle) from "moderate signal on FOUR
axes simultaneously" (real escalation: jets up, ships moving, NAVAREA closures
out, ground combat). The latter is dramatically more meaningful and deserves a
compound bonus.

This module is a PURE function over an already-built forecast + the same `layers`
dict the forecast was built from. No network. Two products:

  hotspots[]      Per-hotspot corroboration: how many of the four KINETIC signal
                  families are lit (sub-score >= LIT_THRESHOLD), a corroboration
                  ratio (0-1), a compound bonus added to the base score, and the
                  resulting compound_score / compound_level. Single-lit-signal
                  hotspots get no bonus — multi-signal convergence is the point.

  convergences[]  Globe-wide grid scan that catches EMERGING incidents the 12
                  predefined hotspots do not cover yet: a 5-degree cell with >=2
                  distinct kinetic signal families present that does NOT overlap
                  an existing hotspot bbox. Capped at 8, ranked by lit-family
                  count then total item count.

Reused from forecast.py: _items (safe item-list extraction), _bbox_contains
(bbox membership test), HOTSPOTS (existing hotspot boxes), and the same kinetic
signal family list intel._KINETIC_TELLS keys are derived from.
"""
from datetime import datetime, timezone

from forecast import HOTSPOTS, _bbox_contains, _items, _level


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


# A signal family is "lit" when its sub-score meets this threshold. Matches the
# brief's existing tells-board cutoff (intel.build_brief uses score >= 35) so
# the corroboration view and the brief view agree on what counts as a real tell.
LIT_THRESHOLD = 35

# The KINETIC families — hard-to-fake, operationally meaningful signals. The
# corroboration ratio and compound bonus are computed ONLY over these; news and
# events volume are ambient signals that shouldn't drive the "convergence"
# narrative. Order doesn't matter; this is a set semantically.
KINETIC_KEYS = ("military_air", "naval", "nav_warnings", "frontlines")

# Piecewise compound bonus added to a hotspot's base score when KINETIC signals
# corroborate. 0 or 1 lit -> no bonus (one signal is not corroboration). Stops
# at 25 when all four kinetic families are lit simultaneously. Picked so a
# moderate (50-ish) score with 3 kinetic tells lit (50 + 14 = 64) clearly
# outranks a similar score with one tell (50 + 0 = 50), without letting the
# bonus alone push a quiet hotspot into "high" territory unaided.
_COMPOUND_BONUS = {0: 0, 1: 0, 2: 8, 3: 14, 4: 20}
_COMPOUND_BONUS_KINETIC_ALL = 25   # all four kinetic lit -> max bonus


# ---------------------------------------------------------------------------
# Per-hotspot corroboration.
# ---------------------------------------------------------------------------
def _signal_score(hotspot, key):
    """Return the score of signal `key` on a forecast hotspot, or 0 if missing."""
    for s in hotspot.get("signals", []):
        if s.get("key") == key:
            return int(s.get("score") or 0)
    return 0


def _hotspot_corroboration(hotspot):
    """Compute the corroboration block for a single forecast hotspot."""
    lit_signals = []
    kinetic_lit = 0
    for s in hotspot.get("signals", []):
        key = s.get("key")
        score = int(s.get("score") or 0)
        if score >= LIT_THRESHOLD:
            lit_signals.append(key)
            if key in KINETIC_KEYS:
                kinetic_lit += 1

    # corroboration ratio is over KINETIC families only — that is the signal we
    # care about. 4/4 -> 1.0; 0/4 -> 0.0.
    corroboration = round(kinetic_lit / len(KINETIC_KEYS), 3)

    if kinetic_lit >= len(KINETIC_KEYS):
        bonus = _COMPOUND_BONUS_KINETIC_ALL
    else:
        bonus = _COMPOUND_BONUS.get(kinetic_lit, 0)

    base = int(hotspot.get("score") or 0)
    compound = max(0, min(100, base + bonus))
    level = _level(compound)

    if kinetic_lit == 0:
        verdict = "NONE"
    elif kinetic_lit == 1:
        verdict = "LOW"
    elif kinetic_lit == 2:
        verdict = "MODERATE"
    elif kinetic_lit == 3:
        verdict = "HIGH"
    else:
        verdict = "FULL"

    name = hotspot.get("name") or hotspot.get("id") or "hotspot"
    summary = (f"{name} corroboration {verdict} — "
               f"{kinetic_lit}/{len(KINETIC_KEYS)} kinetic signals lit"
               + (f" ({', '.join(lit_signals)})" if lit_signals else "")
               + f"; compound {compound}/100 ({level}).")

    return {
        "id": hotspot.get("id"),
        "name": name,
        "lat": hotspot.get("lat"),
        "lng": hotspot.get("lng"),
        "lit_count": len(lit_signals),
        "lit_signals": lit_signals,
        "kinetic_lit": kinetic_lit,
        "corroboration": corroboration,
        "compound_bonus": bonus,
        "base_score": base,
        "compound_score": compound,
        "compound_level": level,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Convergence scan — coarse global grid, find cells with multiple lit families
# that fall OUTSIDE the 12 predefined hotspot boxes.
# ---------------------------------------------------------------------------
GRID_DEG = 5.0      # 5 degree cells -> 36 lat x 72 lng = 2592 cells globally
MAX_CONVERGENCES = 8

# Layers the convergence scan iterates point items from. We deliberately use the
# same kinetic families as the per-hotspot scan, plus events (GDELT) as a co-
# signal — a cluster of mil_air + GDELT events in an unexpected box is exactly
# the kind of emerging incident this is meant to surface.
#
# `boats` is the AIS layer that the naval sub-score reads from in forecast.py.
# nav_warnings items may carry a top-level lat/lng OR a `coords` list of points
# — we handle both. frontlines is a polygon layer (no point coords) and is
# omitted from the convergence scan, which is point-based.
_SCAN_LAYER_FAMILY = {
    "military_air": "military_air",
    "boats":        "naval",
    "nav_warnings": "nav_warnings",
    "events":       "events",
}

# A cell only counts as convergence if at least one KINETIC family is present,
# preventing "events-only" or pure news-driven cells from triggering it.
_REQUIRE_AT_LEAST_ONE = {"military_air", "naval", "nav_warnings"}


def _cell_of(lat, lng):
    """Return the (lat_idx, lng_idx) cell that contains a point, or None if
    the coords are out of range / non-numeric. Lat indices 0..(180/GRID-1),
    lng indices 0..(360/GRID-1)."""
    if not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
        return None
    if lat < -90 or lat > 90 or lng < -180 or lng > 180:
        return None
    # clamp lat=90 / lng=180 down by one cell so they land in-bounds
    lat_i = min(int((lat + 90) // GRID_DEG), int(180 // GRID_DEG) - 1)
    lng_i = min(int((lng + 180) // GRID_DEG), int(360 // GRID_DEG) - 1)
    return (lat_i, lng_i)


def _cell_box(cell):
    """(lat_min, lat_max, lng_min, lng_max) of a grid cell."""
    lat_i, lng_i = cell
    lat_min = -90 + lat_i * GRID_DEG
    lng_min = -180 + lng_i * GRID_DEG
    return (lat_min, lat_min + GRID_DEG, lng_min, lng_min + GRID_DEG)


def _cell_center(cell):
    lat_min, lat_max, lng_min, lng_max = _cell_box(cell)
    return ((lat_min + lat_max) / 2.0, (lng_min + lng_max) / 2.0)


def _cell_overlaps_hotspot(cell, hotspot_boxes):
    """True if this grid cell's bbox overlaps any predefined hotspot box.

    bbox-overlap (not point-in-polygon) is intentional: we want to suppress a
    cell as 'already covered' even if its center falls just outside a hotspot
    box but the cell intersects it — the hotspot scan already accounts for it.
    """
    c_lat_min, c_lat_max, c_lng_min, c_lng_max = _cell_box(cell)
    for hb in hotspot_boxes:
        lat_min, lat_max, lng_min, lng_max = hb
        if not (c_lat_max < lat_min or c_lat_min > lat_max
                or c_lng_max < lng_min or c_lng_min > lng_max):
            return True
    return False


def _iter_layer_points(layer_id, payload):
    """Yield (lat, lng) for each item in a layer payload. nav_warnings items can
    carry several coordinate points; we yield each one (a multi-point warning
    that touches several cells is correctly counted in all of them)."""
    for item in _items(payload):
        if not isinstance(item, dict):
            continue
        if layer_id == "nav_warnings":
            coords = item.get("coords") or []
            yielded_any = False
            for c in coords:
                if isinstance(c, (list, tuple)) and len(c) >= 2:
                    lat, lng = c[0], c[1]
                    if isinstance(lat, (int, float)) and isinstance(lng, (int, float)):
                        yield (lat, lng)
                        yielded_any = True
            # fall back to top-level lat/lng if no coords list
            if not yielded_any:
                lat, lng = item.get("lat"), item.get("lng")
                if isinstance(lat, (int, float)) and isinstance(lng, (int, float)):
                    yield (lat, lng)
        else:
            lat, lng = item.get("lat"), item.get("lng")
            if isinstance(lat, (int, float)) and isinstance(lng, (int, float)):
                yield (lat, lng)


def _scan_convergences(layers, hotspot_boxes):
    """Coarse grid scan for convergence cells outside predefined hotspots."""
    # cell -> {family: count}
    grid = {}
    for layer_id, family in _SCAN_LAYER_FAMILY.items():
        payload = layers.get(layer_id)
        if not payload:
            continue
        for lat, lng in _iter_layer_points(layer_id, payload):
            cell = _cell_of(lat, lng)
            if cell is None:
                continue
            cell_fams = grid.get(cell)
            if cell_fams is None:
                cell_fams = {}
                grid[cell] = cell_fams
            cell_fams[family] = cell_fams.get(family, 0) + 1

    convergences = []
    for cell, fams in grid.items():
        # require >=2 distinct families AND at least one kinetic family
        if len(fams) < 2:
            continue
        if not any(f in _REQUIRE_AT_LEAST_ONE for f in fams):
            continue
        if _cell_overlaps_hotspot(cell, hotspot_boxes):
            continue
        total_items = sum(fams.values())
        # score: families lit (heavy) + a mild bump for total item density.
        # 2 families -> 50, 3 -> 75, 4 -> 95; +up to 5 from density (log-ish via
        # min(5, total // 8)).
        fam_n = len(fams)
        family_score = {2: 50, 3: 75, 4: 95}.get(fam_n, 95)
        density_bonus = min(5, total_items // 8)
        score = min(100, family_score + density_bonus)

        lat_c, lng_c = _cell_center(cell)
        box = _cell_box(cell)
        # human-readable summary names the families + their item counts
        family_bits = ", ".join(
            f"{k} ({v})" for k, v in sorted(fams.items(),
                                            key=lambda kv: kv[1], reverse=True))
        summary = (f"Emerging convergence near ({lat_c:.1f}, {lng_c:.1f}): "
                   f"{family_bits} — not on hotspot list.")
        convergences.append({
            "id": f"conv-{lat_c:.1f}-{lng_c:.1f}",
            "lat": lat_c,
            "lng": lng_c,
            "box": list(box),
            "lit_signals": sorted(fams.keys()),
            "family_counts": dict(fams),
            "item_count": total_items,
            "score": score,
            "summary": summary,
        })

    # Rank: more families first, then more total items.
    convergences.sort(key=lambda c: (len(c["lit_signals"]), c["item_count"]),
                      reverse=True)
    return convergences[:MAX_CONVERGENCES]


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------
def corroborate(forecast, layers, *, now=None):
    """Build the corroboration payload from a forecast + the layers it used.

    Pure function — no network, no clock except the explicit `now` (when None,
    walltime is used for `updatedAt`). Safe on a cold/empty forecast: returns a
    payload with empty hotspots/convergences lists.
    """
    updated_at = (now.isoformat() if now is not None else _now_iso())
    hotspots = forecast.get("hotspots") if isinstance(forecast, dict) else None
    hotspots = hotspots or []

    hotspot_blocks = [_hotspot_corroboration(h) for h in hotspots]
    # Sort by compound_score so the dashboard's top-of-list is the most-
    # corroborated escalation, not the highest base score with no corroboration.
    hotspot_blocks.sort(key=lambda h: (h["compound_score"], h["kinetic_lit"]),
                        reverse=True)

    hotspot_boxes = [tuple(hs["box"]) for hs in HOTSPOTS]
    convergences = _scan_convergences(layers or {}, hotspot_boxes)

    return {
        "updatedAt": updated_at,
        "generatedFrom": (forecast.get("updatedAt")
                          if isinstance(forecast, dict) else None),
        "litThreshold": LIT_THRESHOLD,
        "kineticFamilies": list(KINETIC_KEYS),
        "hotspots": hotspot_blocks,
        "convergences": convergences,
    }
