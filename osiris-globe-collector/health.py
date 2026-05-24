"""Feed-health classifier for the globe collector.

Pure-function (no network, clock injected via `now`) so it stays unit-testable
and mirrors intel.py's shape. The collector's `health_writer` task calls
`assess_health(_manifest)` every ~60s and PUTs the result to `globe/health.json`;
the FeedHealth chip in the dashboard StatusBar consumes that blob.

The goal is to surface SILENT failures — the kind of bug where an upstream
(say GDELT or OpenSky) starts returning 0 events for hours and the dashboard
shows nothing without anyone noticing. Per-layer expected minimums + a max-age
window (4× the layer's polling interval) catch both "stopped updating" and
"updates fine but with empty payloads".

Status taxonomy:
  healthy   count >= expected_min AND age <= max_age AND no error
  stale     age > max_age (regardless of count) — upstream stopped responding
  empty     count < expected_min AND no error    — silent-failure tell
  errored   manifest carries an `error` field
  silent    layer missing from manifest entirely  — task likely crashed

Overall:
  any down/errored -> "down" (red)
  any stale/empty  -> "degraded" (amber)
  else             -> "healthy" (green)
"""
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Per-layer baseline table.
#
# expected_min_count: hard floor below which we declare the feed "empty".
#                     Sized conservatively against typical observed counts so
#                     normal diurnal drops (e.g. military_air at 3am UTC)
#                     don't flap into "empty".
# max_age_seconds:    how stale before "stale" fires. Default = 4 * interval_s,
#                     which is generous enough to absorb one missed cycle and
#                     ProxyRack retry but tight enough to catch a real freeze.
# kind:               "live"   high-cadence positional feeds (flights, sats, boats)
#                     "slow"   daily / multi-hour refresh (cctv, infrastructure)
#                     "static" manual / fixed reference data (chokepoints, nuclear)
#                              static layers skip the staleness check entirely.
# ---------------------------------------------------------------------------
BASELINES: dict = {
    # --- live, high-cadence ----------------------------------------------------
    "flights":         {"expected_min_count": 5000,  "max_age_seconds": 60,    "kind": "live"},
    "military_air":    {"expected_min_count": 80,    "max_age_seconds": 60,    "kind": "live"},
    "satellites":      {"expected_min_count": 7000,  "max_age_seconds": 40,    "kind": "live"},
    "markets":         {"expected_min_count": 5,     "max_age_seconds": 120,   "kind": "live"},
    "boats":           {"expected_min_count": 15000, "max_age_seconds": 120,   "kind": "live"},
    "military_naval":  {"expected_min_count": 20,    "max_age_seconds": 1200,  "kind": "live"},
    # --- medium / hourly -------------------------------------------------------
    "earthquakes":     {"expected_min_count": 50,    "max_age_seconds": 240,   "kind": "live"},
    "news":            {"expected_min_count": 20,    "max_age_seconds": 1200,  "kind": "live"},
    "events":          {"expected_min_count": 50,    "max_age_seconds": 3600,  "kind": "live"},
    "natural-events":  {"expected_min_count": 10,    "max_age_seconds": 3600,  "kind": "live"},
    "wildfire":        {"expected_min_count": 100,   "max_age_seconds": 3600,  "kind": "live"},
    "cyber":           {"expected_min_count": 100,   "max_age_seconds": 7200,  "kind": "live"},
    "frontlines":      {"expected_min_count": 1,     "max_age_seconds": 7200,  "kind": "live"},
    "nav_warnings":    {"expected_min_count": 100,   "max_age_seconds": 14400, "kind": "live"},
    "tornado_warnings":{"expected_min_count": 0,     "max_age_seconds": 1200,  "kind": "live"},
    "hurricanes":      {"expected_min_count": 0,     "max_age_seconds": 7200,  "kind": "live"},
    "wind":            {"expected_min_count": 100,   "max_age_seconds": 7200,  "kind": "live"},
    "aqi":             {"expected_min_count": 100,   "max_age_seconds": 7200,  "kind": "live"},
    # --- slow / daily ----------------------------------------------------------
    "cctv":            {"expected_min_count": 200,   "max_age_seconds": 86400,  "kind": "slow"},
    "infrastructure":  {"expected_min_count": 500,   "max_age_seconds": 345600, "kind": "slow"},
    "military_bases":  {"expected_min_count": 5000,  "max_age_seconds": 345600, "kind": "slow"},
    "cell-towers":     {"expected_min_count": 1000,  "max_age_seconds": 2419200,"kind": "slow"},
    # --- conditionally-live (no-op when key missing / nothing to publish) ------
    # vessel_events: GFW — dark without GFW_API_KEY. Empty payload is normal.
    "vessel_events":   {"expected_min_count": 0,     "max_age_seconds": 3600,   "kind": "live"},
    # notams: ≥1 expected (there's always SOMETHING in the US TFR system).
    "notams":          {"expected_min_count": 1,     "max_age_seconds": 3600,   "kind": "live"},
    # --- static / manual reference (no staleness check) ------------------------
    "chokepoints":     {"expected_min_count": 24,    "max_age_seconds": None,   "kind": "static"},
    "nuclear":         {"expected_min_count": 30,    "max_age_seconds": None,   "kind": "static"},
}


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat()


def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _classify(entry, baseline, now):
    """Return (status, age_seconds_or_None) for one layer.

    `entry` is the manifest sub-dict for the layer (may be empty).
    `baseline` is the BASELINES row.
    """
    error = entry.get("error")
    count = int(entry.get("count") or 0)
    last = _parse_iso(entry.get("updatedAt"))
    age = (now - last).total_seconds() if last else None
    max_age = baseline.get("max_age_seconds")
    kind = baseline.get("kind", "live")

    if error:
        return ("errored", age)
    # Static reference data has no staleness — only the count floor matters.
    if kind == "static":
        if count < baseline["expected_min_count"]:
            return ("empty", age)
        return ("healthy", age)
    # Stale beats empty: if we can't even get a fresh update we should say so,
    # rather than complaining about a count that may simply be stale.
    if max_age is not None and (age is None or age > max_age):
        return ("stale", age)
    if count < baseline["expected_min_count"]:
        return ("empty", age)
    return ("healthy", age)


def _overall(layers):
    """green/amber/red roll-up over the per-layer statuses."""
    statuses = {l["status"] for l in layers}
    if "errored" in statuses or "down" in statuses:
        return "down"
    if statuses & {"stale", "empty", "silent"}:
        return "degraded"
    return "healthy"


def _summary(layers):
    counts = {}
    for l in layers:
        counts[l["status"]] = counts.get(l["status"], 0) + 1
    # Always lead with the dominant good number; then list each non-healthy
    # bucket in fixed order so the chip text is stable across refreshes.
    parts = [f"{counts.get('healthy', 0)} healthy"]
    for k in ("stale", "empty", "errored", "silent"):
        if counts.get(k):
            parts.append(f"{counts[k]} {k}")
    return ", ".join(parts)


def assess_health(manifest, *, now=None, baselines=None):
    """Classify every known layer's health from the live manifest blob.

    manifest:  {"layers": {layer_id: {updatedAt, count, error?}}} — exactly the
               shape collector.py's `manifest_writer` publishes.
    now:       inject for deterministic tests; defaults to UTC wall clock.
    baselines: override the BASELINES table (tests); defaults to module table.
    """
    now = now or _now()
    baselines = baselines if baselines is not None else BASELINES
    layers_in = (manifest or {}).get("layers", {}) or {}

    out_layers = []
    for lid, baseline in baselines.items():
        entry = layers_in.get(lid)
        if entry is None:
            # Layer registered in the baseline table but never wrote to the
            # manifest this run -> the per-layer task likely crashed.
            out_layers.append({
                "id": lid,
                "status": "silent",
                "count": 0,
                "expectedMinCount": baseline["expected_min_count"],
                "lastUpdate": None,
                "ageSeconds": None,
                "maxAgeSeconds": baseline.get("max_age_seconds"),
                "error": None,
                "kind": baseline.get("kind", "live"),
            })
            continue
        status, age = _classify(entry, baseline, now)
        out_layers.append({
            "id": lid,
            "status": status,
            "count": int(entry.get("count") or 0),
            "expectedMinCount": baseline["expected_min_count"],
            "lastUpdate": entry.get("updatedAt"),
            "ageSeconds": int(age) if age is not None else None,
            "maxAgeSeconds": baseline.get("max_age_seconds"),
            "error": entry.get("error"),
            "kind": baseline.get("kind", "live"),
        })

    # Stable order: errored/down first, then stale/empty/silent, then healthy.
    # Within each bucket, alphabetical by id so the chip drill-down doesn't
    # reshuffle on every refresh.
    rank = {"errored": 0, "down": 0, "stale": 1, "empty": 1, "silent": 1, "healthy": 2}
    out_layers.sort(key=lambda l: (rank.get(l["status"], 3), l["id"]))

    return {
        "updatedAt": _iso(now),
        "overall": _overall(out_layers),
        "summary": _summary(out_layers),
        "layers": out_layers,
    }
