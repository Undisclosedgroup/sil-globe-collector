"""Unit tests for budget.Budget.

Pure-function tier evaluation over a fresh `Tracker()` per test, so we don't
touch module-level singletons. Mirrors test_quota.py conventions.
"""
import pathlib
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from quota import Tracker  # noqa: E402
from budget import (  # noqa: E402
    Budget,
    ESSENTIAL_PROVIDERS,
    MULT_GREEN,
    MULT_AMBER,
    MULT_RED,
)


NOW = datetime(2026, 5, 23, 12, 0, 0, tzinfo=timezone.utc)


def _record_writes(tracker: Tracker, provider: str, n: int, *,
                   now: datetime = NOW) -> None:
    """Spread `n` writes across the last hour so they land in both windows."""
    for i in range(n):
        tracker.record(provider, now=now - timedelta(seconds=3000 - i))


# ---------------------------------------------------------------------------
# 1. Cold tracker → all providers green.
# ---------------------------------------------------------------------------
def test_cold_tracker_all_green():
    t = Tracker()
    b = Budget(t, now=NOW)
    snap = b.snapshot()
    assert snap["summary"]["red"] == 0
    assert snap["summary"]["amber"] == 0
    assert snap["summary"]["green"] > 0  # all known providers seeded
    for pid, entry in snap["providers"].items():
        assert entry["tier"] == "green", f"{pid} not green: {entry}"
    assert b.cadence_multiplier("vercel_blob") == MULT_GREEN
    assert b.should_skip("gfw") is False


# ---------------------------------------------------------------------------
# 2. vercel_blob at 50% → green.
# ---------------------------------------------------------------------------
def test_vercel_blob_50pct_green():
    t = Tracker()
    _record_writes(t, "vercel_blob", 500)  # 50% of 1000
    b = Budget(t, now=NOW)
    assert b.tier("vercel_blob") == "green"
    assert b.cadence_multiplier("vercel_blob") == MULT_GREEN
    assert b.should_skip("vercel_blob") is False


# ---------------------------------------------------------------------------
# 3. vercel_blob at 70% → amber.
# ---------------------------------------------------------------------------
def test_vercel_blob_70pct_amber():
    t = Tracker()
    _record_writes(t, "vercel_blob", 700)  # 70%
    b = Budget(t, now=NOW)
    assert b.tier("vercel_blob") == "amber"
    assert b.cadence_multiplier("vercel_blob") == MULT_AMBER
    assert b.should_skip("vercel_blob") is False  # essential


# ---------------------------------------------------------------------------
# 4. vercel_blob at 90% → red; should_skip still False (essential).
# ---------------------------------------------------------------------------
def test_vercel_blob_90pct_red_but_not_skipped():
    t = Tracker()
    _record_writes(t, "vercel_blob", 900)  # 90%
    b = Budget(t, now=NOW)
    assert b.tier("vercel_blob") == "red"
    assert b.cadence_multiplier("vercel_blob") == MULT_RED
    # Essential providers stretch but never skip.
    assert b.should_skip("vercel_blob") is False
    assert "vercel_blob" in ESSENTIAL_PROVIDERS


# ---------------------------------------------------------------------------
# 5. gfw at 90% → red; should_skip("gfw") True (non-essential).
#
# gfw has no documented daily limit in KNOWN_LIMITS, so to drive it to red
# via pct_used we have to either (a) inject a synthetic limit or (b) hit the
# error-rate cliff. We do (a) since the assignment is testing the
# pct_used→red→should_skip chain.
# ---------------------------------------------------------------------------
def test_gfw_red_should_skip():
    t = Tracker()
    # Synthesize a limit so pct_used can be computed. We do this by adding a
    # row directly to the tracker (mimicking a future KNOWN_LIMITS["gfw"]).
    from quota import KNOWN_LIMITS
    original = KNOWN_LIMITS.get("gfw")
    KNOWN_LIMITS["gfw"] = {"limit_per_day": 100}
    try:
        _record_writes(t, "gfw", 90)  # 90% of 100
        b = Budget(t, now=NOW)
        assert b.tier("gfw") == "red"
        assert b.cadence_multiplier("gfw") == MULT_RED
        assert b.should_skip("gfw") is True
        assert "gfw" not in ESSENTIAL_PROVIDERS
    finally:
        if original is None:
            KNOWN_LIMITS["gfw"] = {"limit_per_day": None}
        else:
            KNOWN_LIMITS["gfw"] = original


# ---------------------------------------------------------------------------
# 6. Hysteresis: gfw at 90% → red. Drop to 84% → still red. Drop to 79% → amber.
# ---------------------------------------------------------------------------
def test_hysteresis_red_to_amber_sticky():
    t = Tracker()
    from quota import KNOWN_LIMITS
    original = KNOWN_LIMITS.get("gfw")
    KNOWN_LIMITS["gfw"] = {"limit_per_day": 100}
    try:
        _record_writes(t, "gfw", 90)  # 90% → red
        b = Budget(t, now=NOW)
        assert b.tier("gfw") == "red"

        # Manually replace the tracker contents to drive pct_used down
        # without waiting 24h. The cleanest way: blow away the deque and
        # re-record at the new level.
        t._events["gfw"].clear()
        _record_writes(t, "gfw", 84)  # 84% → would be amber raw, but red-sticky
        b.refresh(now=NOW)
        assert b.tier("gfw") == "red", (
            f"expected red-sticky at 84%, got {b.tier('gfw')}"
        )

        t._events["gfw"].clear()
        _record_writes(t, "gfw", 79)  # 79% < (0.85 - 0.05) → amber
        b.refresh(now=NOW)
        assert b.tier("gfw") == "amber", (
            f"expected amber after falling below hysteresis floor, "
            f"got {b.tier('gfw')}"
        )
    finally:
        if original is None:
            KNOWN_LIMITS["gfw"] = {"limit_per_day": None}
        else:
            KNOWN_LIMITS["gfw"] = original


# ---------------------------------------------------------------------------
# 7. Cadence multiplier monotonic per tier.
# ---------------------------------------------------------------------------
def test_cadence_multiplier_monotonic():
    assert MULT_GREEN < MULT_AMBER < MULT_RED
    assert MULT_GREEN == 1.0
    # amber in the allowed 1.5–2.0 band per the spec.
    assert 1.5 <= MULT_AMBER <= 2.0
    assert MULT_RED == 3.0


# ---------------------------------------------------------------------------
# Bonus coverage — defensive lookups.
# ---------------------------------------------------------------------------
def test_unknown_provider_defaults_green():
    t = Tracker()
    b = Budget(t, now=NOW)
    assert b.tier("does_not_exist") == "green"
    assert b.cadence_multiplier("does_not_exist") == MULT_GREEN
    assert b.should_skip("does_not_exist") is False


def test_snapshot_shape_includes_essential_list():
    t = Tracker()
    b = Budget(t, now=NOW)
    snap = b.snapshot()
    assert "updatedAt" in snap
    assert "providers" in snap
    assert "summary" in snap
    assert "essential" in snap
    assert "vercel_blob" in snap["essential"]
    assert "aisstream" in snap["essential"]


def test_refresh_picks_up_new_writes():
    t = Tracker()
    b = Budget(t, now=NOW)
    assert b.tier("vercel_blob") == "green"
    _record_writes(t, "vercel_blob", 900)
    b.refresh(now=NOW)
    assert b.tier("vercel_blob") == "red"


def test_essential_providers_never_skip_even_when_red():
    """Belt-and-suspenders: even if a future code change broke the
    cadence_multiplier path, the essential providers' should_skip must
    still return False."""
    t = Tracker()
    _record_writes(t, "vercel_blob", 999)  # forcibly red
    b = Budget(t, now=NOW)
    for ess in ESSENTIAL_PROVIDERS:
        assert b.should_skip(ess) is False, f"essential {ess} got skipped"
