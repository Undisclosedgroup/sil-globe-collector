"""Unit tests for quota.Tracker + assess_quota.

Pure-function so all cases are deterministic. We avoid touching the
module-level `_tracker` singleton — each test uses a fresh `Tracker()`
instance to keep state isolated. Mirrors test_health.py conventions.
"""
import pathlib
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from quota import (  # noqa: E402
    Tracker,
    assess_quota,
    PCT_CRITICAL,
)


NOW = datetime(2026, 5, 23, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Tracker / snapshot
# ---------------------------------------------------------------------------
def test_empty_tracker_snapshot_is_ok_everywhere():
    t = Tracker()
    snap = t.snapshot(now=NOW)
    out = assess_quota(snap, now=NOW)

    assert out["overall"] == "ok"
    # Every known provider is present with zero activity.
    ids = {p["id"] for p in out["providers"]}
    assert "vercel_blob" in ids
    assert "gfw" in ids
    for p in out["providers"]:
        assert p["status"] == "ok"
        assert p["calls_24h"] == 0
        assert p["calls_1h"] == 0
    assert out["totals"]["outbound_calls_24h"] == 0
    assert out["totals"]["blob_writes_24h"] == 0
    assert out["alerts"] == []


def test_vercel_blob_over_limit_is_critical():
    """1100 blob writes against Hobby's 1000/day cap -> pct_used=1.1, critical."""
    t = Tracker()
    # Spread the writes across the last hour so they're inside both windows.
    for i in range(1100):
        t.record("vercel_blob",
                 now=NOW - timedelta(seconds=3000 - i))  # ~50 min ago to now
    out = assess_quota(t.snapshot(now=NOW), now=NOW)

    vb = next(p for p in out["providers"] if p["id"] == "vercel_blob")
    assert vb["calls_24h"] == 1100
    assert vb["limit_per_day"] == 1000
    assert abs(vb["pct_used"] - 1.1) < 1e-9
    assert vb["status"] == "critical"
    assert out["overall"] == "critical"

    # Alert fired.
    assert any(a["provider"] == "vercel_blob"
               and a["status"] == "critical" for a in out["alerts"])


def test_known_limit_at_warning_threshold():
    """pct_used between 0.7 and 0.9 -> warning, not critical."""
    t = Tracker()
    for i in range(750):  # 750/1000 = 75% -> warning
        t.record("vercel_blob", now=NOW - timedelta(seconds=60_000 + i))
    out = assess_quota(t.snapshot(now=NOW), now=NOW)

    vb = next(p for p in out["providers"] if p["id"] == "vercel_blob")
    assert vb["status"] == "warning"
    assert out["overall"] == "warning"


def test_gfw_no_known_limit_steady_rate_is_ok():
    """No documented free-tier limit on GFW: even thousands of calls/day stay 'ok'
    unless the rate jumps anomalously."""
    t = Tracker()
    # Evenly distribute over 24h so calls_1h isn't pathologically higher than
    # the prior-23h average (1/24 of total).
    for i in range(2400):
        t.record("gfw", now=NOW - timedelta(seconds=i * 30))  # one every 30s
    out = assess_quota(t.snapshot(now=NOW), now=NOW)

    gfw = next(p for p in out["providers"] if p["id"] == "gfw")
    assert gfw["calls_24h"] == 2400
    assert gfw["limit_per_day"] is None
    assert gfw["pct_used"] is None
    assert gfw["status"] == "ok"


def test_sudden_10x_rate_jump_is_anomalous():
    """Steady prior-23h baseline plus a 10× burst in the last hour -> 'anomalous'."""
    t = Tracker()
    # Baseline: 23 calls in the prior 23h (=1/hour avg).
    for i in range(23):
        t.record("aisstream",
                 now=NOW - timedelta(hours=23, seconds=-i * 60))
    # Burst: 30 calls in the last hour — >= 10× the baseline (1/hr) and
    # >= the RATE_JUMP_MIN_RECENT floor (20).
    for i in range(30):
        t.record("aisstream", now=NOW - timedelta(minutes=30, seconds=-i))
    out = assess_quota(t.snapshot(now=NOW), now=NOW)

    ais = next(p for p in out["providers"] if p["id"] == "aisstream")
    assert ais["status"] == "anomalous"
    assert out["overall"] == "anomalous"
    # The alert message mentions the spike.
    spike = next(a for a in out["alerts"] if a["provider"] == "aisstream")
    assert "spike" in spike["message"]


def test_rolling_window_evicts_events_older_than_24h():
    """Events older than 24h are pruned by snapshot()."""
    t = Tracker()
    # 100 calls 25h ago (should evict) + 50 calls 30 min ago (stay).
    for i in range(100):
        t.record("gfw", now=NOW - timedelta(hours=25, seconds=-i))
    for i in range(50):
        t.record("gfw", now=NOW - timedelta(minutes=30, seconds=-i))
    out = assess_quota(t.snapshot(now=NOW), now=NOW)
    gfw = next(p for p in out["providers"] if p["id"] == "gfw")
    # Only the in-window 50 survive.
    assert gfw["calls_24h"] == 50
    assert gfw["calls_1h"] == 50


def test_record_n_aggregates():
    """record(provider, n=N) adds N to the count in one append."""
    t = Tracker()
    t.record("aisstream", kind="message", n=10_000, now=NOW - timedelta(minutes=5))
    snap = t.snapshot(now=NOW)
    ais = snap["providers"]["aisstream"]
    assert ais["calls_24h"] == 10_000
    assert ais["calls_1h"] == 10_000
    assert ais["by_kind_24h"]["message"] == 10_000


def test_pct_critical_boundary():
    """Hitting exactly 90% should already be 'critical' (>= threshold)."""
    t = Tracker()
    n = int(1000 * PCT_CRITICAL)  # 900
    for i in range(n):
        t.record("vercel_blob", now=NOW - timedelta(minutes=30, seconds=-i))
    out = assess_quota(t.snapshot(now=NOW), now=NOW)
    vb = next(p for p in out["providers"] if p["id"] == "vercel_blob")
    assert vb["status"] == "critical"


def test_unknown_provider_records_and_classifies_ok():
    """A record() call with a provider not in KNOWN_LIMITS still shows up."""
    t = Tracker()
    t.record("new_random_source", now=NOW - timedelta(minutes=1))
    out = assess_quota(t.snapshot(now=NOW), now=NOW)
    row = next(p for p in out["providers"] if p["id"] == "new_random_source")
    assert row["calls_24h"] == 1
    assert row["limit_per_day"] is None
    assert row["status"] == "ok"


def test_summary_text_is_stable():
    """The summary string lists severities in fixed order."""
    t = Tracker()
    for i in range(1100):
        t.record("vercel_blob", now=NOW - timedelta(minutes=5, seconds=-i))
    out = assess_quota(t.snapshot(now=NOW), now=NOW)
    # Sanity check on shape — exact OK count depends on KNOWN_LIMITS size.
    assert "critical" in out["summary"]
    assert "ok" in out["summary"]
    # Most-severe first in summary.
    assert out["summary"].split(",")[0].strip().endswith("critical")
