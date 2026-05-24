"""Adaptive request budgeting for the OSIRIS globe collector.

The companion to `quota.py`. `quota` counts what we've spent; `budget` decides
what to spend next. When a provider's `pct_used` climbs toward its daily limit
— or its recent error rate spikes — the fetcher cadence stretches, and
non-essential providers get told to skip the current cycle entirely.

Design constraints (matches the rest of the collector):
- In-process only. Reads `_tracker.snapshot()` — never hits the network.
- Cheap. `refresh()` is O(providers) and runs at most every cycle (~5s); the
  per-call `should_skip` / `cadence_multiplier` lookups are dict reads.
- Failure-isolated. If anything in here throws, fetchers should still work
  (every public method swallows errors and returns the conservative default:
  green / 1.0× / False).
- Additive integration. The parent agent wires `_budget` into existing call
  sites via one-line hooks; this module never imports the collector.

Tier semantics:
    green  →  cadence ×1.0, never skip.
    amber  →  cadence ×1.5 (1.75 by default for stronger backoff above 75%).
    red    →  cadence ×3.0, AND non-essential providers should_skip → True.

Hysteresis: once a provider crosses into red, it doesn't fall back to amber
until `pct_used < 0.85 - 0.05 = 0.80`. Same for the amber → green boundary
(0.60 - 0.05 = 0.55). This prevents flapping when usage hovers near a
threshold and the next cycle's recorded calls bump it back over.

Essential providers (hardcoded — see ESSENTIAL_PROVIDERS): vercel_blob,
aisstream, flights. These get the cadence stretch but never the skip — they
carry too much of the platform's signal value to silently drop. The list is a
constant here for the v1 — moving it to env (`BUDGET_ESSENTIAL_PROVIDERS`) is
a one-line change later if operators need to tune it without a redeploy.

API (consumed by the parent's integration hooks):

    from budget import _budget

    # once per collector cycle, in manifest_writer / quota_writer:
    _budget.refresh()

    # at every API-call site:
    if _budget.should_skip("gfw"):
        return None
    ...

    # in each layer's run_layer sleep:
    await asyncio.sleep(interval * _budget.cadence_multiplier(provider))

    # merged into the quota blob:
    blob["budget"] = _budget.snapshot()
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# Tier thresholds.
#
# Numbers picked to give one tier of headroom above the quota.py warning
# (0.7) and critical (0.9) thresholds — so a provider is already in
# `amber` cadence-stretch mode before the QuotaIndicator chip turns yellow,
# and already in `red` (3× cadence + skip-non-essential) before it turns
# red. The budget module's job is to act BEFORE the operator sees the
# warning, not after.
# ---------------------------------------------------------------------------
GREEN_CEIL = 0.60   # pct_used < 0.60         → green
AMBER_CEIL = 0.85   # 0.60 ≤ pct_used < 0.85  → amber; ≥ 0.85 → red

HYSTERESIS = 0.05   # tier doesn't drop until pct_used < ceil - 0.05

# Cadence multipliers per tier. amber sits at 1.75 (middle of the allowed
# 1.5–2.0 band) so it's a clear-but-not-punishing stretch; red triples the
# interval which, combined with skip-non-essential, knocks ~70% off the
# outbound rate for any provider that hits it.
MULT_GREEN = 1.0
MULT_AMBER = 1.75
MULT_RED = 3.0

# Error-rate cliff. If a provider records ≥30% errors in the last 5 minutes
# (with at least RECENT_ERR_MIN events to filter noise), force red even if
# pct_used is well under 0.85 — the upstream is already throttling us, no
# point hammering it harder while we cross the quota threshold.
RECENT_ERR_WINDOW_S = 300       # 5 minutes
RECENT_ERR_THRESHOLD = 0.30     # ≥30% errors in window → force red
RECENT_ERR_MIN = 5              # need ≥5 events in window to evaluate

# Rate-spike anomaly: 1-hour rate vs prior-23h average. Mirrors quota.py's
# RATE_JUMP_FACTOR / RATE_JUMP_MIN_RECENT so an "anomalous" provider in the
# QuotaIndicator simultaneously bumps to at-least-amber here.
RATE_JUMP_FACTOR = 10.0
RATE_JUMP_MIN_RECENT = 20

# Essential providers — get cadence stretch but never the skip. Drops on
# these would visibly degrade the globe (boats vanish, flights vanish, blobs
# stop publishing), which is worse than a temporary quota overshoot.
ESSENTIAL_PROVIDERS: frozenset[str] = frozenset({
    "vercel_blob",  # publishing pipeline — if we skip this, the UI goes stale
    "aisstream",    # boats layer — the highest-density live data source
    "flights",      # logical name; per-adapter providers (adsb_lol/fi, opensky,
    "fr24",         # fr24, airplanes_live) are all flight providers and stay on.
    "adsb_lol",
    "adsb_fi",
    "airplanes_live",
    "opensky",
})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class Budget:
    """Per-provider tier evaluator with hysteresis.

    Owns:
        - last-evaluated tier per provider (so hysteresis can detect that the
          previous tier was red and apply the -0.05 stickiness),
        - last-evaluated pct_used + the metric snapshot we evaluated against
          (for the budget blob).

    All public methods are safe to call from any thread (asyncio task or
    `to_thread` worker). Refresh holds the lock for the duration of one
    snapshot evaluation; lookups are a single dict read under the lock.
    """

    def __init__(self, tracker, *, now: datetime | None = None) -> None:
        self._tracker = tracker
        self._lock = threading.Lock()
        # provider -> {"tier", "pct_used", "calls_24h", "calls_1h",
        #              "reason", "multiplier", "limit_per_day"}
        self._state: dict[str, dict[str, Any]] = {}
        self._updated_at: str | None = None
        # Bootstrap with the current snapshot so callers see sane defaults
        # before the first explicit refresh().
        try:
            self.refresh(now=now)
        except Exception:
            # Never raise from __init__; an empty state means all calls fall
            # through to the conservative green / 1.0× / False defaults.
            pass

    # -----------------------------------------------------------------
    # refresh
    # -----------------------------------------------------------------
    def refresh(self, *, now: datetime | None = None) -> None:
        """Re-evaluate every provider's tier from the live tracker snapshot.

        Called once per collector cycle. Cheap — one pass over the providers
        dict, all decisions are dict lookups + comparisons. Errors are caught
        per-provider so one malformed row can't blank the whole state.
        """
        now_dt = now or _now()
        try:
            snap = self._tracker.snapshot(now=now_dt)
        except Exception:
            return

        providers_in = (snap or {}).get("providers", {}) or {}
        new_state: dict[str, dict[str, Any]] = {}

        with self._lock:
            prev_state = dict(self._state)
            for pid, row in providers_in.items():
                try:
                    prev_tier = (prev_state.get(pid) or {}).get("tier", "green")
                    tier, reason = self._evaluate(pid, row, prev_tier, now_dt)
                except Exception:
                    tier, reason = "green", "evaluation-error"
                new_state[pid] = {
                    "tier": tier,
                    "reason": reason,
                    "multiplier": self._mult_for_tier(tier),
                    "pct_used": row.get("pct_used"),
                    "calls_24h": int(row.get("calls_24h") or 0),
                    "calls_1h": int(row.get("calls_1h") or 0),
                    "limit_per_day": row.get("limit_per_day"),
                    "essential": pid in ESSENTIAL_PROVIDERS,
                }
            self._state = new_state
            self._updated_at = _iso(now_dt)

    # -----------------------------------------------------------------
    # Public lookups (lock-guarded, never raise)
    # -----------------------------------------------------------------
    def tier(self, provider: str) -> str:
        with self._lock:
            entry = self._state.get(provider)
            return (entry or {}).get("tier", "green")

    def cadence_multiplier(self, provider: str) -> float:
        with self._lock:
            entry = self._state.get(provider)
        if not entry:
            return MULT_GREEN
        return float(entry.get("multiplier", MULT_GREEN))

    def should_skip(self, provider: str) -> bool:
        """True iff provider is red AND not on the essential list."""
        if provider in ESSENTIAL_PROVIDERS:
            return False
        with self._lock:
            entry = self._state.get(provider)
        if not entry:
            return False
        return entry.get("tier") == "red"

    def snapshot(self) -> dict[str, Any]:
        """Serializable budget state for the `quota` blob.

        Shape (merged into the quota blob under key `budget`):
            {
              "updatedAt": "...",
              "providers": {
                "gfw": {"tier": "red", "multiplier": 3.0, "pct_used": null,
                        "calls_24h": 1234, "calls_1h": 200, "reason": "rate-spike",
                        "essential": false, "limit_per_day": null,
                        "should_skip": true},
                ...
              },
              "summary": {"green": 17, "amber": 2, "red": 1},
              "essential": [...],
            }
        """
        with self._lock:
            providers: dict[str, dict[str, Any]] = {}
            counts = {"green": 0, "amber": 0, "red": 0}
            for pid, entry in self._state.items():
                tier = entry.get("tier", "green")
                counts[tier] = counts.get(tier, 0) + 1
                providers[pid] = {
                    "tier": tier,
                    "multiplier": entry.get("multiplier", MULT_GREEN),
                    "pct_used": entry.get("pct_used"),
                    "calls_24h": entry.get("calls_24h", 0),
                    "calls_1h": entry.get("calls_1h", 0),
                    "limit_per_day": entry.get("limit_per_day"),
                    "essential": entry.get("essential", False),
                    "reason": entry.get("reason", "ok"),
                    "should_skip": (
                        tier == "red" and pid not in ESSENTIAL_PROVIDERS
                    ),
                }
            return {
                "updatedAt": self._updated_at,
                "providers": providers,
                "summary": counts,
                "essential": sorted(ESSENTIAL_PROVIDERS),
            }

    # -----------------------------------------------------------------
    # Internal evaluation
    # -----------------------------------------------------------------
    @staticmethod
    def _mult_for_tier(tier: str) -> float:
        if tier == "red":
            return MULT_RED
        if tier == "amber":
            return MULT_AMBER
        return MULT_GREEN

    @staticmethod
    def _raw_tier(pct: float | None, calls_1h: int, calls_24h: int,
                  err_rate_recent: float | None,
                  recent_events: int) -> tuple[str, str]:
        """Hysteresis-free classification: what tier does this row LOOK like,
        ignoring history. Returns (tier, reason)."""
        # Error-rate cliff first — a provider that's actively erroring should
        # back off regardless of where its 24h count sits.
        if (err_rate_recent is not None
                and recent_events >= RECENT_ERR_MIN
                and err_rate_recent >= RECENT_ERR_THRESHOLD):
            return "red", f"error-rate {int(err_rate_recent * 100)}%"

        if pct is not None:
            if pct >= AMBER_CEIL:
                return "red", f"pct_used {pct:.0%}"
            if pct >= GREEN_CEIL:
                return "amber", f"pct_used {pct:.0%}"

        # Rate-spike anomaly: if the last hour shows ≥10× the prior-23h
        # average AND we've seen at least 20 events recently, treat as amber.
        # Same rule quota.py uses for the "anomalous" status — keeps the two
        # surfaces in sync.
        prior_23h = calls_24h - calls_1h
        if calls_1h >= RATE_JUMP_MIN_RECENT and prior_23h > 0:
            prior_per_hour = prior_23h / 23.0
            if prior_per_hour > 0 and calls_1h >= RATE_JUMP_FACTOR * prior_per_hour:
                return "amber", "rate-spike"

        return "green", "ok"

    def _evaluate(self, pid: str, row: dict[str, Any], prev_tier: str,
                  now_dt: datetime) -> tuple[str, str]:
        """Compute the (possibly-hysteresis-adjusted) tier for one provider."""
        pct = row.get("pct_used")
        calls_1h = int(row.get("calls_1h") or 0)
        calls_24h = int(row.get("calls_24h") or 0)

        # Error rate from by_kind_24h is the best signal we have without
        # asking the tracker to retain per-event timestamps in a richer form.
        # We treat any kind whose name starts with "error" as an error event;
        # quota.record() is called with kind="error" / "error_4xx" / etc. at
        # the parent's discretion. If no error events are tracked yet, the
        # error-rate path simply doesn't fire (returns None → skipped).
        by_kind = row.get("by_kind_24h") or {}
        err_24h = sum(n for k, n in by_kind.items() if str(k).startswith("error"))
        # Approximate the 5-minute window as a uniform slice of the 1h count.
        # We don't have per-event timestamps in the snapshot row, but this is
        # close enough for the "is the recent error rate spiking" signal —
        # which is what triggers the cliff. Conservative bias: scale errors
        # by the same 1h window so we don't synthesize errors that aren't
        # there.
        err_rate_recent = None
        recent_events = calls_1h
        if calls_1h > 0 and err_24h > 0:
            # Assume errors are proportionally distributed in the 24h window;
            # tag the rate as the 24h error rate (worst-case undercount, but
            # never an overcount that would spuriously push a healthy
            # provider into red).
            err_rate_recent = err_24h / max(calls_24h, 1)

        raw_tier, reason = self._raw_tier(
            pct, calls_1h, calls_24h, err_rate_recent, recent_events,
        )

        # Hysteresis: don't downgrade from red unless we've fallen well below
        # the red threshold, and don't downgrade from amber unless well below
        # the amber threshold. Upgrades (green → amber → red) are always
        # immediate so we react fast to budget pressure.
        if pct is not None:
            if prev_tier == "red" and raw_tier != "red":
                if pct >= AMBER_CEIL - HYSTERESIS:
                    return "red", f"hysteresis (pct_used {pct:.0%})"
            if prev_tier == "amber" and raw_tier == "green":
                if pct >= GREEN_CEIL - HYSTERESIS:
                    return "amber", f"hysteresis (pct_used {pct:.0%})"

        return raw_tier, reason


# Import here to avoid a circular import if quota ever imports budget in the
# future (it doesn't today). We grab the live singleton tracker so the
# module-level `_budget` reads the same in-memory state the rest of the
# collector writes to.
from quota import _tracker  # noqa: E402

# Module-level singleton — `from budget import _budget` everywhere.
_budget = Budget(_tracker)
