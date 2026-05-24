"""Boats AISStream daemon — long-running WebSocket listener for GH Actions.

Connects to AISStream's public WebSocket, accumulates per-MMSI vessel state
in memory, writes a snapshot blob every 30s, runs for ~5.5 hours, then
exits cleanly. The workflow that hosts this script (`boats.yml`) chains
itself so coverage is ~continuous with ~10-min gaps between job restarts.

This was previously a daemon-only task inside the Mac collector. AISStream
is WebSocket-only — there's no REST snapshot, so we need a persistent host
for it. GH Actions on a 6-hour job ceiling gives us "free, durable enough"
without needing Fly.io / Railway / a $5 droplet.

Failure isolation: on any unhandled exception or AISStream drop, the
existing run_boats_task's reconnect loop handles recovery. We only let
control return when the watchdog timer fires at ~5.5h.
"""
import asyncio
import logging
import os
import sys
import time

# layers.py expects ../_shared and ../flightradar24 on sys.path — same as tick.py.
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "_shared"))
sys.path.insert(0, os.path.join(HERE, "flightradar24"))
sys.path.insert(0, os.path.join(HERE, "osiris-globe-collector"))

from blob_put import put_blob as _put_blob  # noqa: E402
from layers import run_boats_task  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("boats-daemon")

# Watchdog: exit cleanly after 5h 50m so the workflow's 6-hour job ceiling
# never kills us mid-blob-write. The workflow chains itself so the next
# job picks up within ~30s.
WATCHDOG_S = 5 * 3600 + 50 * 60


async def _watchdog(task: asyncio.Task):
    """Sleep until the budget elapses, then cancel the boats task so we
    return control to main() and exit 0. Boats task's `finally:` cleans
    up the snapshotter sub-task."""
    await asyncio.sleep(WATCHDOG_S)
    log.info("boats: watchdog tripped after %ds — exiting cleanly", WATCHDOG_S)
    task.cancel()


async def main():
    if not os.environ.get("AISSTREAM_API_KEY"):
        log.error("boats: AISSTREAM_API_KEY not set — nothing to do")
        sys.exit(2)
    if not os.environ.get("BLOB_READ_WRITE_TOKEN"):
        log.error("boats: BLOB_READ_WRITE_TOKEN not set — nothing to write")
        sys.exit(2)

    t0 = time.monotonic()
    boats_task = asyncio.create_task(run_boats_task(_put_blob, log))
    watch_task = asyncio.create_task(_watchdog(boats_task))
    try:
        await boats_task
    except asyncio.CancelledError:
        log.info("boats: cancelled by watchdog after %.0fs", time.monotonic() - t0)
    finally:
        watch_task.cancel()
    log.info("boats: exit 0 — workflow will chain to next run")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
