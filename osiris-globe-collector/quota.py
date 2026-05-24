"""Usage / cost quota tracker for the OSIRIS globe collector.

Mirrors health.py in shape: in-memory state lives in the running collector
process; the collector's `quota_writer` task periodically calls
`assess_quota(_tracker.snapshot())` and PUTs the resulting payload to
`globe/quota.json` for the dashboard's QuotaIndicator chip.

What this module does:
- Counts every outbound dependency the collector touches (per provider,
  per kind) on a rolling 24-hour window, with cap-per-provider so a runaway
  loop can't blow up memory.
- Surfaces per-provider rate-derived metrics (calls_24h / calls_1h /
  rate per minute) plus optional `pct_used` against a best-effort known
  limit table.
- Classifies each provider as ok / warning / critical / anomalous so the UI
  can pre-empt a quota cliff before something tips over.

This module deliberately does NOT instrument the collector itself — the
parent agent pastes in one-line `_tracker.record("provider")` hooks at the
right call sites (see this file's bottom-of-file comment for the checklist).
That avoids merge conflicts with the concurrent session editing layers.py,
gfw.py, notams.py, blob_put.py.

Pure-function `assess_quota(snapshot, *, now=None)` is unit-testable in
isolation; the live `Tracker` is a singleton importable as
`from quota import _tracker, assess_quota`.
"""
from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# Per-provider known-limit table.
#
# Most public-data upstreams (USGS, NASA, NOAA, NGA, NIFC, FAA TFR) publish
# no per-application rate limit — record them but leave `limit_per_day=None`
# so `pct_used` stays `null` in the snapshot (the UI renders an em-dash).
#
# Vercel Blob is the one that matters: Hobby plan caps `put` calls at ~1000/
# day. The collector PUTs every layer blob + manifest_writer every 5s +
# health_writer every 60s + intel_writer every 5 min — easy to drift past
# Hobby silently. Default the limit to 1000 here; users on Pro can override
# via env (VERCEL_BLOB_DAILY_LIMIT).
#
# AISStream / GFW free-tier limits aren't publicly documented — we record
# them anyway so the operator can spot anomalous rate jumps.
# ---------------------------------------------------------------------------
import os


def _env_int(name: str, default: int | None) -> int | None:
    v = os.environ.get(name)
    if not v:
        return default
    try:
        return int(v)
    except ValueError:
        return default


KNOWN_LIMITS: dict[str, dict[str, Any]] = {
    # Vercel Blob — Hobby default; bump via env on Pro/Enterprise.
    "vercel_blob": {"limit_per_day": _env_int("VERCEL_BLOB_DAILY_LIMIT", 1000)},
    # Free public APIs — no documented per-app limit, but tracked so anomaly
    # detection can fire if our own bug doubles the call rate.
    "gfw":            {"limit_per_day": None},
    "aisstream":      {"limit_per_day": None},
    "noaa":           {"limit_per_day": None},
    "usgs":           {"limit_per_day": None},
    "nga":            {"limit_per_day": None},
    "nasa_eonet":     {"limit_per_day": None},
    "nifc":           {"limit_per_day": None},
    "nasa_firms":     {"limit_per_day": None},
    "cisa_kev":       {"limit_per_day": None},
    "deepstatemap":   {"limit_per_day": None},
    "faa_tfr":        {"limit_per_day": None},
    "yahoo_finance":  {"limit_per_day": None},
    "stooq":          {"limit_per_day": None},
    "open_meteo":     {"limit_per_day": None},
    "overpass":       {"limit_per_day": None},
    "fr24":           {"limit_per_day": None},
    "adsb_lol":       {"limit_per_day": None},
    "adsb_fi":        {"limit_per_day": None},
    "airplanes_live": {"limit_per_day": None},
    "opensky":        {"limit_per_day": None},
    "celestrak":      {"limit_per_day": None},
    "cisa":           {"limit_per_day": None},
    # ProxyRack: unmetered residential per ~/.proxyrack.env CLAUDE.md note.
    # We still track thread-seconds so a thread-leak shows up as anomalous.
    "proxyrack":      {"limit_per_day": None},
}


# ---------------------------------------------------------------------------
# Thresholds for status derivation.
# ---------------------------------------------------------------------------
PCT_CRITICAL = 0.9
PCT_WARNING = 0.7
RATE_JUMP_FACTOR = 10.0     # 10× rate in the latest hour vs prior 23h average
RATE_JUMP_MIN_RECENT = 20   # need ≥20 events in the recent hour to bother
                            # (otherwise random fluctuation is noise)

# Per-provider cap on the in-memory event deque. A noisy provider can never
# pin more than ~10k entries; older events are evicted FIFO before the rolling
# window prune even runs. Keeps memory bounded under all failure modes.
DEFAULT_MAXLEN = 10_000

WINDOW_24H_S = 86_400
WINDOW_1H_S = 3_600


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class Tracker:
    """Thread-safe in-memory counter for outbound dependency usage.

    Use the module-level singleton `_tracker` from the collector loop; this
    class is only instantiated directly in tests (so each test gets a clean
    instance without touching global state)."""

    def __init__(self, maxlen: int = DEFAULT_MAXLEN) -> None:
        self._maxlen = maxlen
        # provider -> deque[(epoch_seconds, kind, n)]
        self._events: dict[str, deque[tuple[float, str, int]]] = {}
        self._lock = threading.Lock()

    def record(self, provider: str, *, kind: str = "api_call", n: int = 1,
               now: datetime | None = None) -> None:
        """Record `n` events of `kind` against `provider`. Cheap, lock-guarded.

        Designed to be called from any thread (the collector runs blob PUTs
        in `asyncio.to_thread`, and gfw/notams fetchers run synchronously
        in worker threads), so the lock is non-negotiable.
        """
        if not provider:
            return
        ts = (now or _now()).timestamp()
        with self._lock:
            d = self._events.get(provider)
            if d is None:
                d = deque(maxlen=self._maxlen)
                self._events[provider] = d
            d.append((ts, kind, int(n)))

    def snapshot(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Prune entries older than 24h and return the current usage snapshot.

        Shape (consumed by `assess_quota`):
            {
              "updatedAt": "...",
              "providers": {
                "gfw": {
                   "calls_24h": 138,
                   "calls_1h": 12,
                   "by_kind_24h": {"api_call": 138},
                   "last_call": "...",
                   "limit_per_day": null,
                   "pct_used": null,
                },
                ...
              },
              "totals": { "outbound_calls_24h": ..., "blob_writes_24h": ... },
            }
        """
        now_dt = now or _now()
        now_ts = now_dt.timestamp()
        cutoff_24h = now_ts - WINDOW_24H_S
        cutoff_1h = now_ts - WINDOW_1H_S

        providers: dict[str, Any] = {}
        total_outbound = 0
        total_blob_writes = 0

        with self._lock:
            for provider, d in self._events.items():
                # Prune-on-snapshot: drop anything older than 24h. Cheap because
                # the deque is ordered by append time.
                while d and d[0][0] < cutoff_24h:
                    d.popleft()

                calls_24h = 0
                calls_1h = 0
                by_kind_24h: dict[str, int] = {}
                last_ts: float | None = None
                for ts, kind, n in d:
                    calls_24h += n
                    if ts >= cutoff_1h:
                        calls_1h += n
                    by_kind_24h[kind] = by_kind_24h.get(kind, 0) + n
                    last_ts = ts  # deque is in append order, last wins

                limit = (KNOWN_LIMITS.get(provider) or {}).get("limit_per_day")
                pct_used = (calls_24h / limit) if limit else None

                row: dict[str, Any] = {
                    "calls_24h": calls_24h,
                    "calls_1h": calls_1h,
                    "by_kind_24h": by_kind_24h,
                    "limit_per_day": limit,
                    "pct_used": pct_used,
                    "last_call": _iso(
                        datetime.fromtimestamp(last_ts, tz=timezone.utc)
                    ) if last_ts is not None else None,
                }
                providers[provider] = row

                total_outbound += calls_24h
                if provider == "vercel_blob":
                    total_blob_writes = calls_24h

        # Include known providers with zero activity so the UI can show
        # "this upstream hasn't been hit yet today" rather than dropping
        # the row entirely. Keeps the list stable across collector restarts.
        for provider, meta in KNOWN_LIMITS.items():
            if provider in providers:
                continue
            providers[provider] = {
                "calls_24h": 0,
                "calls_1h": 0,
                "by_kind_24h": {},
                "limit_per_day": meta.get("limit_per_day"),
                "pct_used": 0.0 if meta.get("limit_per_day") else None,
                "last_call": None,
            }

        return {
            "updatedAt": _iso(now_dt),
            "providers": providers,
            "totals": {
                "outbound_calls_24h": total_outbound,
                "blob_writes_24h": total_blob_writes,
            },
        }


# Module-level singleton — `from quota import _tracker` and call
# `_tracker.record("provider")` from any instrumentation site.
_tracker = Tracker()


# ---------------------------------------------------------------------------
# assess_quota — pure function from snapshot -> classified payload.
# ---------------------------------------------------------------------------
def _provider_status(row: dict[str, Any]) -> str:
    """Classify one provider row.

    Priority order (most-severe first):
      critical    pct_used >= 0.9
      warning     pct_used >= 0.7
      anomalous   recent-hour rate >= 10× prior-23h average (and >=20 events)
      ok          otherwise
    """
    pct = row.get("pct_used")
    if pct is not None:
        if pct >= PCT_CRITICAL:
            return "critical"
        if pct >= PCT_WARNING:
            return "warning"

    # Anomaly detection: compare the most-recent hour rate against the prior
    # 23h average. We need both windows to have data, otherwise a single first
    # batch of events would always look "anomalous" vs the empty prior window.
    calls_1h = int(row.get("calls_1h") or 0)
    calls_24h = int(row.get("calls_24h") or 0)
    prior_23h = calls_24h - calls_1h
    if calls_1h >= RATE_JUMP_MIN_RECENT and prior_23h > 0:
        prior_per_hour = prior_23h / 23.0
        if prior_per_hour > 0 and calls_1h >= RATE_JUMP_FACTOR * prior_per_hour:
            return "anomalous"

    return "ok"


def _overall(provider_payloads: dict[str, dict[str, Any]]) -> str:
    """Roll up per-provider statuses into a single chip color.

    critical    > warning > anomalous > ok
    """
    statuses = {p["status"] for p in provider_payloads.values()}
    if "critical" in statuses:
        return "critical"
    if "warning" in statuses:
        return "warning"
    if "anomalous" in statuses:
        return "anomalous"
    return "ok"


def assess_quota(snapshot: dict[str, Any], *,
                 now: datetime | None = None) -> dict[str, Any]:
    """Classify a snapshot from `Tracker.snapshot()`.

    Returns the blob payload published to `globe/quota.json`:
        {
          "updatedAt": "...",
          "overall": "ok" | "warning" | "critical" | "anomalous",
          "summary": "1 critical, 2 warning, 17 ok",
          "providers": [
            {
               "id": "vercel_blob",
               "status": "critical",
               "calls_24h": 1100,
               "calls_1h": 80,
               "limit_per_day": 1000,
               "pct_used": 1.1,
               "last_call": "...",
               "by_kind_24h": {...},
            }, ...
          ],
          "totals": {...},
          "alerts": [
            {"provider": "vercel_blob", "status": "critical",
             "message": "Vercel Blob writes 1100/1000 (110%) in last 24h"},
            ...
          ],
        }
    """
    _ = now or _now()  # accept but don't need it (snapshot already carries time)
    providers_in = (snapshot or {}).get("providers", {}) or {}

    classified: dict[str, dict[str, Any]] = {}
    for pid, row in providers_in.items():
        status = _provider_status(row)
        classified[pid] = {
            "id": pid,
            "status": status,
            "calls_24h": int(row.get("calls_24h") or 0),
            "calls_1h": int(row.get("calls_1h") or 0),
            "by_kind_24h": dict(row.get("by_kind_24h") or {}),
            "limit_per_day": row.get("limit_per_day"),
            "pct_used": row.get("pct_used"),
            "last_call": row.get("last_call"),
        }

    alerts = _build_alerts(classified)

    # Stable sort: most-severe first, then alphabetically. Mirrors health.py.
    rank = {"critical": 0, "warning": 1, "anomalous": 2, "ok": 3}
    rows = sorted(classified.values(),
                  key=lambda r: (rank.get(r["status"], 9), r["id"]))

    return {
        "updatedAt": snapshot.get("updatedAt"),
        "overall": _overall(classified),
        "summary": _summary(rows),
        "providers": rows,
        "totals": (snapshot or {}).get("totals", {}),
        "alerts": alerts,
    }


def _build_alerts(classified: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """One-line human-readable alert per non-ok provider."""
    out: list[dict[str, Any]] = []
    for p in classified.values():
        if p["status"] == "ok":
            continue
        msg = _alert_message(p)
        out.append({
            "provider": p["id"],
            "status": p["status"],
            "calls_24h": p["calls_24h"],
            "limit_per_day": p["limit_per_day"],
            "pct_used": p["pct_used"],
            "message": msg,
        })
    # Most-severe first.
    rank = {"critical": 0, "warning": 1, "anomalous": 2}
    out.sort(key=lambda a: (rank.get(a["status"], 9), a["provider"]))
    return out


def _alert_message(p: dict[str, Any]) -> str:
    pid = p["id"]
    if p["status"] in {"critical", "warning"} and p["limit_per_day"]:
        pct = (p["pct_used"] or 0) * 100
        return (f"{pid} writes {p['calls_24h']}/{p['limit_per_day']} "
                f"({pct:.0f}%) in last 24h")
    if p["status"] == "anomalous":
        return (f"{pid} rate spike: {p['calls_1h']} calls in last 1h "
                f"vs {p['calls_24h'] - p['calls_1h']} in prior 23h")
    return f"{pid} status {p['status']}"


def _summary(rows: list[dict[str, Any]]) -> str:
    """Compact rollup like '1 critical, 2 warning, 17 ok' for the chip."""
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    parts: list[str] = []
    for k in ("critical", "warning", "anomalous", "ok"):
        if counts.get(k):
            parts.append(f"{counts[k]} {k}")
    return ", ".join(parts) or "0 tracked"


# ---------------------------------------------------------------------------
# Instrumentation checklist for the parent agent.
#
# The parent should paste these one-liners into the existing files. Each hook
# is idempotent and free of side-effects beyond the in-memory counter:
#
#   collector.py (top of file):
#       from quota import _tracker, assess_quota
#
#   blob_put.py / collector.py — wrap every put_blob site:
#       _tracker.record("vercel_blob")
#       put_blob(...)
#
#   gfw.py — inside _get (after urlopen succeeds):
#       _tracker.record("gfw")
#
#   notams.py — inside _get_json (after urlopen succeeds):
#       _tracker.record("faa_tfr")
#
#   layers.py — at the top of each fetch_*() (or after the first _aget call):
#       _tracker.record("usgs")           # fetch_earthquakes
#       _tracker.record("celestrak")      # fetch_satellites (per group)
#       _tracker.record("yahoo_finance")  # _market_yahoo
#       _tracker.record("stooq")          # _market_stooq
#       _tracker.record("nasa_eonet")     # fetch_natural_events
#       _tracker.record("nifc")           # fetch_wildfire
#       _tracker.record("cisa_kev")       # fetch_cyber
#       _tracker.record("deepstatemap")   # fetch_frontlines
#       _tracker.record("adsb_lol")       # fetch_flights base 0 + mil_url 0
#       _tracker.record("adsb_fi")        # fetch_flights base 1 + mil_url 1
#       _tracker.record("opensky")        # _fetch_opensky_states
#       _tracker.record("fr24")           # fetch_flights FR24 path
#       _tracker.record("overpass")       # fetch_infrastructure etc.
#       _tracker.record("open_meteo")     # fetch_wind / fetch_aqi
#       _tracker.record("noaa")           # fetch_tornado_warnings / hurricanes
#       _tracker.record("nga")            # fetch_nav_warnings
#       _tracker.record("aisstream", kind="message", n=batch_size)
#                                          # inside run_boats_task per WS frame
#
#   collector.py — add a writer task next to health_writer():
#       async def quota_writer():
#           while True:
#               try:
#                   blob = assess_quota(_tracker.snapshot())
#                   await asyncio.to_thread(put_blob, "quota", blob)
#                   _tracker.record("vercel_blob")  # count our own write
#               except Exception as e:
#                   log.warning("quota write FAIL %s", e)
#               await asyncio.sleep(60)
#
#       # and inside main():
#       tasks.append(asyncio.create_task(quota_writer()))
#
# Route: add "quota" to the ALLOWED set in
#   src/app/api/globe/[layer]/route.ts
# ---------------------------------------------------------------------------
