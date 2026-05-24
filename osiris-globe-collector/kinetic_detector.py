"""Kinetic-event detector v2 — JSOC-doctrine refactor.

Same purpose as v1: detect ONSET of kinetic events in space+time on a 1°
world grid. ("When airspace shuts down + CCTV goes dark + we see military
activity, something is going on.")

v2 changes (empirical-rigor wins over v1 ad-hoc heuristics):

1. **Per-cell, per-time-of-day Welford baselines.** Each cell carries 6
   four-hour ToD buckets (0-4, 4-8, …, 20-24 UTC), each with running mean
   + variance per signal. Triggers are Z-scores against the same-ToD
   bucket, NOT percent-deltas vs a 40-min rolling window. 03:00 UTC and
   14:00 UTC stop being treated identically; Manhattan and rural Wyoming
   develop their own personalities after ~1 day of warm-up.

2. **Calibrated sigmoid confidence.** Each signature returns confidence
   as a weighted sum of logistic sub-scores per indicator. Weights at
   the top of the file, one block per signature, hand-tuned from JSOC
   ABI / I&W literature. (Bayesian fitting is future work — needs
   ground-truth outcome data we don't have yet.)

3. **Weighted multi-domain composite.** Replaces "≥2 of signatures 1-3
   fire" with a domain-scored composite ({air, sea, ground, cyber, em}).
   Fires when ≥2 domains lit AND weighted score ≥ 0.55.

4. **Exponential cooldown decay.** Replaces binary 1h gate. Lets a
   worsening situation re-emit even within the cooldown window if the
   evidence has materially strengthened.

Public API is unchanged:
    detect_kinetic_insights(layers, prev_layers, prev_state, prev_insights,
                            *, now=None) -> (payload, new_state)

State migration: v1 state dicts are read on the first v2 cycle; missing
v2 fields (tod_buckets) are initialized empty. No flag day required.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any

# Reuse forecast.py helpers: _items (payload-shape-safe item extraction) and
# _bbox_contains (point-in-bbox test). _level not needed — we emit our own
# severity directly.
from forecast import _items, _bbox_contains


# ---------------------------------------------------------------------------
# Grid + state limits
# ---------------------------------------------------------------------------
GRID_DEG = 1.0                  # 1° cells -> 180 lat × 360 lng = 64,800 cells
CELL_TTL_S = 24 * 3600          # cells unobserved 24h drop out
CELL_CAP = 5000                 # hard cap on per-cycle active cells
INSIGHTS_WINDOW = timedelta(days=7)
INSIGHTS_MAX = 250

# History depth — 8 samples at 5-min intel cadence = 40 min, generous for the
# "last 1h baseline" comparisons. We pad to 8 because some signatures look at
# 4h ago (military_naval) — those use the oldest available sample as proxy.
HISTORY_DEPTH = 8

# Re-emit cooldown — don't surface the same (cell, signature) twice within
# this window unless severity escalates. Stops a persistent condition from
# flooding alerts cycle after cycle.
COOLDOWN_S = 60 * 60            # 1h


# ---------------------------------------------------------------------------
# Detection thresholds (documented for tuning)
# ---------------------------------------------------------------------------
# Multi-signal: airspace_denial
FLIGHT_DROP_PCT = 50.0          # civilian flights dropped this % vs 1h baseline
MIL_AIR_RISE_PCT = 20.0         # military_air rose this % vs 1h baseline
MIL_AIR_NEIGHBOR_RADIUS_DEG = 2.0  # ~200km — check this many cells around

# Multi-signal: surveillance_blackout
CCTV_DARK_COUNT_MULTI = 3       # ≥N cameras went silent in 60 min
CCTV_DARK_WINDOW_S = 60 * 60    # within last hour

# Multi-signal: naval_massing
NAVAL_SURGE_FROM = 0            # was N or fewer
NAVAL_SURGE_TO = 3              # now ≥ N
NAVAL_HISTORY_LOOKBACK = 8      # samples (40 min at 5-min cadence, proxy for ~4h)
DARK_FLEET_RADIUS_DEG = 1.0     # 100km

# Single-signal: airspace_shutdown
LARGE_NOTAM_KM2 = 50_000        # km²
BUSY_AIRSPACE_FLIGHTS = 50      # normally ≥50 civilian flights in cell

# Single-signal: mass_cctv_blackout
CCTV_DARK_COUNT_SINGLE = 10


# ---------------------------------------------------------------------------
# v2 — Z-score baselines (per-cell, per-time-of-day, Welford)
# ---------------------------------------------------------------------------
# 6 four-hour ToD buckets: 0-4, 4-8, 8-12, 12-16, 16-20, 20-24 UTC.
# Lookup: bucket = hour // 4.
TOD_BUCKETS = 6
TOD_BUCKET_HOURS = 4

# Minimum samples per (cell, ToD bucket) before Z-score gating engages.
# Below this we fall back to the v1 percent-delta logic so cold-start
# doesn't suppress everything for 24h.
ZSCORE_MIN_SAMPLES = 6

# Z-score thresholds. Standard statistical defaults; tunable from Phase A run.
# Negative for drops (civilian flights), positive for rises (military).
Z_FLIGHT_DROP = -2.0     # civilian flights ~ 2σ below same-ToD mean
Z_MIL_AIR_RISE = +1.5    # military air ~ 1.5σ above same-ToD mean
Z_MIL_NAVAL_RISE = +2.0  # naval massing ~ 2σ above same-ToD mean

# Bucket cap — protect memory if a cell explodes (shouldn't, but defensive).
BUCKET_VARIANCE_CAP = 1.0e9


# ---------------------------------------------------------------------------
# v2 — Calibrated sigmoid confidence
# ---------------------------------------------------------------------------
# Weight tables per signature. Each indicator contributes:
#     w_i * sigmoid((value_i - threshold_i) / scale_i)
# Sum across i, clamp to [0, 1]. Sum of weights ≈ 1.0 by convention but not
# enforced — clamp at output. References: JSOC ABI literature; JP 2-0 Annex on
# Indicators & Warnings; war-college analyses of indicator weighting.
SIGMOID_WEIGHTS = {
    "airspace_denial": [
        # indicator: |civ_z| (Z magnitude; bigger drop -> higher contribution)
        {"key": "civ_z_abs",   "w": 0.40, "threshold": 2.0, "scale": 0.5},
        # indicator: mil_z (positive = rise)
        {"key": "mil_z",       "w": 0.30, "threshold": 1.5, "scale": 0.5},
        # indicator: notam_covers (binary 0/1)
        {"key": "notam_covers","w": 0.30, "threshold": 0.5, "scale": 0.2},
    ],
    "surveillance_blackout": [
        {"key": "dark_count",  "w": 0.50, "threshold": 5.0, "scale": 2.0},
        {"key": "outage_or_mil","w": 0.35, "threshold": 0.5, "scale": 0.2},
        {"key": "no_weather",  "w": 0.15, "threshold": 0.5, "scale": 0.2},
    ],
    "naval_massing": [
        {"key": "naval_z",     "w": 0.50, "threshold": 2.0, "scale": 0.5},
        {"key": "dark_nearby", "w": 0.30, "threshold": 0.5, "scale": 0.2},
        {"key": "nav_warning", "w": 0.20, "threshold": 0.5, "scale": 0.2},
    ],
    "airspace_shutdown": [
        {"key": "notam_area_norm", "w": 0.60, "threshold": 1.0, "scale": 0.5},
        {"key": "busy_baseline_norm","w": 0.40, "threshold": 1.0, "scale": 0.5},
    ],
    "mass_cctv_blackout": [
        {"key": "dark_count",  "w": 0.80, "threshold": 12.0, "scale": 3.0},
        {"key": "no_weather",  "w": 0.20, "threshold": 0.5, "scale": 0.2},
    ],
    "internet_shutdown": [
        # always-on indicator with hardcoded weight (cause is binary determinant)
        {"key": "cause_strong","w": 1.00, "threshold": 0.5, "scale": 0.2},
    ],
}


# ---------------------------------------------------------------------------
# v2 — Weighted-domain multi-domain composite
# ---------------------------------------------------------------------------
# Each signature contributes to one domain. multi_domain fires when
# domain_count(domains_lit) ≥ 2 AND weighted_score ≥ MULTI_DOMAIN_THRESHOLD.
SIGNATURE_DOMAIN = {
    "airspace_denial":       "air",
    "surveillance_blackout": "ground",
    "naval_massing":         "sea",
    "airspace_shutdown":     "air",
    "mass_cctv_blackout":    "ground",
    "internet_shutdown":     "cyber",
    # Phase D additions (resolve to None until those signatures land):
    "gps_jam_surge":         "em",
    "comms_blackout":        "cyber",
    "logistics_surge":       "sea",
}
DOMAIN_WEIGHT = {"air": 0.25, "sea": 0.20, "ground": 0.20, "cyber": 0.20, "em": 0.15}
MULTI_DOMAIN_THRESHOLD = 0.55


# ---------------------------------------------------------------------------
# v2 — Exponential cooldown decay
# ---------------------------------------------------------------------------
# Effective confidence = raw - DECAY_PEAK * exp(-(now - last_fire) / DECAY_TAU)
# DECAY_TAU=1800s (30min half-life-ish). At t=0 after fire, eff -= 0.5.
# At t=1h, eff -= 0.5 * e^(-2) ≈ 0.068.
# A re-emit needs raw confidence to clear (gate + decay-deficit) → only happens
# when evidence has materially strengthened.
DECAY_PEAK = 0.5
DECAY_TAU_S = 1800.0
EFFECTIVE_CONFIDENCE_GATE = 0.50  # floor for emission after decay

# Severity by signature (drives UI tier styling).
_SEVERITY = {
    "multi_domain":           "critical",
    "airspace_denial":        "warning",
    "surveillance_blackout":  "warning",
    "naval_massing":          "warning",
    "airspace_shutdown":      "info",
    "mass_cctv_blackout":     "info",
    "internet_shutdown":      "info",
}

# Color per signature (consumed by globe ring halo + ContextPanel chip).
_COLOR = {
    "multi_domain":           "#FF00FF",  # magenta — the headline alarm
    "airspace_denial":        "#FF1744",
    "surveillance_blackout":  "#FF1744",
    "naval_massing":          "#FF1744",
    "airspace_shutdown":      "#FF9500",
    "mass_cctv_blackout":     "#FF9500",
    "internet_shutdown":      "#9C27B0",
}

_TIER = {
    "multi_domain":           "multi_signal",
    "airspace_denial":        "multi_signal",
    "surveillance_blackout":  "multi_signal",
    "naval_massing":          "multi_signal",
    "airspace_shutdown":      "single_signal",
    "mass_cctv_blackout":     "single_signal",
    "internet_shutdown":      "single_signal",
}


# ---------------------------------------------------------------------------
# Cell math
# ---------------------------------------------------------------------------
def _cell_of(lat, lng):
    """Return (lat_idx, lng_idx) for a point, or None if out-of-range."""
    if not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
        return None
    if lat < -90 or lat > 90 or lng < -180 or lng > 180:
        return None
    lat_i = min(int((lat + 90) // GRID_DEG), int(180 // GRID_DEG) - 1)
    lng_i = min(int((lng + 180) // GRID_DEG), int(360 // GRID_DEG) - 1)
    return (lat_i, lng_i)


def _cell_box(cell):
    lat_i, lng_i = cell
    lat_min = -90 + lat_i * GRID_DEG
    lng_min = -180 + lng_i * GRID_DEG
    return (lat_min, lat_min + GRID_DEG, lng_min, lng_min + GRID_DEG)


def _cell_center(cell):
    lat_min, lat_max, lng_min, lng_max = _cell_box(cell)
    return ((lat_min + lat_max) / 2.0, (lng_min + lng_max) / 2.0)


def _neighbor_cells(cell, radius_deg=1.0):
    """Cells within `radius_deg` of the given cell (Chebyshev — square ring)."""
    lat_i, lng_i = cell
    r = int(radius_deg // GRID_DEG)
    out = []
    for di in range(-r, r + 1):
        for dj in range(-r, r + 1):
            nl = lat_i + di
            nj = (lng_i + dj) % int(360 // GRID_DEG)
            if 0 <= nl < int(180 // GRID_DEG):
                out.append((nl, nj))
    return out


# ---------------------------------------------------------------------------
# Per-cell layer indexing — group items into the cell they fall in
# ---------------------------------------------------------------------------
def _items_by_cell(payload):
    """Bucket each lat/lng item of a payload into its containing 1° cell.
    Returns {cell: [items]}."""
    out: dict = {}
    for it in _items(payload):
        if not isinstance(it, dict):
            continue
        lat, lng = it.get("lat"), it.get("lng")
        cell = _cell_of(lat, lng)
        if cell is None:
            continue
        out.setdefault(cell, []).append(it)
    return out


def _flights_by_cell(payload, category_filter=None):
    """Like _items_by_cell but only retains flights matching category_filter
    (None = all). Civil = not in {military, jet} when filtering for the
    'civilian flight density' signal."""
    out: dict = {}
    for it in _items(payload):
        if not isinstance(it, dict):
            continue
        cat = (it.get("category") or "").lower()
        if category_filter == "civilian":
            if cat in ("military",):
                continue
        elif category_filter == "military":
            if cat != "military":
                continue
        lat, lng = it.get("lat"), it.get("lng")
        cell = _cell_of(lat, lng)
        if cell is None:
            continue
        out.setdefault(cell, []).append(it)
    return out


# ---------------------------------------------------------------------------
# NOTAM / nav_warning / frontlines polygon coverage helpers
# ---------------------------------------------------------------------------
def _notam_covers_cell(notam_items, cell):
    """True if any notam_item bbox / radius covers the cell.
    NOTAM items in our pipeline carry lat/lng + radius_km OR a bbox-style
    polygon. We accept either — cheap point-in-radius / bbox test."""
    lat_min, lat_max, lng_min, lng_max = _cell_box(cell)
    cell_lat = (lat_min + lat_max) / 2
    cell_lng = (lng_min + lng_max) / 2
    for n in notam_items:
        radius_km = n.get("radius_km") or n.get("radius_nm")
        if radius_km and n.get("lat") is not None and n.get("lng") is not None:
            try:
                # crude: 1° ≈ 111 km. Within (radius_km / 111) degrees of cell center?
                if abs(float(n["lat"]) - cell_lat) < float(radius_km) / 111.0 + GRID_DEG / 2 \
                        and abs(float(n["lng"]) - cell_lng) < float(radius_km) / 111.0 + GRID_DEG / 2:
                    return True
            except (TypeError, ValueError):
                pass
        # bbox form: [w, s, e, n]
        bbox = n.get("bbox")
        if isinstance(bbox, list) and len(bbox) == 4:
            try:
                w, s, e, no = float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])
                if not (lng_max < w or lng_min > e or lat_max < s or lat_min > no):
                    return True
            except (TypeError, ValueError):
                pass
    return False


def _notam_area_km2(notam):
    """Approximate area of a NOTAM. If a radius is given, area = π r².
    If bbox is given, area = bbox_w_km × bbox_h_km."""
    radius_km = notam.get("radius_km") or notam.get("radius_nm")
    if radius_km:
        try:
            return 3.14159 * float(radius_km) ** 2
        except (TypeError, ValueError):
            return 0.0
    bbox = notam.get("bbox")
    if isinstance(bbox, list) and len(bbox) == 4:
        try:
            w, s, e, no = float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])
            return abs(e - w) * abs(no - s) * 111.0 * 111.0
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _nav_warning_covers_cell(nav_warnings, cell):
    """nav_warnings items may carry coords (multi-point) or lat/lng. Crude
    same-cell check — sufficient for our 1° resolution."""
    for it in nav_warnings:
        coords = it.get("coords") or []
        if coords:
            for c in coords:
                if isinstance(c, (list, tuple)) and len(c) >= 2:
                    if _cell_of(c[0], c[1]) == cell:
                        return True
        if _cell_of(it.get("lat"), it.get("lng")) == cell:
            return True
    return False


def _frontline_covers_cell(frontlines_payload, cell):
    """Frontlines items are GeoJSON Feature[] (Polygon control areas).
    We check if any frontline feature's bbox overlaps the cell — point-in-poly
    would be more correct but our 1° resolution makes bbox sufficient."""
    lat_min, lat_max, lng_min, lng_max = _cell_box(cell)
    feats = _items(frontlines_payload)
    for feat in feats:
        geom = feat.get("geometry") if isinstance(feat, dict) else None
        if not geom or geom.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        coords = geom.get("coordinates")
        # find bbox of feature
        f_w = 180.0; f_e = -180.0; f_s = 90.0; f_n = -90.0

        def _walk(c):
            nonlocal f_w, f_e, f_s, f_n
            if isinstance(c, (list, tuple)) and len(c) >= 2 \
                    and isinstance(c[0], (int, float)) and isinstance(c[1], (int, float)):
                lng, lat = float(c[0]), float(c[1])
                if lng < f_w: f_w = lng
                if lng > f_e: f_e = lng
                if lat < f_s: f_s = lat
                if lat > f_n: f_n = lat
            elif isinstance(c, list):
                for x in c:
                    _walk(x)

        _walk(coords)
        # bbox overlap
        if not (lng_max < f_w or lng_min > f_e or lat_max < f_s or lat_min > f_n):
            return True
    return False


# ---------------------------------------------------------------------------
# Weather-suppression helper — avoid blaming a hurricane for cameras going dark
# ---------------------------------------------------------------------------
_WEATHER_LAYERS = ("hurricanes", "tornado_warnings", "tsunami", "natural-events",
                   "earthquakes", "gdacs", "wildfire", "volcanoes")


def _weather_active_in_cell(layers, cell):
    """True if a meaningful weather/hazard event is active in cell."""
    for lid in _WEATHER_LAYERS:
        payload = layers.get(lid)
        if not payload:
            continue
        for it in _items(payload):
            if _cell_of(it.get("lat"), it.get("lng")) == cell:
                return True
    return False


# ---------------------------------------------------------------------------
# CCTV health tracking (per camera, per cell, healthy → silent transitions)
# ---------------------------------------------------------------------------
def _cctv_now_seen(layers):
    """{camera_id: cell} for cameras present in this cycle's CCTV blob.
    The CCTV blob items have an `id` (e.g. 'nyctmc-XXXX') + lat/lng. Camera
    is 'seen' if it appears in the blob this cycle (the collector skips
    cameras with no recent snapshot)."""
    payload = layers.get("cctv")
    out: dict = {}
    if not payload:
        return out
    for it in _items(payload):
        cid = it.get("id") or it.get("camera_id")
        if not cid:
            continue
        cell = _cell_of(it.get("lat"), it.get("lng"))
        if cell is None:
            continue
        out[cid] = cell
    return out


# ---------------------------------------------------------------------------
# Per-cell state (rolling history of signal counts)
# ---------------------------------------------------------------------------
def _ensure_cell(state, cell, now_ts):
    """Initialize cell history if absent. Returns the cell-state dict."""
    cells = state.setdefault("cells", {})
    cs = cells.get(cell)
    if cs is None:
        cs = {
            "flights_count": [],         # rolling, len ≤ HISTORY_DEPTH
            "mil_air_count": [],
            "mil_naval_count": [],
            "last_seen_ts": now_ts,
            "last_signature_ts": {},     # {signature: ts}  for cooldown
        }
        cells[cell] = cs
    cs["last_seen_ts"] = now_ts
    return cs


def _push_history(arr, value, max_len=HISTORY_DEPTH):
    arr.append(int(value))
    while len(arr) > max_len:
        arr.pop(0)


def _evict_stale_cells(state, now_ts):
    """Drop cells unobserved > TTL or beyond CAP."""
    cells = state.get("cells") or {}
    if not cells:
        return
    to_drop = [c for c, cs in cells.items()
               if now_ts - cs.get("last_seen_ts", now_ts) > CELL_TTL_S]
    for c in to_drop:
        cells.pop(c, None)
    if len(cells) > CELL_CAP:
        ordered = sorted(cells.items(), key=lambda kv: kv[1].get("last_seen_ts", 0))
        for c, _ in ordered[: len(cells) - CELL_CAP]:
            cells.pop(c, None)


# ---------------------------------------------------------------------------
# Signature evaluators — each takes the per-cell context + state, returns
# either an insight dict (signature fired) or None.
# ---------------------------------------------------------------------------
def _baseline(history, current):
    """Return (baseline, delta_pct). Baseline = average of the older half of
    the history (excluding `current`). Returns (None, None) if not enough
    history.

    v2 retains this as the COLD-START fallback before any ToD bucket has
    accumulated ZSCORE_MIN_SAMPLES observations. Z-score gating in v2
    supersedes it once history is sufficient."""
    if not history or len(history) < 2:
        return None, None
    older = history[:-1] if len(history) > 1 else history
    if not older:
        return None, None
    baseline = sum(older) / len(older)
    if baseline <= 0:
        return baseline, None
    delta_pct = (current - baseline) / baseline * 100.0
    return baseline, delta_pct


# ---------------------------------------------------------------------------
# v2 — Welford online stats + per-cell ToD-bucketed Z-score
# ---------------------------------------------------------------------------
def _welford_update(bucket, x):
    """One-step Welford online mean+variance update.

    bucket dict: {n, mean, M2} — n=count, mean=running mean,
    M2=sum of squared deviations from mean (used to derive variance).
    Time complexity O(1); memory O(1).
    Numerical-stability tested up to n~1e6.
    """
    bucket["n"] = bucket.get("n", 0) + 1
    n = bucket["n"]
    mean = bucket.get("mean", 0.0)
    delta = x - mean
    mean += delta / n
    delta2 = x - mean
    bucket["mean"] = mean
    bucket["M2"] = min(bucket.get("M2", 0.0) + delta * delta2, BUCKET_VARIANCE_CAP)


def _welford_stats(bucket):
    """Return (mean, stddev) from a Welford bucket, or (None, None) if n<2.
    Sample stddev (divisor n-1). Floor stddev at 1.0 to prevent Z explosions
    when variance is genuinely ≈0 (constant signal)."""
    n = bucket.get("n", 0)
    if n < 2:
        return None, None
    var = bucket["M2"] / (n - 1)
    stddev = max(var ** 0.5, 1.0)
    return bucket["mean"], stddev


def _tod_bucket_idx(now_dt):
    """Which ToD bucket [0..TOD_BUCKETS-1] does this datetime belong to?"""
    return now_dt.hour // TOD_BUCKET_HOURS


def _zscore_for(cs, signal_key, current, now_dt):
    """Compute Z-score of `current` vs the SAME-ToD bucket for this cell.

    Returns (z, n_samples) or (None, n_samples) if bucket has < ZSCORE_MIN_SAMPLES
    observations — caller uses n to decide whether to fall back to v1 baseline.
    """
    buckets = cs.setdefault("tod_buckets", {}).setdefault(signal_key, {})
    bidx = _tod_bucket_idx(now_dt)
    bucket = buckets.get(bidx)
    if not bucket:
        return None, 0
    n = bucket.get("n", 0)
    if n < ZSCORE_MIN_SAMPLES:
        return None, n
    mean, stddev = _welford_stats(bucket)
    if mean is None or stddev is None:
        return None, n
    return (current - mean) / stddev, n


def _update_tod_bucket(cs, signal_key, current, now_dt):
    """Push `current` into the appropriate (signal, ToD) bucket for this cell."""
    buckets = cs.setdefault("tod_buckets", {}).setdefault(signal_key, {})
    bidx = _tod_bucket_idx(now_dt)
    bucket = buckets.setdefault(bidx, {"n": 0, "mean": 0.0, "M2": 0.0})
    _welford_update(bucket, float(current))


# ---------------------------------------------------------------------------
# v2 — Calibrated sigmoid confidence
# ---------------------------------------------------------------------------
def _sigmoid(x):
    """Standard logistic. Safe for large |x| (clamp at ±50 to avoid overflow)."""
    if x > 50:
        return 1.0
    if x < -50:
        return 0.0
    import math
    return 1.0 / (1.0 + math.exp(-x))


def _sigmoid_confidence(signature, indicators):
    """Score from the SIGMOID_WEIGHTS table for this signature.

    indicators: {key: value} dict matching the keys in SIGMOID_WEIGHTS[sig].
    Missing keys treated as 0 (no contribution to that indicator).
    Output clamped to [0, 1].
    """
    table = SIGMOID_WEIGHTS.get(signature, [])
    if not table:
        return 0.0
    total = 0.0
    for ind in table:
        v = float(indicators.get(ind["key"], 0.0))
        score = _sigmoid((v - ind["threshold"]) / max(ind["scale"], 1e-6))
        total += ind["w"] * score
    return max(0.0, min(1.0, total))


def _eval_airspace_denial(cell, civ_count, mil_count, layers, cs, now_dt):
    """Signature 1: flight density drop + NOTAM + military_air rise.

    v2: Z-score gate against same-ToD baseline when ZSCORE_MIN_SAMPLES
    available; otherwise falls back to v1 percent-delta logic for cold-start.
    Confidence via SIGMOID_WEIGHTS['airspace_denial'].
    """
    # NOTAM cover is the cheap gate — bail early if no airspace closure exists.
    notam_items = _items(layers.get("notams"))
    notam_covers = 1 if _notam_covers_cell(notam_items, cell) else 0
    if not notam_covers:
        return None

    # Civilian flight drop signal (Z-score vs same-ToD bucket, fallback to v1 pct).
    civ_z, civ_n = _zscore_for(cs, "flights_count", civ_count, now_dt)
    if civ_z is not None:
        if civ_z > Z_FLIGHT_DROP:
            return None
        civ_signal_value = round(abs(civ_z), 2)
        civ_summary = f"civilian flights {abs(civ_z):.1f}σ below same-ToD baseline"
        civ_metric = "z_drop_sigma"
    else:
        baseline_civ, civ_delta = _baseline(cs["flights_count"], civ_count)
        if baseline_civ is None or civ_delta is None or civ_delta > -FLIGHT_DROP_PCT:
            return None
        civ_signal_value = round(abs(civ_delta))
        civ_summary = f"civilian flights dropped {abs(civ_delta):.0f}% vs short-term baseline"
        civ_metric = "density_drop_pct"

    # Military air rise (Z-score vs same-ToD bucket, fallback to v1 pct).
    mil_z, mil_n = _zscore_for(cs, "mil_air_count", mil_count, now_dt)
    if mil_z is not None:
        if mil_z < Z_MIL_AIR_RISE:
            return None
        mil_signal_value = round(mil_z, 2)
        mil_summary = f"military air {mil_z:.1f}σ above same-ToD baseline"
        mil_metric = "z_rise_sigma"
    else:
        _, mil_delta = _baseline(cs["mil_air_count"], mil_count)
        if mil_delta is None or mil_delta < MIL_AIR_RISE_PCT:
            return None
        mil_signal_value = round(mil_delta)
        mil_summary = f"military_air up {mil_delta:.0f}%"
        mil_metric = "count_rise_pct"

    confidence = _sigmoid_confidence("airspace_denial", {
        "civ_z_abs": abs(civ_z) if civ_z is not None else min(abs((_baseline(cs["flights_count"], civ_count)[1] or 0)) / 25.0, 4.0),
        "mil_z": mil_z if mil_z is not None else min((_baseline(cs["mil_air_count"], mil_count)[1] or 0) / 25.0, 4.0),
        "notam_covers": 1.0,
    })
    return {
        "signature": "airspace_denial",
        "confidence": round(confidence, 2),
        "title": "Airspace denial pattern",
        "summary": f"{civ_summary} · active NOTAM covers cell · {mil_summary}",
        "signals": [
            {"layer": "flights", "metric": civ_metric,
             "value": civ_signal_value, "delta_label": "vs same-ToD baseline"},
            {"layer": "notams", "metric": "active_in_cell", "value": 1,
             "delta_label": "covers cell"},
            {"layer": "military_air", "metric": mil_metric,
             "value": mil_signal_value, "delta_label": "vs same-ToD baseline"},
        ],
    }


def _eval_surveillance_blackout(cell, now_ts, layers, state, cctv_now,
                                cctv_dark_in_cell, now_dt):
    """Signature 2: ≥3 CCTV cameras went healthy → silent within 60 min,
    no weather to blame, AND (internet_outage in country OR mil_air/naval rise).

    v2: confidence via sigmoid weights; military-rise corroborator uses Z-score
    if same-ToD bucket is warmed up, else falls back to v1 percent-delta.
    """
    if cctv_dark_in_cell < CCTV_DARK_COUNT_MULTI:
        return None
    if _weather_active_in_cell(layers, cell):
        return None
    # corroborating signal: internet_outage OR military rise
    outage_items = _items(layers.get("internet_outages"))
    has_outage = bool(outage_items)
    cs = state.get("cells", {}).get(cell) or {}

    # Military rise: prefer Z-score, fall back to v1 pct.
    last_mil = cs.get("mil_air_count", [0])[-1] if cs.get("mil_air_count") else 0
    last_naval = cs.get("mil_naval_count", [0])[-1] if cs.get("mil_naval_count") else 0
    mil_z, _ = _zscore_for(cs, "mil_air_count", last_mil, now_dt)
    naval_z, _ = _zscore_for(cs, "mil_naval_count", last_naval, now_dt)
    mil_rise_z = (mil_z is not None and mil_z >= Z_MIL_AIR_RISE) \
                 or (naval_z is not None and naval_z >= Z_MIL_AIR_RISE)
    if not mil_rise_z:
        _, mil_delta = _baseline(cs.get("mil_air_count", []), last_mil)
        _, naval_delta = _baseline(cs.get("mil_naval_count", []), last_naval)
        mil_rise = (mil_delta is not None and mil_delta >= MIL_AIR_RISE_PCT) \
                   or (naval_delta is not None and naval_delta >= MIL_AIR_RISE_PCT)
    else:
        mil_rise = True
        mil_delta = mil_z * 50 if mil_z else 0  # for summary text
    if not (has_outage or mil_rise):
        return None
    confidence = _sigmoid_confidence("surveillance_blackout", {
        "dark_count": float(cctv_dark_in_cell),
        "outage_or_mil": 1.0 if (has_outage or mil_rise) else 0.0,
        "no_weather": 1.0,
    })
    bits = [f"{cctv_dark_in_cell} CCTV cameras silent in last 60 min"]
    if has_outage: bits.append("internet outage active")
    if mil_rise:
        if mil_z is not None and mil_z >= Z_MIL_AIR_RISE:
            bits.append(f"military air {mil_z:.1f}σ above baseline")
        else:
            bits.append("military activity rising")
    return {
        "signature": "surveillance_blackout",
        "confidence": round(confidence, 2),
        "title": "Coordinated surveillance blackout",
        "summary": " · ".join(bits),
        "signals": [
            {"layer": "cctv", "metric": "cameras_dark",
             "value": cctv_dark_in_cell, "delta_label": "in 60 min"},
            *([{"layer": "internet_outages", "metric": "active_country",
                "value": 1, "delta_label": ""}] if has_outage else []),
            *([{"layer": "military_air", "metric": "z_rise" if mil_z is not None else "count_rise_pct",
                "value": round(mil_z, 2) if mil_z is not None else round((mil_delta or 0)),
                "delta_label": "σ vs baseline" if mil_z is not None else "vs baseline"}]
              if mil_rise else []),
        ],
    }


def _eval_naval_massing(cell, naval_count, layers, cs, now_dt):
    """Signature 3: military_naval surge + dark_fleet nearby + nav_warning.

    v2: surge detection via Z-score (vs same-ToD baseline) replaces the brittle
    "history[0] <= NAVAL_SURGE_FROM" gate. Cold-start fallback still allows the
    v1 logic so we don't go silent for 24h.
    """
    if naval_count < NAVAL_SURGE_TO:
        return None
    naval_z, naval_n = _zscore_for(cs, "mil_naval_count", naval_count, now_dt)
    if naval_z is not None:
        # Warm: require sigma rise
        if naval_z < Z_MIL_NAVAL_RISE:
            return None
        surge_summary = f"{naval_count} naval vessels in cell ({naval_z:.1f}σ above same-ToD baseline)"
    else:
        # Cold-start: v1 surge gate (history[0] ≤ NAVAL_SURGE_FROM, now ≥ NAVAL_SURGE_TO)
        history = cs["mil_naval_count"]
        if history and history[0] > NAVAL_SURGE_FROM and len(history) >= NAVAL_HISTORY_LOOKBACK:
            return None
        if not history:
            return None
        surge_summary = f"{naval_count} military_naval vessels in cell (was ≤{NAVAL_SURGE_FROM})"
    # dark_fleet nearby
    dark_items = _items(layers.get("dark_fleet"))
    has_dark_nearby = False
    for d in dark_items:
        if _cell_of(d.get("lat"), d.get("lng")) in _neighbor_cells(cell, DARK_FLEET_RADIUS_DEG):
            has_dark_nearby = True
            break
    if not has_dark_nearby:
        return None
    # nav_warnings
    if not _nav_warning_covers_cell(_items(layers.get("nav_warnings")), cell):
        return None
    confidence = _sigmoid_confidence("naval_massing", {
        "naval_z": naval_z if naval_z is not None else min(naval_count / 3.0, 4.0),
        "dark_nearby": 1.0,
        "nav_warning": 1.0,
    })
    return {
        "signature": "naval_massing",
        "confidence": round(confidence, 2),
        "title": "Naval kinetic posture",
        "summary": (f"{surge_summary} · dark-fleet vessel within 100km · "
                    f"nav_warning active"),
        "signals": [
            {"layer": "military_naval", "metric": "count", "value": naval_count,
             "delta_label": f"vs {history[0]} earlier" if history else ""},
            {"layer": "dark_fleet", "metric": "nearby", "value": 1,
             "delta_label": "within 100km"},
            {"layer": "nav_warnings", "metric": "active_in_cell", "value": 1,
             "delta_label": "covers cell"},
        ],
    }


def _eval_airspace_shutdown_single(layers, cell, civ_count_baseline):
    """Signature 5: large NOTAM polygon over normally-busy airspace.

    v2: confidence via SIGMOID_WEIGHTS; logic otherwise unchanged.
    """
    notams = _items(layers.get("notams"))
    for n in notams:
        if _cell_of(n.get("lat"), n.get("lng")) != cell and not _notam_covers_cell([n], cell):
            continue
        area = _notam_area_km2(n)
        if area < LARGE_NOTAM_KM2:
            continue
        if civ_count_baseline < BUSY_AIRSPACE_FLIGHTS:
            continue
        confidence = _sigmoid_confidence("airspace_shutdown", {
            "notam_area_norm": area / LARGE_NOTAM_KM2,  # 1.0 = at threshold
            "busy_baseline_norm": civ_count_baseline / BUSY_AIRSPACE_FLIGHTS,
        })
        return {
            "signature": "airspace_shutdown",
            "confidence": round(confidence, 2),
            "title": "Major airspace closure",
            "summary": (f"NOTAM ~{int(area):,} km² over normally-busy airspace "
                        f"(baseline {civ_count_baseline:.0f} civilian flights)"),
            "signals": [
                {"layer": "notams", "metric": "area_km2", "value": int(area),
                 "delta_label": ""},
                {"layer": "flights", "metric": "civilian_baseline",
                 "value": int(civ_count_baseline), "delta_label": "1h"},
            ],
        }
    return None


def _eval_mass_cctv_blackout_single(cell, layers, cctv_dark_in_cell):
    """Signature 6: ≥10 CCTV cameras dark in 60 min, no weather cause.

    v2: confidence via SIGMOID_WEIGHTS; logic otherwise unchanged.
    """
    if cctv_dark_in_cell < CCTV_DARK_COUNT_SINGLE:
        return None
    if _weather_active_in_cell(layers, cell):
        return None
    confidence = _sigmoid_confidence("mass_cctv_blackout", {
        "dark_count": float(cctv_dark_in_cell),
        "no_weather": 1.0,
    })
    return {
        "signature": "mass_cctv_blackout",
        "confidence": round(confidence, 2),
        "title": "Mass camera outage",
        "summary": f"{cctv_dark_in_cell} CCTV cameras went silent in last 60 min",
        "signals": [
            {"layer": "cctv", "metric": "cameras_dark",
             "value": cctv_dark_in_cell, "delta_label": "in 60 min"},
        ],
    }


def _eval_internet_shutdown_single(layers):
    """Signature 7: government-directed / nationwide outage.

    Country-level — emits one insight PER affected country (using country
    centroid). Doesn't depend on cell history.

    v2: confidence still 0.85 — this is a deterministic indicator (the cause
    field is the source of truth, not noisy), so sigmoid weighting buys us
    nothing. Kept consistent with v1.
    """
    out = []
    seen = set()
    for o in _items(layers.get("internet_outages")):
        cause = (o.get("outage_cause") or "").upper()
        otype = (o.get("outage_type") or "").upper()
        if "GOVERNMENT" not in cause and "NATIONWIDE" not in otype:
            continue
        cc = o.get("country_code") or ""
        if cc in seen:
            continue
        seen.add(cc)
        lat, lng = o.get("lat"), o.get("lng")
        if not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
            continue
        confidence = _sigmoid_confidence("internet_shutdown", {"cause_strong": 1.0})
        out.append({
            "signature": "internet_shutdown",
            "lat": lat, "lng": lng,
            "country": cc,
            "confidence": round(confidence, 2),
            "title": f"Government-directed internet shutdown ({cc})",
            "summary": o.get("label") or "Nationwide / government-directed outage",
            "signals": [
                {"layer": "internet_outages", "metric": "cause",
                 "value": cause or otype or "?", "delta_label": ""},
            ],
        })
    return out


# ---------------------------------------------------------------------------
# Phase D — new signatures (Phase C data sources are stubbed when missing)
# ---------------------------------------------------------------------------
# When a Phase-C layer (gps_jam, bgp_events, etc.) hasn't shipped yet, each
# evaluator's pre-flight check on `layers.get(...)` returns None and the
# signature silently no-ops. No special feature-flag plumbing needed.

# Phase D adds 3 weights blocks to the SIGMOID_WEIGHTS table (registered
# lazily here to keep the constants section uncluttered).
SIGMOID_WEIGHTS.update({
    "gps_jam_surge": [
        # GPSJam interference level on Wiseman's 0-4 scale ("bad-pct quartile")
        {"key": "gps_intensity", "w": 0.55, "threshold": 3.0, "scale": 0.6},
        # corroborator: mil_air Z-rise in same cell
        {"key": "mil_air_z",     "w": 0.45, "threshold": 1.5, "scale": 0.5},
    ],
    "comms_blackout": [
        # BGP leak/hijack count for the country in last hour (1 = at threshold)
        {"key": "bgp_event_count", "w": 0.40, "threshold": 1.0, "scale": 0.5},
        # internet outage active in same country
        {"key": "outage_in_country", "w": 0.30, "threshold": 0.5, "scale": 0.2},
        # CCTV dark in any cell of that country (binary 0/1)
        {"key": "cctv_dark_in_country", "w": 0.30, "threshold": 0.5, "scale": 0.2},
    ],
    "logistics_surge": [
        # port congestion delta vs baseline (1.0 = at +30%)
        {"key": "port_congestion_delta", "w": 0.35, "threshold": 1.0, "scale": 0.5},
        # mil_naval Z-rise in same 5° region (1.0 = at +1σ)
        {"key": "mil_naval_z",  "w": 0.35, "threshold": 1.0, "scale": 0.5},
        # dark_fleet Z-rise in same 5° region (1.0 = at +1σ)
        {"key": "dark_fleet_z", "w": 0.30, "threshold": 1.0, "scale": 0.5},
    ],
})

# Severity / tier / color for the new signatures.
_SEVERITY.update({
    "gps_jam_surge":   "warning",
    "comms_blackout":  "critical",
    "logistics_surge": "warning",
})
_TIER.update({
    "gps_jam_surge":   "multi_signal",
    "comms_blackout":  "multi_signal",
    "logistics_surge": "multi_signal",
})
_COLOR.update({
    "gps_jam_surge":   "#00FFAA",  # cyan-green — EW
    "comms_blackout":  "#FF00FF",  # magenta — critical C4ISR shutdown
    "logistics_surge": "#FFB800",  # amber — slower-moving prep signal
})


def _eval_gps_jam_surge(cell, mil_count, layers, cs, now_dt):
    """Signature 8: GPSJam high interference + military_air Z-rise → EW prep.

    Phase D. Stubbed off until gps_jam layer ships in Phase C.
    """
    gps_payload = layers.get("gps_jam")
    if not gps_payload:
        return None
    # Find the cell's gps_jam intensity. Items are {lat, lng, intensity (0-4)}.
    intensity = 0
    for it in _items(gps_payload):
        if _cell_of(it.get("lat"), it.get("lng")) == cell:
            intensity = max(intensity, float(it.get("intensity") or 0))
    if intensity < 3.0:
        return None
    # Military air corroborator
    mil_z, _ = _zscore_for(cs, "mil_air_count", mil_count, now_dt)
    if mil_z is None or mil_z < Z_MIL_AIR_RISE:
        # Cold-start: percent-delta fallback
        _, mil_delta = _baseline(cs.get("mil_air_count", []), mil_count)
        if mil_delta is None or mil_delta < MIL_AIR_RISE_PCT:
            return None
        mil_indicator = min(mil_delta / 25.0, 4.0)
    else:
        mil_indicator = mil_z
    confidence = _sigmoid_confidence("gps_jam_surge", {
        "gps_intensity": intensity,
        "mil_air_z": mil_indicator,
    })
    return {
        "signature": "gps_jam_surge",
        "confidence": round(confidence, 2),
        "title": "GNSS jamming + military air surge",
        "summary": (f"GPSJam intensity {intensity:.0f}/4 in cell · "
                    f"military air {mil_indicator:.1f}σ above baseline — likely electronic warfare posture"),
        "signals": [
            {"layer": "gps_jam", "metric": "intensity", "value": int(intensity), "delta_label": "0-4 scale"},
            {"layer": "military_air", "metric": "z_rise", "value": round(mil_indicator, 2), "delta_label": "σ vs same-ToD"},
        ],
    }


def _eval_comms_blackout(country_code, layers, cctv_dark_cells_by_country):
    """Signature 9: BGP leak/hijack + internet outage + CCTV dark — same country.

    Phase D. Country-level (single result per affected country, like
    internet_shutdown). Lands when bgp_events layer ships in Phase C.
    """
    bgp_payload = layers.get("bgp_events")
    if not bgp_payload:
        return None
    bgp_count = 0
    sample = None
    for ev in _items(bgp_payload):
        if (ev.get("country_code") or "").upper() == country_code.upper():
            bgp_count += 1
            if sample is None:
                sample = ev
    if bgp_count < 1:
        return None
    # Outage in same country?
    outage_match = False
    outage_label = None
    for o in _items(layers.get("internet_outages")):
        if (o.get("country_code") or "").upper() == country_code.upper():
            outage_match = True
            outage_label = o.get("label")
            break
    cctv_dark = cctv_dark_cells_by_country.get(country_code.upper(), 0)
    confidence = _sigmoid_confidence("comms_blackout", {
        "bgp_event_count": float(bgp_count),
        "outage_in_country": 1.0 if outage_match else 0.0,
        "cctv_dark_in_country": 1.0 if cctv_dark > 0 else 0.0,
    })
    if confidence < 0.40:
        # neither corroborator present + only 1 BGP event ≈ noise floor
        return None
    sample_lat = (sample or {}).get("lat") or 0
    sample_lng = (sample or {}).get("lng") or 0
    bits = [f"{bgp_count} BGP route event(s)"]
    if outage_match: bits.append(f"internet outage active ({outage_label or 'unspecified'})")
    if cctv_dark: bits.append(f"{cctv_dark} CCTV cells dark in country")
    return {
        "signature": "comms_blackout",
        "lat": sample_lat, "lng": sample_lng,
        "country": country_code.upper(),
        "confidence": round(confidence, 2),
        "title": f"Coordinated C4ISR disruption ({country_code.upper()})",
        "summary": " · ".join(bits),
        "signals": [
            {"layer": "bgp_events", "metric": "events_1h", "value": bgp_count, "delta_label": ""},
            *([{"layer": "internet_outages", "metric": "active_country", "value": 1, "delta_label": ""}] if outage_match else []),
            *([{"layer": "cctv", "metric": "dark_cells", "value": cctv_dark, "delta_label": "in country"}] if cctv_dark else []),
        ],
    }


def _eval_logistics_surge(region_5deg, layers, region_state, now_dt):
    """Signature 10: port congestion + mil_naval Z-rise + dark_fleet Z-rise.

    Phase D. 5° regional rather than 1° per-cell — sealift prep manifests
    over larger areas. Uses existing data: port_congestion, military_naval,
    dark_fleet. No new Phase-C source needed.
    """
    # Port congestion in the 5° region
    port_payload = layers.get("port_congestion")
    if not port_payload:
        return None
    rl_min, rl_max, rg_min, rg_max = region_5deg
    region_ports = []
    for p in _items(port_payload):
        plat, plng = p.get("lat"), p.get("lng")
        if isinstance(plat, (int, float)) and isinstance(plng, (int, float)):
            if rl_min <= plat <= rl_max and rg_min <= plng <= rg_max:
                region_ports.append(p)
    if not region_ports:
        return None
    cur_congestion = sum(float(p.get("congestion_pct") or 0) for p in region_ports) / max(1, len(region_ports))
    prev_congestion = float((region_state.get("port_congestion") or [0])[-1]) if region_state.get("port_congestion") else cur_congestion
    delta = (cur_congestion - prev_congestion) / max(prev_congestion, 10.0)  # normalized delta
    if delta < 0.30:
        return None
    # mil_naval Z-rise across the region (sum of cells in region, vs baseline)
    naval_payload = layers.get("military_naval")
    region_naval = sum(1 for n in _items(naval_payload)
                       if isinstance(n.get("lat"), (int, float))
                       and rl_min <= n["lat"] <= rl_max
                       and rg_min <= n.get("lng", -999) <= rg_max)
    naval_z, _ = _zscore_for(region_state, "naval_count", region_naval, now_dt)
    if naval_z is None or naval_z < 1.0:
        return None
    # dark_fleet Z-rise
    dark_payload = layers.get("dark_fleet")
    region_dark = sum(1 for d in _items(dark_payload)
                      if isinstance(d.get("lat"), (int, float))
                      and rl_min <= d["lat"] <= rl_max
                      and rg_min <= d.get("lng", -999) <= rg_max)
    dark_z, _ = _zscore_for(region_state, "dark_fleet_count", region_dark, now_dt)
    if dark_z is None or dark_z < 1.0:
        return None
    confidence = _sigmoid_confidence("logistics_surge", {
        "port_congestion_delta": delta / 0.30,  # normalize so 0.30 -> 1.0
        "mil_naval_z": naval_z,
        "dark_fleet_z": dark_z,
    })
    return {
        "signature": "logistics_surge",
        "confidence": round(confidence, 2),
        "title": "Logistics/sealift surge pattern",
        "summary": (f"Port congestion +{int(delta*100)}% across region · "
                    f"naval {naval_z:.1f}σ + dark-fleet {dark_z:.1f}σ above baseline — "
                    f"possible forward-positioning"),
        "signals": [
            {"layer": "port_congestion", "metric": "delta_pct", "value": int(delta * 100), "delta_label": "vs prev"},
            {"layer": "military_naval", "metric": "z_rise", "value": round(naval_z, 2), "delta_label": "σ vs baseline"},
            {"layer": "dark_fleet", "metric": "z_rise", "value": round(dark_z, 2), "delta_label": "σ vs baseline"},
        ],
    }


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------
def _now_dt(now):
    return now if now is not None else datetime.now(timezone.utc)


def detect_kinetic_insights(layers, prev_layers, prev_state, prev_insights,
                            *, now=None):
    """Build the kinetic_insights payload + new state.

    Args:
        layers: {layer_id: payload} — the dict already in scope of
                _build_intel_cycle. Read-only.
        prev_layers: same shape, the PREVIOUS cycle's layers (for delta
                detection — flight count drop, CCTV gone dark). May be None
                on first cycle.
        prev_state: {"cells": {cell_tuple: cell_state_dict},
                     "cctv_last_seen": {camera_id: ts}}
                or None on first cycle.
        prev_insights: previous insights payload (for the 7-day rolling
                window) or None.
        now: datetime override (for unit tests).

    Returns:
        (payload_dict, new_state_dict)
    """
    now_dt = _now_dt(now)
    now_ts = int(now_dt.timestamp())
    state = {
        "cells": dict((prev_state or {}).get("cells", {})),
        "cctv_last_seen": dict((prev_state or {}).get("cctv_last_seen", {})),
    }

    # 1) Roll forward per-cell rolling histories
    civ_by_cell = _flights_by_cell(layers.get("flights"), category_filter="civilian")
    mil_by_cell = _flights_by_cell(layers.get("military_air"))
    naval_by_cell = _items_by_cell(layers.get("military_naval"))

    # Union of cells we have data for THIS cycle (so we know which cells to
    # update). Ocean cells with no observations get skipped — keeps state tight.
    active_cells = set(civ_by_cell) | set(mil_by_cell) | set(naval_by_cell)
    for cell in active_cells:
        cs = _ensure_cell(state, cell, now_ts)
        civ_c = len(civ_by_cell.get(cell, []))
        mil_c = len(mil_by_cell.get(cell, []))
        nav_c = len(naval_by_cell.get(cell, []))
        # Short rolling history (v1, retained for cold-start fallback in _baseline()).
        _push_history(cs["flights_count"], civ_c)
        _push_history(cs["mil_air_count"], mil_c)
        _push_history(cs["mil_naval_count"], nav_c)
        # v2: also push into the per-ToD Welford bucket for Z-score gating.
        _update_tod_bucket(cs, "flights_count", civ_c, now_dt)
        _update_tod_bucket(cs, "mil_air_count", mil_c, now_dt)
        _update_tod_bucket(cs, "mil_naval_count", nav_c, now_dt)

    # 2) CCTV gone-dark detection
    cctv_now = _cctv_now_seen(layers)
    # Update last-seen for every currently-healthy camera
    for cid, cell in cctv_now.items():
        state["cctv_last_seen"][cid] = now_ts
    # Compute cells with ≥N cameras whose last_seen is in the past CCTV_DARK_WINDOW_S
    # (i.e. healthy recently but missing now)
    silent_now: set = set(state["cctv_last_seen"].keys()) - set(cctv_now.keys())
    cctv_dark_count_by_cell: dict = {}
    # Need the per-camera CELL from PREVIOUS observation. We don't store it
    # in state today (would inflate); approximate by reading the PREVIOUS
    # cycle's CCTV blob if available.
    prev_cctv = _cctv_now_seen(prev_layers or {})
    for cid in silent_now:
        last_seen = state["cctv_last_seen"].get(cid, 0)
        if now_ts - last_seen > CCTV_DARK_WINDOW_S:
            # Too long silent — no longer a "just went dark" candidate.
            continue
        # Where was the camera before? Use prev_cctv mapping.
        cell = prev_cctv.get(cid)
        if cell is None:
            continue
        cctv_dark_count_by_cell[cell] = cctv_dark_count_by_cell.get(cell, 0) + 1

    # Garbage-collect cctv_last_seen entries older than 24h
    cutoff = now_ts - 24 * 3600
    state["cctv_last_seen"] = {cid: ts for cid, ts in state["cctv_last_seen"].items()
                                if ts >= cutoff}

    # 3) Evaluate signatures per active cell
    new_items = []
    # The set of cells to evaluate = active_cells ∪ cells with CCTV-dark
    eval_cells = active_cells | set(cctv_dark_count_by_cell.keys())
    for cell in eval_cells:
        cs = _ensure_cell(state, cell, now_ts)
        civ_count = len(civ_by_cell.get(cell, []))
        mil_count = len(mil_by_cell.get(cell, []))
        naval_count = len(naval_by_cell.get(cell, []))
        cctv_dark = cctv_dark_count_by_cell.get(cell, 0)

        signatures_hit = []

        # --- Multi-signal (v2: now_dt threaded through for Z-score gating) ---
        s1 = _eval_airspace_denial(cell, civ_count, mil_count, layers, cs, now_dt)
        if s1: signatures_hit.append(s1)
        s2 = _eval_surveillance_blackout(cell, now_ts, layers, state, cctv_now, cctv_dark, now_dt)
        if s2: signatures_hit.append(s2)
        s3 = _eval_naval_massing(cell, naval_count, layers, cs, now_dt)
        if s3: signatures_hit.append(s3)
        # Phase D: GPSJam + military air surge in same cell
        s8 = _eval_gps_jam_surge(cell, mil_count, layers, cs, now_dt)
        if s8: signatures_hit.append(s8)

        # --- Multi-domain composite (v2: weighted-domain scoring) ---
        # Build the per-domain score: each domain = max confidence among
        # signatures that lit in that domain this cycle.
        has_frontline = _frontline_covers_cell(layers.get("frontlines"), cell)
        domains_lit = {}
        for sh in signatures_hit:
            d = SIGNATURE_DOMAIN.get(sh["signature"])
            if d:
                domains_lit[d] = max(domains_lit.get(d, 0.0), sh["confidence"])
        # Frontline polygon coverage acts as an honorary ground-domain signal
        # (ground combat is happening here right now — fold it in if any sig hit).
        if has_frontline and signatures_hit:
            domains_lit["ground"] = max(domains_lit.get("ground", 0.0), 0.6)

        # Weighted average across LIT domains only — normalize by the sum of
        # weights for active domains so the score sits in [0, 1] regardless of
        # which subset fired. (Raw weighted-sum with weights summing to 1
        # could never exceed ~0.45 for 2-domain hits, defeating the gate.)
        if domains_lit:
            wsum = sum(DOMAIN_WEIGHT.get(d, 0.1) for d in domains_lit)
            weighted_score = sum(DOMAIN_WEIGHT.get(d, 0.1) * s for d, s in domains_lit.items()) / max(wsum, 1e-6)
        else:
            weighted_score = 0.0
        # Fire multi_domain if ≥2 domains lit AND weighted_score clears the gate.
        # Frontline + any single signature also qualifies (v1 compat — kinetic
        # activity inside an active frontline polygon is doctrinally meaningful
        # even at lower indicator density).
        fires_multi = (len(domains_lit) >= 2 and weighted_score >= MULTI_DOMAIN_THRESHOLD) \
                      or (has_frontline and len(signatures_hit) >= 1)
        if fires_multi:
            combined_signals = []
            combined_titles = []
            for sh in signatures_hit:
                combined_signals.extend(sh["signals"])
                combined_titles.append(sh["title"])
            if has_frontline:
                combined_titles.append("active frontline in cell")
                combined_signals.append({
                    "layer": "frontlines", "metric": "active_polygon",
                    "value": 1, "delta_label": "covers cell"
                })
            new_items.append(_finalize(cell, cs, now_dt, now_ts, {
                "signature": "multi_domain",
                "confidence": round(min(1.0, weighted_score + (0.15 if has_frontline else 0.0)), 2),
                "title": f"Multi-domain kinetic event likely ({', '.join(sorted(domains_lit))})",
                "summary": " + ".join(combined_titles),
                "signals": combined_signals,
            }))
        else:
            for sh in signatures_hit:
                new_items.append(_finalize(cell, cs, now_dt, now_ts, sh))

        # --- Single-signal (tier 2) — emitted only if no multi-signal fired
        # for this cell, so we don't double-count.
        if not signatures_hit:
            # use baseline civ as proxy for 'normally busy airspace'
            baseline_civ, _ = _baseline(cs["flights_count"], civ_count)
            if baseline_civ is not None:
                s5 = _eval_airspace_shutdown_single(layers, cell, baseline_civ)
                if s5: new_items.append(_finalize(cell, cs, now_dt, now_ts, s5))
            s6 = _eval_mass_cctv_blackout_single(cell, layers, cctv_dark)
            if s6: new_items.append(_finalize(cell, cs, now_dt, now_ts, s6))

    # Signature 7: internet shutdowns (country-level, not cell-based)
    for shutdown in _eval_internet_shutdown_single(layers):
        cell = _cell_of(shutdown["lat"], shutdown["lng"]) or (0, 0)
        cs = _ensure_cell(state, cell, now_ts)
        new_items.append(_finalize(cell, cs, now_dt, now_ts, shutdown))

    # Phase D — country-level comms_blackout (depends on bgp_events layer; no-op if missing)
    bgp_payload = layers.get("bgp_events")
    if bgp_payload:
        # Build {country: dark_cell_count} for cctv_dark_in_country indicator
        cctv_dark_by_cc: dict = {}
        for cell, cnt in cctv_dark_count_by_cell.items():
            # Look up any cctv item in this cell for its country_code
            for it in _items(layers.get("cctv") or {}):
                if _cell_of(it.get("lat"), it.get("lng")) == cell:
                    cc = (it.get("country_code") or "").upper()
                    if cc:
                        cctv_dark_by_cc[cc] = cctv_dark_by_cc.get(cc, 0) + cnt
                    break
        seen_cc = set()
        for ev in _items(bgp_payload):
            cc = (ev.get("country_code") or "").upper()
            if not cc or cc in seen_cc:
                continue
            seen_cc.add(cc)
            comms = _eval_comms_blackout(cc, layers, cctv_dark_by_cc)
            if comms:
                cell = _cell_of(comms["lat"], comms["lng"]) or (0, 0)
                cs = _ensure_cell(state, cell, now_ts)
                new_items.append(_finalize(cell, cs, now_dt, now_ts, comms))

    # Phase D — region-level logistics_surge (5° grid; uses existing data; no Phase C dep).
    # Maintain region state similarly to per-cell state, but at 5° resolution.
    if layers.get("port_congestion"):
        REGION_DEG = 5.0
        regions_state = state.setdefault("regions_5deg", {})
        port_payload = layers.get("port_congestion")
        # Discover regions with at least one port
        active_regions = set()
        for p in _items(port_payload):
            plat, plng = p.get("lat"), p.get("lng")
            if isinstance(plat, (int, float)) and isinstance(plng, (int, float)):
                rl = int(plat // REGION_DEG) * REGION_DEG
                rg = int(plng // REGION_DEG) * REGION_DEG
                active_regions.add((rl, rl + REGION_DEG, rg, rg + REGION_DEG))
        for region in active_regions:
            rkey = f"{region[0]:.0f}-{region[2]:.0f}"
            rstate = regions_state.setdefault(rkey, {
                "tod_buckets": {}, "last_signature_ts": {},
            })
            # Push current cycle's naval + dark_fleet counts into region's ToD buckets.
            rl_min, rl_max, rg_min, rg_max = region
            r_naval = sum(1 for n in _items(layers.get("military_naval"))
                          if isinstance(n.get("lat"), (int, float))
                          and rl_min <= n["lat"] <= rl_max
                          and rg_min <= n.get("lng", -999) <= rg_max)
            r_dark = sum(1 for d in _items(layers.get("dark_fleet"))
                         if isinstance(d.get("lat"), (int, float))
                         and rl_min <= d["lat"] <= rl_max
                         and rg_min <= d.get("lng", -999) <= rg_max)
            _update_tod_bucket(rstate, "naval_count", r_naval, now_dt)
            _update_tod_bucket(rstate, "dark_fleet_count", r_dark, now_dt)
            # Track recent congestion in a 4-sample list so delta is computable.
            pc_history = rstate.setdefault("port_congestion", [])
            cur_pc = sum(float(p.get("congestion_pct") or 0)
                         for p in _items(layers.get("port_congestion"))
                         if isinstance(p.get("lat"), (int, float))
                         and rl_min <= p["lat"] <= rl_max
                         and rg_min <= p.get("lng", -999) <= rg_max)
            n_in_region = max(1, sum(1 for p in _items(layers.get("port_congestion"))
                                     if isinstance(p.get("lat"), (int, float))
                                     and rl_min <= p["lat"] <= rl_max
                                     and rg_min <= p.get("lng", -999) <= rg_max))
            pc_history.append(cur_pc / n_in_region)
            if len(pc_history) > 4:
                pc_history.pop(0)
            ls = _eval_logistics_surge(region, layers, rstate, now_dt)
            if ls:
                # Anchor logistics insights at the region centroid cell.
                center_lat = (region[0] + region[1]) / 2
                center_lng = (region[2] + region[3]) / 2
                cell = _cell_of(center_lat, center_lng) or (0, 0)
                cs = _ensure_cell(state, cell, now_ts)
                ls["lat"] = center_lat
                ls["lng"] = center_lng
                new_items.append(_finalize(cell, cs, now_dt, now_ts, ls))

    # 4) Drop cooled-down items (v2 decay gate): is_new=False means the
    # same (cell, signature) fired recently and current evidence hasn't
    # materially strengthened beyond the decay deficit.
    new_items = [it for it in new_items if it.get("is_new")]

    # Merge with prior insights, respecting the 7-day rolling window + cap
    prev_items = []
    if isinstance(prev_insights, dict):
        prev_items = list(prev_insights.get("items") or [])
    # Drop prior items older than INSIGHTS_WINDOW
    cutoff_iso = (now_dt - INSIGHTS_WINDOW).isoformat()
    prev_items = [p for p in prev_items
                  if isinstance(p, dict) and (p.get("t") or "9") >= cutoff_iso]
    # Dedup: don't re-add an insight that fires in the SAME cell + signature
    # if we already have one in the recent items
    existing_keys = {(p.get("cell_key"), p.get("signature")) for p in prev_items}
    appended = 0
    for it in new_items:
        key = (it["cell_key"], it["signature"])
        if key in existing_keys:
            continue
        prev_items.append(it)
        existing_keys.add(key)
        appended += 1
    prev_items.sort(key=lambda p: p.get("t") or "", reverse=True)
    prev_items = prev_items[:INSIGHTS_MAX]

    # 5) Evict stale cells from state
    _evict_stale_cells(state, now_ts)

    payload = {
        "updatedAt": now_dt.isoformat(),
        "count": len(prev_items),
        "new": appended,
        "items": prev_items,
    }
    return payload, state


def _finalize(cell, cs, now_dt, now_ts, hit):
    """Apply cooldown decay, attach cell metadata, severity/color/tier.

    v2: replaces v1's binary 1h cooldown with exponential confidence decay.
    Effective confidence = raw - DECAY_PEAK * exp(-(now - last_fire) / DECAY_TAU_S).
    A previously-fired signature must clear EFFECTIVE_CONFIDENCE_GATE in raw
    minus the decay deficit before it re-emits. Result: rapid follow-up only
    when evidence has materially strengthened, not on flat re-triggers.
    """
    import math
    sig = hit["signature"]
    raw_conf = hit["confidence"]
    last = cs["last_signature_ts"].get(sig, 0)
    age = max(0, now_ts - last)
    decay_deficit = DECAY_PEAK * math.exp(-age / DECAY_TAU_S) if last > 0 else 0.0
    effective_conf = raw_conf - decay_deficit
    # First-time fire (last==0) bypasses decay; otherwise gate on effective conf.
    is_new = (last == 0) or (effective_conf >= EFFECTIVE_CONFIDENCE_GATE)
    if is_new:
        cs["last_signature_ts"][sig] = now_ts

    if "lat" in hit and "lng" in hit:
        lat, lng = hit["lat"], hit["lng"]
        bbox = None
    else:
        lat, lng = _cell_center(cell)
        lat_min, lat_max, lng_min, lng_max = _cell_box(cell)
        bbox = [lng_min, lat_min, lng_max, lat_max]

    return {
        "id": f"ki-{cell[0]}-{cell[1]}-{sig}-{now_ts}",
        "t": now_dt.isoformat(),
        "lat": lat, "lng": lng,
        "bbox": bbox,
        "cell_key": f"{cell[0]}-{cell[1]}",
        "signature": sig,
        "tier": _TIER.get(sig, "single_signal"),
        "severity": _SEVERITY.get(sig, "info"),
        "confidence": round(raw_conf, 2),
        "effective_confidence": round(max(0.0, effective_conf), 2),
        "title": hit["title"],
        "summary": hit["summary"],
        "signals": hit["signals"],
        "country": hit.get("country"),
        "category": "Kinetic Event",
        "color": _COLOR.get(sig, "#FF1744"),
        "is_new": is_new,
    }
