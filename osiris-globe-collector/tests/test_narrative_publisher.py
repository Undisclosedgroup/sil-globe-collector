"""Unit tests for narrative_publisher.

Follows the conventions in test_narrative.py: top-of-file sys.path injection,
plain pytest functions, fixture builders inline. All network is stubbed via
the `_post` injection hook so tests are pure and deterministic.
"""
import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from narrative_publisher import (  # noqa: E402
    DAILY_HOUR_UTC,
    ENV_VARS,
    configured_destinations,
    format_for_destination,
    publish_all,
    should_publish,
)


NOW = datetime(2026, 5, 23, DAILY_HOUR_UTC, 5, 0, tzinfo=timezone.utc)
NOON = datetime(2026, 5, 23, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------
def _narr(body: str = "Cuba leads the board today, driven by military surge.",
          *, hotspots=None, cached: bool = False,
          model: str = "claude-sonnet-4-6") -> dict:
    body = body.strip()
    return {
        "updatedAt": NOW.isoformat(),
        "generatedAt": NOW.isoformat(),
        "model": model,
        "narrative": body,
        "wordCount": len(body.split()),
        "cached": cached,
        "sources": ["forecast", "alerts", "anomalies", "corroborate"],
        "hotspots_snapshot": hotspots or [],
    }


def _hot(hid: str, level: str) -> dict:
    return {"id": hid, "level": level}


class _PostRecorder:
    """Stub for the HTTP POST hook. Records calls; returns scripted responses."""
    def __init__(self, script=None):
        # `script` is a list of (status, body) tuples consumed in FIFO order.
        # If exhausted, defaults to (200, "ok").
        self.script = list(script or [])
        self.calls = []

    def __call__(self, url, body, headers):
        self.calls.append({
            "url": url,
            "body": json.loads(body.decode("utf-8")),
            "headers": headers,
        })
        if self.script:
            return self.script.pop(0)
        return 200, "ok"


def _no_sleep(_):
    return None


# ---------------------------------------------------------------------------
# 1. No env keys set -> publish_all returns same state, no network calls.
# ---------------------------------------------------------------------------
def test_no_destinations_is_noop(monkeypatch):
    for env in ENV_VARS.values():
        monkeypatch.delenv(env, raising=False)
    rec = _PostRecorder()
    state = {"sentinel": "untouched"}
    new_state = publish_all(_narr(), state, now=NOW,
                            _post=rec, _sleep=_no_sleep)
    assert rec.calls == []
    # Returns a fresh dict (we don't mutate input) but the sentinel survives.
    assert new_state["sentinel"] == "untouched"
    assert configured_destinations() == []


# ---------------------------------------------------------------------------
# 2. Single destination + first call -> publishes, returns updated state.
# ---------------------------------------------------------------------------
def test_single_destination_publishes_and_updates_state(monkeypatch):
    for env in ENV_VARS.values():
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("NARRATIVE_SLACK_WEBHOOK", "https://hooks.example/slack/abc")
    rec = _PostRecorder()
    narrative = _narr(hotspots=[_hot("cuba", "elevated")])
    new_state = publish_all(narrative, {}, now=NOW,
                            _post=rec, _sleep=_no_sleep)

    assert len(rec.calls) == 1
    call = rec.calls[0]
    assert call["url"] == "https://hooks.example/slack/abc"
    # Slack Block Kit shape
    assert call["body"]["blocks"][0]["type"] == "header"
    assert "SIL Daily Brief" in call["body"]["blocks"][0]["text"]["text"]
    assert call["body"]["blocks"][2]["type"] == "section"

    # state advanced
    assert new_state["publishes"]["slack"]["last_md5"]
    assert new_state["last_body_md5"]
    assert new_state["last_daily_date"] == NOW.date().isoformat()


# ---------------------------------------------------------------------------
# 3. Same narrative published twice -> first publishes, second dedup-skipped.
# ---------------------------------------------------------------------------
def test_same_narrative_dedup_skips_second_call(monkeypatch):
    for env in ENV_VARS.values():
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("NARRATIVE_SLACK_WEBHOOK", "https://hooks.example/slack/abc")
    rec = _PostRecorder()
    narrative = _narr()
    state = publish_all(narrative, {}, now=NOW, _post=rec, _sleep=_no_sleep)
    assert len(rec.calls) == 1
    # Second pass with identical body -> no new POST.
    state2 = publish_all(narrative, state, now=NOW + timedelta(minutes=10),
                         _post=rec, _sleep=_no_sleep)
    assert len(rec.calls) == 1, "dedup should suppress identical body"
    # State remains consistent.
    assert state2["last_body_md5"] == state["last_body_md5"]


# ---------------------------------------------------------------------------
# 4. Daily-trigger fires only once per UTC day.
# ---------------------------------------------------------------------------
def test_daily_trigger_fires_once_per_day(monkeypatch):
    for env in ENV_VARS.values():
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("NARRATIVE_SLACK_WEBHOOK", "https://hooks.example/slack/abc")
    rec = _PostRecorder()

    state = publish_all(_narr("Brief one."), {}, now=NOW,
                        _post=rec, _sleep=_no_sleep)
    assert len(rec.calls) == 1
    assert state["last_daily_date"] == NOW.date().isoformat()

    # Same day, different body (cache invalidates), but should_publish must
    # not re-fire on the daily trigger because state['last_daily_date'] matches.
    later_same_day = NOW + timedelta(hours=2)
    publish2, reason2 = should_publish(_narr("Brief two."), state,
                                       now=later_same_day)
    assert not publish2, f"daily must not re-fire same UTC day, got {reason2}"

    # Next UTC day at the trigger hour -> fires again.
    next_day = NOW + timedelta(days=1)
    publish3, reason3 = should_publish(_narr("Brief three."), state,
                                       now=next_day)
    assert publish3, f"daily must re-fire next UTC day, got {reason3}"
    assert reason3.startswith("daily")


# ---------------------------------------------------------------------------
# 5. Significant-change trigger: routine -> crisis fires; staying at crisis does not.
# ---------------------------------------------------------------------------
def test_significant_change_trigger_fires_on_new_crisis(monkeypatch):
    # Use noon (NOT the daily hour) so we know only the change trigger can fire.
    state = {
        "last_hotspot_levels": {"cuba": "routine"},
        "last_daily_date": NOON.date().isoformat(),
        "last_body_md5": "stale-md5",
    }
    narrative = _narr("New crisis prose body.",
                      hotspots=[_hot("cuba", "crisis")])
    publish, reason = should_publish(narrative, state, now=NOON)
    assert publish, f"routine->crisis must fire, got {reason}"
    assert "significant_change" in reason


def test_significant_change_does_not_refire_when_staying_at_crisis():
    state = {
        "last_hotspot_levels": {"cuba": "crisis"},
        "last_daily_date": NOON.date().isoformat(),
        "last_body_md5": "stale-md5",
    }
    narrative = _narr("Same crisis story, different body.",
                      hotspots=[_hot("cuba", "crisis")])
    publish, reason = should_publish(narrative, state, now=NOON)
    assert not publish, f"crisis->crisis must NOT re-fire, got {reason}"


def test_significant_change_fires_on_new_high():
    state = {
        "last_hotspot_levels": {"taiwan": "elevated"},
        "last_daily_date": NOON.date().isoformat(),
        "last_body_md5": "stale-md5",
    }
    narrative = _narr("Taiwan escalation prose.",
                      hotspots=[_hot("taiwan", "high")])
    publish, reason = should_publish(narrative, state, now=NOON)
    assert publish
    assert "significant_change" in reason


def test_high_to_high_does_not_refire():
    state = {
        "last_hotspot_levels": {"taiwan": "high"},
        "last_daily_date": NOON.date().isoformat(),
        "last_body_md5": "stale-md5",
    }
    narrative = _narr("Same body different hash.",
                      hotspots=[_hot("taiwan", "high")])
    publish, reason = should_publish(narrative, state, now=NOON)
    assert not publish


# ---------------------------------------------------------------------------
# 6. 503 then 200 -> retry succeeds.
# ---------------------------------------------------------------------------
def test_retry_on_503_then_succeeds(monkeypatch):
    for env in ENV_VARS.values():
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("NARRATIVE_DISCORD_WEBHOOK",
                       "https://discord.example/webhooks/123/abc")
    rec = _PostRecorder(script=[(503, "service unavailable"), (200, "ok")])
    state = publish_all(_narr(), {}, now=NOW, _post=rec, _sleep=_no_sleep)
    assert len(rec.calls) == 2, "must retry once and succeed"
    assert state["publishes"]["discord"]["last_status"] == 200


# ---------------------------------------------------------------------------
# 7. Per-destination failure isolation.
# ---------------------------------------------------------------------------
def test_one_destination_failure_does_not_block_others(monkeypatch):
    for env in ENV_VARS.values():
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("NARRATIVE_SLACK_WEBHOOK", "https://hooks.example/slack/abc")
    monkeypatch.setenv("NARRATIVE_WEBHOOK_URL", "https://json.example/in")

    # Slack hard-fails (400 — non-retryable). JSON succeeds (200).
    # Order in configured_destinations() is fixed by ENV_VARS dict insertion:
    # slack, discord, json, email.
    rec = _PostRecorder(script=[(400, "bad request"), (200, "ok")])
    state = publish_all(_narr(), {}, now=NOW, _post=rec, _sleep=_no_sleep)

    # Both attempted; one failed, one succeeded.
    assert len(rec.calls) == 2
    assert "last_error" in state["publishes"]["slack"]
    assert state["publishes"]["json"]["last_status"] == 200


# ---------------------------------------------------------------------------
# Format checks (sanity — that the payloads are well-formed).
# ---------------------------------------------------------------------------
def test_format_slack_shape():
    p = format_for_destination(_narr(), "slack", now=NOW)
    assert "blocks" in p
    kinds = [b["type"] for b in p["blocks"]]
    assert kinds[0] == "header"
    assert "divider" in kinds


def test_format_discord_shape():
    p = format_for_destination(
        _narr(hotspots=[_hot("cuba", "crisis")]), "discord", now=NOW)
    assert "embeds" in p
    emb = p["embeds"][0]
    assert emb["color"] == 0xFF4444   # critical
    assert "SIL Daily Brief" in emb["title"]


def test_format_json_shape():
    p = format_for_destination(_narr(), "json", now=NOW)
    assert "narrative" in p and "publishedAt" in p and "model" in p


def test_format_email_shape():
    p = format_for_destination(_narr(), "email", now=NOW)
    assert set(p.keys()) >= {"to", "subject", "html", "text"}
    assert "SIL Daily Brief" in p["subject"]
    assert "<p>" in p["html"]


# ---------------------------------------------------------------------------
# Empty narrative (dark-mode) never publishes.
# ---------------------------------------------------------------------------
def test_empty_narrative_is_never_published(monkeypatch):
    for env in ENV_VARS.values():
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("NARRATIVE_SLACK_WEBHOOK", "https://hooks.example/slack/abc")
    rec = _PostRecorder()
    empty = _narr("")
    publish, reason = should_publish(empty, {}, now=NOW)
    assert not publish
    assert reason == "empty"
    state = publish_all(empty, {}, now=NOW, _post=rec, _sleep=_no_sleep)
    assert rec.calls == []
    assert "publishes" in state
