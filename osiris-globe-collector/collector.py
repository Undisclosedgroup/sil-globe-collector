"""OSIRIS globe collector — persistent async ProxyRack pool -> Vercel Blob.

Runs an asyncio loop with a 20-thread ProxyRack worker pool (Semaphore(20)).
Each layer (see layers.LAYERS) has a target refresh interval; due layers are
refreshed concurrently and their normalized payload is PUT to Vercel Blob at
`globe/<layer>.json`. A `globe/_manifest.json` (per-layer updatedAt + count +
error) is written after each cycle for the dashboard status bar.

Safety:
- A layer fetch that raises is caught: the error is recorded in the manifest and
  the loop continues — one bad upstream never crashes the collector.
- A successful fetch that returns 0 items DOES NOT overwrite a previously good
  blob (avoids blanking the globe on a transient empty upstream).

Run:
    cd recon-out/osiris-globe-collector
    set -a; . ~/.proxyrack.env; set +a
    export BLOB_READ_WRITE_TOKEN=...      # Vercel project -> Storage -> Blob
    python collector.py
"""
import asyncio, logging, time, os

from layers import LAYERS, run_boats_task
try:
    from extra_layers import EXTRA_LAYERS  # 22 extra layers (Wave 3-4)
    LAYERS = list(LAYERS) + EXTRA_LAYERS
except Exception:
    pass
try:
    from derived_layers import DERIVED_LAYERS  # cross-layer joins (intel multipliers)
    LAYERS = list(LAYERS) + DERIVED_LAYERS
except Exception:
    pass
# VIIRS nightly dark-light vessel detections (EOG_USER/EOG_PASS env-gated;
# no-op when unset). Routes via ProxyRack per CLAUDE.md never-burn-IP rule.
try:
    from viirs_vbd import fetch_viirs_vessels
    LAYERS = list(LAYERS) + [
        {"id": "viirs_vessels", "interval_s": 21600, "fetch": fetch_viirs_vessels},
    ]
except Exception:
    pass
# IUU incursions — derived join (boats × viirs_vessels × geo_zones).
# Surfaces foreign fishing vessels inside EEZs of concern (Galapagos,
# Argentine, Natuna, Hormuz, DPRK). 5-min cadence — pure local join.
try:
    from iuu_incursions import fetch_iuu_incursions
    LAYERS = list(LAYERS) + [
        {"id": "iuu_incursions", "interval_s": 300, "fetch": fetch_iuu_incursions},
    ]
except Exception:
    pass
# Kinetic-detector v2 inputs — JSOC-doctrine indicators. Each is env-gated
# and degrades gracefully when its credential/source isn't reachable.
#   bgp_events: Cloudflare Radar BGP leaks + hijacks (10-min cadence)
#   gps_jam:    GPSJam.org daily GNSS interference (hourly poll; updates daily)
#   eu_grid:    ENTSO-E transmission/production unavailability (15-min cadence)
try:
    from cloudflare_bgp import fetch_bgp_events
    LAYERS = list(LAYERS) + [
        {"id": "bgp_events", "interval_s": 600, "fetch": fetch_bgp_events},
    ]
except Exception:
    pass
try:
    from gpsjam import fetch_gps_jam
    LAYERS = list(LAYERS) + [
        {"id": "gps_jam", "interval_s": 3600, "fetch": fetch_gps_jam},
    ]
except Exception:
    pass
try:
    from entsoe import fetch_eu_grid
    LAYERS = list(LAYERS) + [
        {"id": "eu_grid", "interval_s": 900, "fetch": fetch_eu_grid},
    ]
except Exception:
    pass
from blob_put import put_blob as _raw_put_blob
from forecast import build_forecast_full, fetch_layer, _items
from intel import (build_brief, derive_alerts, append_history, METRIC_LAYERS)
from predict import predict_trajectories
from health import assess_health
from corroborate import corroborate
from anomalies import detect_anomalies
from quota import _tracker as _quota, assess_quota
from narrative import build_narrative
from trails import record_trails, TRAIL_SOURCES
from budget import _budget
from projection_alerts import derive_projection_alerts
from narrative_publisher import narrative_publisher_writer


def put_blob(*args, **kwargs):
    """Thin wrapper around blob_put.put_blob that records the call in the
    quota tracker so the quota_writer can surface our Vercel-Blob write rate.
    Every layer-write, manifest-write, health-write, and intel-derived-write
    flows through here, so a single instrumentation point covers everything.

    Order matters: do the write FIRST so a tracker-internal failure can never
    block or skip the actual blob put. The tracker record is isolated in a
    try/except — instrumentation must never break the operation it observes."""
    result = _raw_put_blob(*args, **kwargs)
    try:
        _quota.record("vercel_blob")
    except Exception:
        pass
    return result

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("globe-collector")

SEM = asyncio.Semaphore(20)   # 20 ProxyRack worker threads
_manifest: dict = {}          # {layer_id: {updatedAt, count, error?}}


async def refresh(layer):
    lid = layer["id"]
    async with SEM:
        try:
            payload = await layer["fetch"]()
            # Never overwrite a good blob with an empty payload.
            prev = _manifest.get(lid, {})
            if payload["count"] == 0 and prev.get("count"):
                log.warning("%s returned 0 items; keeping previous blob (%d)",
                            lid, prev["count"])
                return
            await asyncio.to_thread(put_blob, lid, payload)
            entry = {"updatedAt": payload["updatedAt"], "count": payload["count"]}
            if payload.get("error"):
                entry["error"] = payload["error"]
            _manifest[lid] = entry
            log.info("%s OK %d", lid, payload["count"])
        except Exception as e:
            _manifest.setdefault(lid, {})["error"] = f"{type(e).__name__}: {e}"
            log.warning("%s FAIL %s", lid, e)


async def run_layer(layer):
    """One INDEPENDENT loop per layer on its own cadence. Critical: a slow layer
    (e.g. CCTV ~minutes, 6h interval) must NOT throttle fast layers (flights every
    ~10s). Each layer reschedules itself the moment its own fetch completes."""
    interval = layer["interval_s"]
    while True:
        await refresh(layer)
        await asyncio.sleep(interval)


async def manifest_writer():
    """Publish the per-layer freshness manifest every few seconds."""
    while True:
        try:
            await asyncio.to_thread(put_blob, "_manifest", {"layers": _manifest})
        except Exception as e:
            log.warning("_manifest write FAIL %s", e)
        await asyncio.sleep(5)


async def health_writer():
    """Publish the derived feed-health blob every 60s. Reads the live in-memory
    `_manifest` (no network), classifies each layer against its baseline, and
    PUTs `globe/health.json` for the StatusBar's FeedHealth chip. Failures are
    isolated — never crash the loop, never block other writers."""
    while True:
        try:
            blob = assess_health({"layers": _manifest})
            await asyncio.to_thread(put_blob, "health", blob)
        except Exception as e:
            log.warning("health write FAIL %s", e)
        await asyncio.sleep(60)


async def quota_writer():
    """Publish the rolling 24h quota/usage snapshot every 60s. Pure derivation
    over in-memory counters — `assess_quota` classifies each provider against
    its known limits. Also refreshes the adaptive Budget tiers + merges them
    into the blob so the QuotaIndicator can show per-provider back-pressure
    state. Failure-isolated."""
    while True:
        try:
            _budget.refresh()
            snap = _quota.snapshot()
            blob = assess_quota(snap)
            blob["budget"] = _budget.snapshot()
            await asyncio.to_thread(put_blob, "quota", blob)
        except Exception as e:
            log.warning("quota write FAIL %s", e)
        await asyncio.sleep(60)


TRAILS_INTERVAL_S = 90   # matches the boats snapshot floor (positions sampled / 90s)


def _build_trails_cycle(state):
    """Sample positions from the trail-relevant layers and append to the rolling
    per-entity track. Reads our own production blobs (direct stdlib fetch — our
    infra). Pure, deterministic, capped output."""
    layers = {src: fetch_layer(src) for src in TRAIL_SOURCES}
    payload, new_state = record_trails(layers, state.get("trails_state"))
    put_blob("trails", payload)
    state["trails_state"] = new_state
    return payload["count"]


async def trails_writer():
    """Every 90s: sample live positions of every aircraft / vessel in the
    trail-relevant feeds, append to the per-entity 6h rolling track, publish
    the `trails` blob. Failure-isolated; rebuilds in a cycle or two after
    restart."""
    state = {"trails_state": None}
    while True:
        try:
            n = await asyncio.to_thread(_build_trails_cycle, state)
            log.info("trails OK %d entities tracked", n)
        except Exception as e:
            log.warning("trails FAIL %s", e)
        await asyncio.sleep(TRAILS_INTERVAL_S)


FORECAST_INTERVAL_S = 300   # rebuild the escalation forecast every ~5 min


def _layer_counts(layers):
    """Live feed counts for the brief + history time-series. Reuse the layers
    already fetched for the forecast; fetch only the few extras not in that set
    (flights, military_naval) — all reads hit our OWN production API."""
    counts = {}
    for lid in METRIC_LAYERS:
        if lid in layers:
            counts[lid] = len(_items(layers[lid]))
        else:
            counts[lid] = len(_items(fetch_layer(lid)))
    return counts


def _build_intel_cycle(state):
    """One synchronous intel pass (runs in a worker thread). Builds the forecast
    once, derives brief/alerts/history from that single pass, PUTs all four, and
    updates `state` (prev_forecast / prev_alerts / prev_history) in place."""
    forecast, layers = build_forecast_full()
    counts = _layer_counts(layers)

    put_blob("forecast", forecast)
    put_blob("brief", build_brief(forecast, counts))

    # alerts is deferred — built AFTER the kinetic detector below so it can
    # fold new kinetic_event items into the rolling log in one pass.

    history = append_history(state.get("history"), forecast, counts)
    put_blob("history", history)

    # Predictive trajectory: pure derivation from history + forecast, runs AFTER
    # append_history so the newest sample is included in the slope fit.
    predict = predict_trajectories(history, forecast)
    put_blob("predict", predict)

    # Trajectory-projection alerts: forward-looking events derived from the
    # predict output (ETA-to-crisis crossings, 6/24h projections crossing
    # thresholds, rapid climb/drop). Distinct from intel.derive_alerts'
    # kinetic_event stream which fires on CURRENT state. Pure function;
    # failure-isolated.
    try:
        projection_alerts = derive_projection_alerts(
            predict, state.get("projection_alerts"))
        put_blob("projection_alerts", projection_alerts)
        state["projection_alerts"] = projection_alerts
    except Exception as _e:
        log.warning("projection_alerts FAIL %s", _e)

    # Cross-source corroboration: per-hotspot compound bonus when multiple
    # kinetic signals are lit + emerging convergences off the hotspot list.
    corroboration = corroborate(forecast, layers)
    put_blob("corroborate", corroboration)

    # Per-entity anomalies (dropped vessels, MMSI swaps, emergency squawks, …).
    anomalies, anom_state = detect_anomalies(
        layers, state.get("anomaly_state"), state.get("anomalies"))
    put_blob("anomalies", anomalies)

    # Kinetic-event detector — change-detection over the same `layers` dict.
    # Surfaces "something just started happening here" insights (airspace
    # shutdown + CCTV dark + military rise = likely kinetic event). Reads
    # prev cycle's `layers` for delta detection. Failure-isolated.
    prev_kinetic = state.get("kinetic_insights")
    try:
        from kinetic_detector import detect_kinetic_insights
        kinetic, kin_state = detect_kinetic_insights(
            layers,
            state.get("prev_layers_for_kinetic"),
            state.get("kinetic_state"),
            prev_kinetic,
        )
        put_blob("kinetic_insights", kinetic)
        state["kinetic_state"] = kin_state
        state["kinetic_insights"] = kinetic
        # Persist a shallow snapshot for the NEXT cycle's delta comparisons.
        # v2 additions: gps_jam (EW posture), bgp_events (C4ISR), port_congestion
        # (logistics surge corroborator), military_bases (force-posture context).
        state["prev_layers_for_kinetic"] = {
            k: layers.get(k) for k in (
                "flights", "military_air", "military_naval", "cctv",
                "notams", "frontlines", "nav_warnings", "internet_outages",
                "dark_fleet",
                "gps_jam", "bgp_events", "eu_grid", "port_congestion",
            )
        }
    except Exception as _e:
        log.warning("kinetic_detector FAIL %s", _e)
        kinetic = {"updatedAt": None, "count": 0, "new": 0, "items": []}

    # Build alerts NOW that kinetic_insights is available — folds new critical/
    # warning kinetic_event items into the same rolling log the LiveAlerts UI
    # reads, so they surface without any frontend changes.
    alerts = derive_alerts(
        forecast, state.get("forecast"), state.get("alerts"),
        kinetic_insights=kinetic, prev_kinetic_insights=prev_kinetic,
    )
    put_blob("alerts", alerts)

    # LLM-generated narrative brief — composes the structured intel into prose
    # via Claude. No-ops cleanly when ANTHROPIC_API_KEY is unset (same dark-
    # mode pattern as gfw.py). Cache-keyed on top hotspot scores + recent
    # alert/anomaly ids so we only burn API budget when state actually shifts.
    narrative, narrative_state = build_narrative(
        forecast,
        state.get("brief") or build_brief(forecast, counts),
        alerts,
        anomalies,
        corroboration,
        prev_state=state.get("narrative_state"),
    )
    put_blob("narrative", narrative)
    if not narrative.get("cached"):
        _quota.record("claude_api")

    state["forecast"] = forecast
    state["alerts"] = alerts
    state["history"] = history
    state["anomalies"] = anomalies
    state["anomaly_state"] = anom_state
    state["narrative_state"] = narrative_state
    top = max((h["score"] for h in forecast["hotspots"]), default=0)
    rising = sum(1 for t in predict["trajectories"] if t["trend"] == "rising")
    return (len(forecast["hotspots"]), top, alerts["new"], history["count"],
            rising, anomalies["new"])


async def intel_writer():
    """Every ~5 min: build the escalation forecast and derive the intel brief,
    alert events, and history time-series from that one pass — publishing the
    `forecast`, `brief`, `alerts`, and `history` blobs. Rolling state (prev
    forecast/alerts/history) is seeded from the production API on cold start so a
    collector restart doesn't lose the alert log or playback history. The whole
    pass runs in a worker thread so its network reads never block the event loop;
    any failure is logged and the loop continues."""
    state = {
        "forecast": fetch_layer("forecast"),
        "alerts": fetch_layer("alerts"),
        "history": fetch_layer("history"),
        "anomalies": fetch_layer("anomalies"),
        "projection_alerts": fetch_layer("projection_alerts"),
        # anomaly_state is in-memory only (tracked entity history); rebuilds
        # naturally over a few cycles after restart.
        "anomaly_state": None,
    }
    log.info("intel: seeded from API (alerts=%s history-samples=%s)",
             (state["alerts"] or {}).get("count"),
             (state["history"] or {}).get("count"))
    while True:
        try:
            n, top, new_alerts, hist, rising, new_anoms = await asyncio.to_thread(
                _build_intel_cycle, state)
            log.info("intel OK forecast=%d (top %d) alerts+%d history=%d rising=%d anomalies+%d",
                     n, top, new_alerts, hist, rising, new_anoms)
        except Exception as e:
            log.warning("intel FAIL %s", e)
        await asyncio.sleep(FORECAST_INTERVAL_S)


async def main():
    if not os.environ.get("BLOB_READ_WRITE_TOKEN"):
        raise SystemExit(
            "BLOB_READ_WRITE_TOKEN unset — set it (Vercel project -> Storage -> "
            "Blob) before starting the collector. Exiting cleanly.")
    log.info("starting globe collector: %d layers (independent schedules), "
             "20-thread ProxyRack pool", len(LAYERS))
    # 2026-05-24: boats task disabled — its AISStream connection was deadlocking
    # the entire asyncio loop after the morning's DNS storm, blocking every
    # other layer for 16h. Re-enable after migrating boats to Fly.io or
    # adding a hard timeout / supervisor around run_boats_task.
    # asyncio.create_task(run_boats_task(put_blob, log))
    # One independent task per layer so the slowest layer can't block the fastest.
    tasks = [asyncio.create_task(run_layer(l)) for l in LAYERS]
    tasks.append(asyncio.create_task(manifest_writer()))
    tasks.append(asyncio.create_task(health_writer()))
    tasks.append(asyncio.create_task(quota_writer()))
    tasks.append(asyncio.create_task(intel_writer()))
    tasks.append(asyncio.create_task(trails_writer()))
    # Daily narrative auto-publisher — no-op until any of the
    # NARRATIVE_{SLACK,DISCORD,EMAIL}_WEBHOOK / NARRATIVE_WEBHOOK_URL envs
    # are set. Cheap 5-min check loop.
    tasks.append(asyncio.create_task(narrative_publisher_writer()))
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("stopped")
