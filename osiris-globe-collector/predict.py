"""Trajectory predictor — turns the rolling 72h history + current forecast into a
forward-looking per-hotspot read of "where is this heading?"

Pure heuristic, deterministic, stdlib-only. No network, no clock except the
explicit `now` arg (so it stays unit-testable like intel.py and forecast.py).

The collector publishes the result as the `predict` blob:

    predict_trajectories(history, forecast, now=None) -> payload dict

Shape (see Output payload contract in the spec):

    {
      "updatedAt": iso,
      "generatedFrom": forecast.updatedAt,
      "windowSampledHours": int,
      "trajectories": [
        {
          "id", "name", "lat", "lng",
          "currentScore", "currentLevel",
          "slope",                       # points / hour (signed)
          "trend",                       # rising | falling | flat | volatile
          "projection_6h", "projection_24h",
          "projectedLevel_6h", "projectedLevel_24h",
          "eta_to_crisis",               # hours, or null
          "eta_to_routine",              # hours, or null
          "confidence",                  # 0..1
          "summary",                     # one-line plain English
        },
        ...
      ]
    }

Heuristic notes
---------------
- Slope: ordinary-least-squares linear regression on the last ~12h of samples,
  with a single-pass outlier trim (drop the worst-fit point if we have >=5
  samples and that point sits >2x the mean residual). One or two samples ->
  slope=0 / trend=flat / confidence near 0.
- Trend: |slope| < 0.3 pts/hr -> flat; slope > 0 -> rising; slope < 0 -> falling.
  "volatile" overrides when residual stddev is high relative to the score range.
- Projection: current + slope * h, clamped to [0,100]. We deliberately do NOT
  smooth with a logistic — the slope is already taken from a smoothed regression
  and a hard clamp is what consumers want for "if this continues" reads.
- ETA: linear extrapolation against the 75 / 25 thresholds, capped at 72h.
- Confidence: blend of sample-count adequacy, regression R^2, and recency of the
  last sample (samples >2h old start to decay).
"""
from datetime import datetime, timezone, timedelta

from forecast import _level

# Regression window. We keep the last ~12h because the forecast cadence is 5 min,
# so a healthy window gives us ~140 samples — plenty for a stable fit, recent
# enough to react when the situation changes within a shift.
REGRESSION_WINDOW_HOURS = 12.0

# Trend thresholds.
FLAT_SLOPE = 0.3            # |slope| below this is "flat" (pts / hour)
VOLATILE_RES_RATIO = 0.35   # residual_stddev / max(1, score_span) > this -> volatile
VOLATILE_MIN_SAMPLES = 5    # need a few samples before we'll call it volatile

# ETA caps + thresholds (mirror forecast.py band edges).
CRISIS_THRESHOLD = 75
ROUTINE_THRESHOLD = 25
MAX_ETA_HOURS = 72.0

# Confidence weights (sum 1.0).
CONF_W_SAMPLES = 0.4        # do we have enough samples?
CONF_W_R2 = 0.4             # how well does the line actually fit?
CONF_W_RECENCY = 0.2        # is the latest sample fresh?

# Sample-density target. ~12 samples in the regression window (1/hr) is "enough".
CONF_SAMPLES_FULL = 12


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat()


def _parse_t(s):
    """Parse an ISO timestamp from a history sample. Returns None on garbage."""
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _regress(points):
    """Ordinary least squares on [(x, y), ...]. Returns (slope, intercept, r2,
    residual_stddev). Returns (0.0, mean_y, 0.0, 0.0) when degenerate (<2 points
    or zero x-variance)."""
    n = len(points)
    if n < 2:
        y = points[0][1] if n == 1 else 0.0
        return 0.0, float(y), 0.0, 0.0
    sum_x = sum(p[0] for p in points)
    sum_y = sum(p[1] for p in points)
    mean_x = sum_x / n
    mean_y = sum_y / n
    num = 0.0
    den = 0.0
    for x, y in points:
        dx = x - mean_x
        num += dx * (y - mean_y)
        den += dx * dx
    if den == 0:
        return 0.0, float(mean_y), 0.0, 0.0
    slope = num / den
    intercept = mean_y - slope * mean_x
    # R^2 and residual stddev
    ss_tot = sum((y - mean_y) ** 2 for _, y in points)
    residuals = [y - (slope * x + intercept) for x, y in points]
    ss_res = sum(r * r for r in residuals)
    r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0
    # variance of residuals
    res_var = ss_res / n
    res_std = res_var ** 0.5
    return slope, intercept, _clamp(r2, 0.0, 1.0), res_std


def _regress_trimmed(points):
    """Like _regress but with a single-pass worst-residual trim when we have
    enough samples. Resists 1-2 spikes without hiding a real direction change."""
    slope, intercept, r2, res_std = _regress(points)
    if len(points) >= 5 and res_std > 0:
        residuals = [(abs(y - (slope * x + intercept)), i)
                     for i, (x, y) in enumerate(points)]
        worst_abs, worst_i = max(residuals)
        # Mean of |residual| across the other points
        others_mean = (sum(r for r, _ in residuals) - worst_abs) / (len(points) - 1)
        if others_mean > 0 and worst_abs > 2.0 * others_mean:
            trimmed = [p for j, p in enumerate(points) if j != worst_i]
            return _regress(trimmed)
    return slope, intercept, r2, res_std


def _series_for_hotspot(samples, hid, now, window_hours):
    """Extract [(hours_before_now_as_x, score), ...] for one hotspot, restricted
    to the regression window. `x` is hours since the window start so slope is
    naturally pts/hour and positive x means newer."""
    cutoff = now - timedelta(hours=window_hours)
    pts = []
    for s in samples:
        t = _parse_t(s.get("t"))
        if t is None or t < cutoff:
            continue
        score = s.get("scores", {}).get(hid)
        if score is None:
            continue
        # x = hours since window start (older samples get smaller x).
        x = (t - cutoff).total_seconds() / 3600.0
        pts.append((x, float(score)))
    return pts


def _trend_label(slope, points, r2):
    """rising / falling / flat / volatile from slope + sample variance + r2.

    Volatility wins over slope when the data has wide dispersion AND the linear
    fit explains very little of the variance (low R^2). A clean rise/fall has a
    high R^2 even if it's steep; a whipping series has near-zero R^2 even if a
    naive slope falls out of the regression."""
    if len(points) >= VOLATILE_MIN_SAMPLES:
        ys = [y for _, y in points]
        span = max(ys) - min(ys)
        if span >= 15:
            mean = sum(ys) / len(ys)
            var = sum((y - mean) ** 2 for y in ys) / len(ys)
            std = var ** 0.5
            ratio = std / max(1.0, span)
            # Two volatility signatures, either qualifies:
            #   (a) wide dispersion + the line explains <20% of the variance, OR
            #   (b) wide dispersion + slope can't reach the dispersion mid-window.
            if ratio > VOLATILE_RES_RATIO and r2 < 0.2:
                return "volatile"
            if ratio > VOLATILE_RES_RATIO and abs(slope) < 1.5:
                return "volatile"
    if abs(slope) < FLAT_SLOPE:
        return "flat"
    return "rising" if slope > 0 else "falling"


def _project(current, slope, hours):
    return int(round(_clamp(current + slope * hours, 0, 100)))


def _eta(current, slope, threshold):
    """Hours until `current` crosses `threshold` at the given slope, capped at
    MAX_ETA_HOURS. Returns None when the slope doesn't move us toward threshold."""
    if slope == 0:
        return None
    delta = threshold - current
    if (delta > 0 and slope <= 0) or (delta < 0 and slope >= 0):
        return None
    if delta == 0:
        return 0.0
    hours = delta / slope
    if hours <= 0:
        return None
    return round(min(hours, MAX_ETA_HOURS), 2)


def _confidence(points, r2, last_sample_age_hours):
    """0..1 blend of sample adequacy, fit quality, and recency."""
    n = len(points)
    if n < 2:
        # one sample = we have a level but no direction; near-zero confidence
        return 0.05 if n == 1 else 0.0
    sample_score = _clamp(n / CONF_SAMPLES_FULL, 0.0, 1.0)
    fit_score = _clamp(r2, 0.0, 1.0)
    # Recency: full credit for <=15 min old, linear decay to 0 by 6h old.
    if last_sample_age_hours <= 0.25:
        recency = 1.0
    elif last_sample_age_hours >= 6.0:
        recency = 0.0
    else:
        recency = 1.0 - (last_sample_age_hours - 0.25) / (6.0 - 0.25)
    raw = (CONF_W_SAMPLES * sample_score
           + CONF_W_R2 * fit_score
           + CONF_W_RECENCY * recency)
    return round(_clamp(raw, 0.0, 1.0), 3)


def _fmt_eta(hours):
    """Compact human-friendly hour string for the summary line."""
    if hours is None:
        return None
    if hours < 1:
        mins = max(1, int(round(hours * 60)))
        return f"~{mins}m"
    if hours < 10:
        return f"~{hours:.1f}h"
    return f"~{int(round(hours))}h"


def _summary(name, current, current_level, slope, trend, proj_6h, eta_crisis,
             eta_routine):
    """One-line plain English read."""
    # Cold-start / sample-starved case
    if trend == "flat" and slope == 0 and current is None:
        return f"{name}: no recent samples — direction unknown."
    if trend == "flat":
        return (f"{name} plateaued at {current} ({current_level}); "
                f"no meaningful movement.")
    if trend == "volatile":
        return (f"{name} volatile around {current} ({current_level}); "
                f"direction unclear, projects {proj_6h} in 6h.")
    direction = "ascending" if trend == "rising" else "falling"
    sharp = " sharply" if abs(slope) >= 2.0 else ""
    parts = [
        f"{name} {direction}{sharp} (slope {slope:+.1f}/hr)",
        f"projects {proj_6h} in 6h",
    ]
    if trend == "rising" and eta_crisis is not None:
        parts.append(f"crisis ETA {_fmt_eta(eta_crisis)}")
    elif trend == "falling" and eta_routine is not None:
        parts.append(f"routine ETA {_fmt_eta(eta_routine)}")
    return "; ".join(parts) + "."


def _trajectory_for(hotspot, samples, now):
    """Compute one trajectory entry. `hotspot` is the forecast hotspot dict."""
    hid = hotspot["id"]
    name = hotspot["name"]
    current = int(hotspot.get("score", 0))
    current_level = hotspot.get("level") or _level(current)

    pts = _series_for_hotspot(samples, hid, now, REGRESSION_WINDOW_HOURS)

    # Compute slope + r2, with outlier trimming. Cold-start guard for <2 pts.
    if len(pts) < 2:
        slope = 0.0
        r2 = 0.0
        last_age_h = 999.0
    else:
        slope, _intercept, r2, _res_std = _regress_trimmed(pts)
        # `newest_x` is "hours since cutoff" for the most-recent sample, where
        # cutoff = now - REGRESSION_WINDOW_HOURS. The age of that sample
        # relative to `now` is simply (REGRESSION_WINDOW_HOURS - newest_x).
        # (The earlier formulation cancelled to zero — recency was always 1.0,
        #  silently defeating the staleness decay in _confidence.)
        newest_x = max(p[0] for p in pts)
        last_age_h = max(0.0, REGRESSION_WINDOW_HOURS - newest_x)

    trend = _trend_label(slope, pts, r2)
    # Flatten near-zero slopes to clean 0.0 so consumers don't see noise like 1e-15.
    if abs(slope) < 1e-6:
        slope = 0.0

    projection_6h = _project(current, slope, 6)
    projection_24h = _project(current, slope, 24)

    eta_crisis = (_eta(current, slope, CRISIS_THRESHOLD)
                  if trend == "rising" and current < CRISIS_THRESHOLD else None)
    eta_routine = (_eta(current, slope, ROUTINE_THRESHOLD)
                   if trend == "falling" and current > ROUTINE_THRESHOLD else None)

    confidence = _confidence(pts, r2, last_age_h)

    summary = _summary(name, current, current_level, slope, trend,
                       projection_6h, eta_crisis, eta_routine)

    return {
        "id": hid,
        "name": name,
        "lat": hotspot.get("lat"),
        "lng": hotspot.get("lng"),
        "currentScore": current,
        "currentLevel": current_level,
        "slope": round(float(slope), 3),
        "trend": trend,
        "projection_6h": projection_6h,
        "projection_24h": projection_24h,
        "projectedLevel_6h": _level(projection_6h),
        "projectedLevel_24h": _level(projection_24h),
        "eta_to_crisis": eta_crisis,
        "eta_to_routine": eta_routine,
        "confidence": confidence,
        "summary": summary,
    }


def predict_trajectories(history, forecast, *, now=None):
    """Pure: derive a per-hotspot forward-looking trajectory from the rolling
    history time-series + the current forecast.

    history:  the `history` blob (see intel.append_history). May be None / empty
              on cold start; we still emit safe-default entries for every hotspot
              in the forecast.
    forecast: the `forecast` blob (see forecast.build_forecast).
    now:      optional override for testability."""
    now = now or _now()

    samples = []
    window_h = 0
    if isinstance(history, dict):
        raw = history.get("samples")
        if isinstance(raw, list):
            samples = raw
        # how much history was actually available (for the consumer)
        window_h = int(history.get("windowHours") or 0)

    hotspots = (forecast or {}).get("hotspots", []) if isinstance(forecast, dict) else []

    # Compute the actual span of samples we have on hand, in hours.
    if samples:
        ts = [t for t in (_parse_t(s.get("t")) for s in samples) if t is not None]
        if ts:
            span = (max(ts) - min(ts)).total_seconds() / 3600.0
            # Report the smaller of (real span, declared window) so the consumer
            # knows how much data backed this trajectory.
            window_h = int(round(min(window_h or span, span)))

    trajectories = [_trajectory_for(h, samples, now) for h in hotspots]

    return {
        "updatedAt": _iso(now),
        "generatedFrom": (forecast or {}).get("updatedAt") if isinstance(forecast, dict) else None,
        "windowSampledHours": window_h,
        "trajectories": trajectories,
    }
