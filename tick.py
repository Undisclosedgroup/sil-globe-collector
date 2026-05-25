"""One-shot tick wrapper for GitHub Actions.

The original collector.py is a long-running asyncio daemon — it loops
forever, refreshing each layer on its own interval. That model assumes
a persistent VM (Mac launchd / Fly.io machine).

GitHub Actions gives us 6-min-ish bursts every 5 minutes (the workflow
cron). So this entry point does ONE pass of every layer: fetch, normalize,
PUT to Vercel Blob, exit. The next cron run is the next "tick".

What runs:
- All `LAYERS` + `EXTRA_LAYERS` + `DERIVED_LAYERS` + env-gated optionals
  (viirs_vessels, iuu_incursions, bgp_events, gps_jam, eu_grid)
- Skips: boats (websocket daemon — needs persistent host)
- Skips: intel/forecast/predict/anomalies/corroborate/trails/narrative
  (these are analytical writers that depend on other layers being fresh
   in the same process — port separately if needed)

Failure isolation: every layer is wrapped in try/except so one bad
upstream never aborts the rest. A successful 0-item fetch does NOT
overwrite a previously-good blob (same safety rule as the daemon).
"""
import asyncio
import os
import sys
import time
import traceback

# layers.py expects ../_shared and ../flightradar24 on sys.path — both are
# siblings of the collector dir in the GH Actions workspace.
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "_shared"))
sys.path.insert(0, os.path.join(HERE, "flightradar24"))
sys.path.insert(0, os.path.join(HERE, "osiris-globe-collector"))

# Now everything imports as if we were inside the collector dir.
import layers  # noqa: E402
from blob_put import put_blob  # noqa: E402


def _collect_layer_specs():
    """Build the merged LAYERS list the same way collector.py does — but
    skip the daemon-only ones (boats websocket, analytical writers)."""
    specs = list(layers.LAYERS)
    for mod_name, attr in [
        ("extra_layers", "EXTRA_LAYERS"),
        ("derived_layers", "DERIVED_LAYERS"),
    ]:
        try:
            mod = __import__(mod_name)
            specs += getattr(mod, attr, [])
        except Exception as e:
            print(f"WARN: failed to import {mod_name}: {e}", flush=True)

    # Env-gated optionals (mirror collector.py).
    for mod_name, fn_name, lid, interval in [
        ("viirs_vbd", "fetch_viirs_vessels", "viirs_vessels", 21600),
        ("iuu_incursions", "fetch_iuu_incursions", "iuu_incursions", 300),
        ("cloudflare_bgp", "fetch_bgp_events", "bgp_events", 600),
        ("gpsjam", "fetch_gps_jam", "gps_jam", 3600),
        ("entsoe", "fetch_eu_grid", "eu_grid", 900),
    ]:
        try:
            mod = __import__(mod_name)
            fn = getattr(mod, fn_name)
            specs.append({"id": lid, "interval_s": interval, "fetch": fn})
        except Exception:
            pass

    # Drop layers that need a persistent host. boats has its own daemon-
    # only WebSocket loop (`run_boats_task`), not a normal fetch().
    return [s for s in specs if s.get("id") != "boats"]


_LAYER_TIMEOUT_DEFAULT = 120
_LAYER_TIMEOUT_OVERRIDES = {
    "cctv": 900,         # 17 sub-networks SEQUENTIAL × proxy rotation; ~13 min observed
    "power_plants": 700, # now uses ~1000 fine bboxes (was 27) to dodge Overpass throttle
    "hospitals": 700,    # 120k OSM elements — Overpass takes 60-180s server-side
}
_LAYER_SEM = asyncio.Semaphore(8)  # cap concurrent layers — GH runner + Webshare pool sanity


async def _tick_layer(spec):
    """Fetch one layer + write blob. Returns (id, count, error|None).

    Wrapped in a semaphore (max 8 concurrent layers) so we don't fork 45
    parallel proxy sessions and saturate the Webshare pool, and in a
    wait_for(_LAYER_TIMEOUT_S) so a single slow upstream can't blow the
    whole tick past the workflow timeout."""
    lid = spec["id"]
    fetch = spec["fetch"]
    t0 = time.monotonic()
    async with _LAYER_SEM:
        try:
            timeout = _LAYER_TIMEOUT_OVERRIDES.get(lid, _LAYER_TIMEOUT_DEFAULT)
            payload = await asyncio.wait_for(fetch(), timeout=timeout)
        except asyncio.TimeoutError:
            return lid, None, f"timeout after {timeout}s"
        except Exception as e:
            return lid, None, f"{type(e).__name__}: {e}"

    count = payload.get("count", 0) if isinstance(payload, dict) else 0
    # Same safety rule as the daemon: don't overwrite a previously-good
    # blob with an empty result (transient upstream failure).
    if count == 0:
        return lid, 0, "0 items — kept previous blob"

    try:
        put_blob(lid, payload)
    except Exception as e:
        return lid, count, f"blob PUT failed: {type(e).__name__}: {e}"

    dt = time.monotonic() - t0
    return lid, count, None


async def main():
    specs = _collect_layer_specs()
    print(f"=== globe collector tick: {len(specs)} layers ===", flush=True)
    # Run all layers concurrently — one bad layer never blocks the rest,
    # and total wall-clock stays under the GH Actions job timeout.
    results = await asyncio.gather(
        *(_tick_layer(s) for s in specs), return_exceptions=True
    )
    ok = err = empty = 0
    for r in results:
        if isinstance(r, BaseException):
            err += 1
            print(f"  EXC {r}", flush=True)
            continue
        lid, count, error = r
        if error and "0 items" in error:
            empty += 1
            print(f"  empty  {lid:24s} (kept prev)", flush=True)
        elif error:
            err += 1
            print(f"  FAIL   {lid:24s} {error}", flush=True)
        else:
            ok += 1
            print(f"  OK     {lid:24s} count={count}", flush=True)
    print(f"=== tick done: {ok} ok, {empty} empty, {err} failed ===", flush=True)
    # Exit 0 even on partial failures — partial freshness is better than
    # marking the whole workflow run as failed and getting an alert email.
    sys.exit(0)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
