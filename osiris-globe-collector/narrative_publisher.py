"""Auto-publish the LLM narrative brief to operator destinations.

The narrative built by `narrative.py` currently only lands in a Vercel-Blob
JSON file consumed by the /globe NarrativeBrief panel. Operators don't read
dashboards — they read what's pushed. This module fans the same payload out
to one or more configured destinations (Slack / Discord / generic JSON /
email-via-webhook) on two trigger paths:

  1. Daily — first cycle of the configured UTC hour (default 07:00).
  2. Significant change — a NEW hotspot crosses into CRISIS, or a NEW HIGH
     hotspot appears that wasn't HIGH/CRISIS at last publish.

Activation: set one or more of NARRATIVE_SLACK_WEBHOOK / NARRATIVE_DISCORD_WEBHOOK
/ NARRATIVE_WEBHOOK_URL / NARRATIVE_EMAIL_WEBHOOK. With no destinations set the
module is a clean no-op — same dark-mode pattern as gfw.py / narrative.py.

Failure isolation: per-destination try/except; a failed Slack POST never
prevents Discord from firing or blocks the collector loop. 3 retries on 5xx
with exponential backoff.

Dedup: md5 over the narrative body; per-destination state remembers the last
md5 + timestamp so the same prose never re-posts (even across triggers).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from urllib import request as _rq
from urllib.error import URLError, HTTPError


log = logging.getLogger("narrative-publisher")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DAILY_HOUR_UTC = int(os.environ.get("NARRATIVE_DAILY_HOUR_UTC", "7"))
HTTP_TIMEOUT = 30
MAX_RETRIES = 3
RETRY_STATUS = {500, 502, 503, 504}
DISCORD_DESCRIPTION_MAX = 4096
SLACK_TEXT_MAX = 2900   # Slack section text limit is 3000; leave a little slack

# Severity-derived embed colors (Discord uses ints; mirror webhooks.ts)
COLOR_CRITICAL = 0xFF4444
COLOR_WARNING = 0xD4AF37
COLOR_INFO = 0x00E5FF

ENV_VARS = {
    "slack":    "NARRATIVE_SLACK_WEBHOOK",
    "discord":  "NARRATIVE_DISCORD_WEBHOOK",
    "json":     "NARRATIVE_WEBHOOK_URL",
    "email":    "NARRATIVE_EMAIL_WEBHOOK",
}


# ---------------------------------------------------------------------------
# Destination discovery
# ---------------------------------------------------------------------------
def configured_destinations() -> list[dict]:
    """Read env vars and return a list of {id, kind, url} for each configured
    destination. Empty list means no-op."""
    out = []
    for kind, env in ENV_VARS.items():
        url = (os.environ.get(env) or "").strip()
        if url:
            out.append({"id": kind, "kind": kind, "url": url})
    return out


# ---------------------------------------------------------------------------
# Trigger logic
# ---------------------------------------------------------------------------
CRISIS_LEVELS = {"crisis", "critical"}
HIGH_OR_CRISIS = {"crisis", "critical", "high"}


def _hotspot_levels(narrative: dict) -> dict[str, str]:
    """Build {hotspot_id: level} map from the optional `hotspots_snapshot`
    embedded on the narrative payload by the caller. We accept either an
    explicit snapshot key or fall back to a flat list shape."""
    snap = narrative.get("hotspots_snapshot") or narrative.get("hotspots") or []
    out = {}
    for h in snap:
        hid = h.get("id") or h.get("hotspotId") or h.get("name")
        lvl = (h.get("level") or "").lower()
        if hid and lvl:
            out[str(hid)] = lvl
    return out


def _body_md5(narrative: dict) -> str:
    body = (narrative.get("narrative") or "").strip()
    return hashlib.md5(body.encode("utf-8")).hexdigest()


def _max_severity(levels: dict[str, str]) -> str:
    """Coarse severity bucket for the current narrative — drives Discord color."""
    vals = set(levels.values())
    if vals & CRISIS_LEVELS:
        return "critical"
    if "high" in vals:
        return "warning"
    return "info"


def should_publish(
    narrative: dict,
    state: dict,
    *,
    now: Optional[datetime] = None,
) -> tuple[bool, str]:
    """Return (publish?, reason).

    Two trigger paths:
      1. Daily: now.hour == DAILY_HOUR_UTC AND state['last_daily_date'] != now.date()
      2. Significant change: a NEW hotspot at CRISIS that wasn't in
         state['last_hotspot_levels'], OR a NEW hotspot at HIGH that wasn't at
         HIGH/CRISIS last publish.

    Returns (False, "cached") if narrative body md5 matches state['last_body_md5'].
    Returns (False, "empty") if the narrative body is empty (dark-mode).
    """
    now = now or datetime.now(timezone.utc)

    body = (narrative.get("narrative") or "").strip()
    if not body:
        return False, "empty"

    # Global dedup — same body as last published anywhere, never re-publish.
    cur_md5 = _body_md5(narrative)
    if state.get("last_body_md5") == cur_md5:
        return False, "cached"

    # --- significant-change trigger ---
    cur_levels = _hotspot_levels(narrative)
    prev_levels = state.get("last_hotspot_levels") or {}
    for hid, lvl in cur_levels.items():
        prev = prev_levels.get(hid, "")
        if lvl in CRISIS_LEVELS and prev not in CRISIS_LEVELS:
            return True, f"significant_change: {hid} -> {lvl}"
        if lvl == "high" and prev not in HIGH_OR_CRISIS:
            return True, f"significant_change: {hid} -> {lvl}"

    # --- daily trigger ---
    last_daily = state.get("last_daily_date")
    today = now.date().isoformat()
    if now.hour == DAILY_HOUR_UTC and last_daily != today:
        return True, f"daily: {DAILY_HOUR_UTC:02d}:00 UTC"

    return False, "no_trigger"


# ---------------------------------------------------------------------------
# Per-destination formatters
# ---------------------------------------------------------------------------
def _date_header(now: datetime) -> str:
    return now.strftime("%-d %b %Y") if hasattr(now, "strftime") else str(now)


def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[: max(0, n - 1)] + "…"


def _format_slack(narrative: dict, *, now: datetime) -> dict:
    """Slack Block Kit — header, context, narrative section, divider, footer."""
    body = (narrative.get("narrative") or "").strip()
    word_count = narrative.get("wordCount") or len(body.split())
    model = narrative.get("model") or "unknown"
    cached = " (cached)" if narrative.get("cached") else ""
    header_text = f"\U0001F310 SIL Daily Brief — {_date_header(now)}"
    ctx_text = f"*{model}*{cached} · {word_count} words · {now.isoformat()}"
    section_text = _truncate(body, SLACK_TEXT_MAX)
    return {
        "text": header_text,   # plain-text fallback for limited clients
        "blocks": [
            {"type": "header",
             "text": {"type": "plain_text", "text": header_text[:150], "emoji": True}},
            {"type": "context",
             "elements": [{"type": "mrkdwn", "text": ctx_text}]},
            {"type": "section",
             "text": {"type": "mrkdwn", "text": section_text}},
            {"type": "divider"},
            {"type": "context",
             "elements": [{"type": "mrkdwn",
                           "text": "Generated by Social Intelligence Labs"}]},
        ],
    }


def _format_discord(narrative: dict, *, now: datetime) -> dict:
    """Discord embed — title, narrative description (truncated), severity color."""
    body = (narrative.get("narrative") or "").strip()
    word_count = narrative.get("wordCount") or len(body.split())
    model = narrative.get("model") or "unknown"
    cached_tag = "cached" if narrative.get("cached") else "fresh"
    levels = _hotspot_levels(narrative)
    sev = _max_severity(levels)
    color = {
        "critical": COLOR_CRITICAL,
        "warning":  COLOR_WARNING,
        "info":     COLOR_INFO,
    }[sev]
    return {
        "embeds": [
            {
                "title": f"SIL Daily Brief — {_date_header(now)}",
                "description": _truncate(body, DISCORD_DESCRIPTION_MAX),
                "color": color,
                "timestamp": now.isoformat(),
                "footer": {"text": f"{model} · {word_count} words · {cached_tag}"},
            }
        ]
    }


def _format_json(narrative: dict, *, now: datetime) -> dict:
    """Passthrough envelope for downstream pipelines."""
    return {
        "narrative": (narrative.get("narrative") or "").strip(),
        "generatedAt": narrative.get("generatedAt") or narrative.get("updatedAt"),
        "model": narrative.get("model"),
        "wordCount": narrative.get("wordCount")
                     or len((narrative.get("narrative") or "").split()),
        "cached": bool(narrative.get("cached")),
        "hotspots_snapshot": narrative.get("hotspots_snapshot")
                             or narrative.get("hotspots") or [],
        "publishedAt": now.isoformat(),
    }


def _format_email(narrative: dict, *, now: datetime) -> dict:
    """{to, subject, html, text} for a generic email-sending webhook gateway.

    The caller's webhook (e.g. a tiny Resend / Postmark proxy) is expected to
    accept this exact shape. The `to` is left empty by default — the user's
    gateway can fill it in from its own config, or we can extend the env-var
    contract later to include NARRATIVE_EMAIL_TO."""
    body = (narrative.get("narrative") or "").strip()
    word_count = narrative.get("wordCount") or len(body.split())
    model = narrative.get("model") or "unknown"
    subject = f"SIL Daily Brief — {_date_header(now)}"
    # Plain-text version: just the prose with a small footer.
    text = (
        f"{body}\n\n"
        f"--\nGenerated by Social Intelligence Labs · "
        f"{model} · {word_count} words · {now.isoformat()}\n"
    )
    # Lightweight HTML — paragraph-wrap on double-newline; safe escaping.
    html_paras = "".join(
        f"<p>{_html_escape(p)}</p>"
        for p in body.split("\n\n") if p.strip()
    )
    html = (
        f"<div style=\"font-family:system-ui,sans-serif;max-width:680px;\">"
        f"<h2 style=\"margin:0 0 12px;\">{_html_escape(subject)}</h2>"
        f"{html_paras}"
        f"<hr style=\"border:none;border-top:1px solid #ddd;margin:24px 0 8px;\"/>"
        f"<p style=\"color:#666;font-size:12px;margin:0;\">Generated by "
        f"Social Intelligence Labs · {_html_escape(model)} · "
        f"{word_count} words · {_html_escape(now.isoformat())}</p>"
        f"</div>"
    )
    to = (os.environ.get("NARRATIVE_EMAIL_TO") or "").strip()
    return {
        "to": to,
        "subject": subject,
        "html": html,
        "text": text,
    }


def _html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace("\"", "&quot;")
    )


def format_for_destination(narrative: dict, kind: str,
                           *, now: Optional[datetime] = None) -> dict:
    """Public formatter dispatch. Returns the JSON-encodable payload body."""
    now = now or datetime.now(timezone.utc)
    if kind == "slack":
        return _format_slack(narrative, now=now)
    if kind == "discord":
        return _format_discord(narrative, now=now)
    if kind == "json":
        return _format_json(narrative, now=now)
    if kind == "email":
        return _format_email(narrative, now=now)
    raise ValueError(f"unknown destination kind: {kind}")


# ---------------------------------------------------------------------------
# HTTP POST with retry
# ---------------------------------------------------------------------------
def _default_post(url: str, body: bytes, headers: dict) -> tuple[int, str]:
    """Single POST attempt. Returns (status, body_text). Raises on transport
    error (caller handles retry)."""
    req = _rq.Request(url, data=body, method="POST", headers=headers)
    with _rq.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.status, resp.read().decode("utf-8", errors="replace")


def _post_with_retry(
    url: str, payload: dict,
    *, _post: Optional[Callable[..., tuple[int, str]]] = None,
    _sleep: Optional[Callable[[float], None]] = None,
) -> tuple[bool, int, str]:
    """POST payload with 3 retries on 5xx + transient transport errors.

    Returns (ok, status, message). `_post` / `_sleep` injectable for tests."""
    post = _post or _default_post
    sleep = _sleep or time.sleep
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "osiris-globe-collector/1.0 (+narrative-publisher)",
    }
    last_status = 0
    last_msg = ""
    for attempt in range(MAX_RETRIES):
        try:
            status, msg = post(url, body, headers)
            last_status = status
            last_msg = msg
            if 200 <= status < 300:
                return True, status, msg
            if status not in RETRY_STATUS:
                # 4xx / other non-retryable — bail immediately
                return False, status, msg
        except HTTPError as e:
            last_status = e.code
            last_msg = str(e)
            if e.code not in RETRY_STATUS:
                return False, e.code, last_msg
        except (URLError, TimeoutError, OSError) as e:
            last_status = 0
            last_msg = f"{type(e).__name__}: {e}"
        if attempt < MAX_RETRIES - 1:
            sleep(2 ** attempt)   # 1s, 2s
    return False, last_status, last_msg


# ---------------------------------------------------------------------------
# Publish-all orchestrator
# ---------------------------------------------------------------------------
def publish_all(
    narrative: dict,
    state: dict,
    *,
    now: Optional[datetime] = None,
    destinations: Optional[list[dict]] = None,
    _post: Optional[Callable[..., tuple[int, str]]] = None,
    _sleep: Optional[Callable[[float], None]] = None,
) -> dict:
    """Decide-and-publish. Returns NEW state (does not mutate input).

    No-ops cleanly when no destinations are configured. Per-destination
    failure is isolated: one failed webhook never prevents the others from
    firing. Dedup is per-destination via state['publishes'][dest_id]."""
    now = now or datetime.now(timezone.utc)
    new_state = dict(state or {})
    new_state.setdefault("publishes", dict(state.get("publishes") or {}))

    dests = destinations if destinations is not None else configured_destinations()
    if not dests:
        return new_state

    publish, reason = should_publish(narrative, new_state, now=now)
    if not publish:
        log.debug("narrative-publisher skip: %s", reason)
        return new_state

    cur_md5 = _body_md5(narrative)
    any_success = False

    for dest in dests:
        dest_id = dest["id"]
        kind = dest["kind"]
        url = dest["url"]
        prev = new_state["publishes"].get(dest_id) or {}
        if prev.get("last_md5") == cur_md5:
            log.debug("narrative-publisher dedup-skip dest=%s", dest_id)
            continue
        try:
            payload = format_for_destination(narrative, kind, now=now)
            ok, status, msg = _post_with_retry(url, payload,
                                               _post=_post, _sleep=_sleep)
            if ok:
                any_success = True
                new_state["publishes"][dest_id] = {
                    "last_md5": cur_md5,
                    "last_at": now.isoformat(),
                    "last_status": status,
                    "last_reason": reason,
                }
                log.info("narrative-publisher OK dest=%s status=%d (%s)",
                         dest_id, status, reason)
            else:
                # Record the failure but DON'T update last_md5 — so the next
                # cycle will retry the same content rather than silently dropping it.
                prev_entry = dict(new_state["publishes"].get(dest_id) or {})
                prev_entry["last_error"] = f"{status}: {_truncate(msg, 200)}"
                prev_entry["last_error_at"] = now.isoformat()
                new_state["publishes"][dest_id] = prev_entry
                log.warning("narrative-publisher FAIL dest=%s status=%s msg=%s",
                            dest_id, status, _truncate(msg, 200))
        except Exception as e:   # noqa: BLE001 — isolation barrier
            log.warning("narrative-publisher EXC dest=%s: %s", dest_id, e)

    # Only advance daily-date / hotspot-levels / body-md5 if SOMETHING succeeded.
    # Otherwise the next cycle re-tries the same trigger.
    if any_success:
        new_state["last_body_md5"] = cur_md5
        new_state["last_hotspot_levels"] = _hotspot_levels(narrative)
        new_state["last_reason"] = reason
        new_state["last_published_at"] = now.isoformat()
        if reason.startswith("daily"):
            new_state["last_daily_date"] = now.date().isoformat()

    return new_state


# ---------------------------------------------------------------------------
# Writer task — wire as `asyncio.create_task(narrative_publisher_writer())`
# from collector.main(). Mirror of health_writer / quota_writer.
# ---------------------------------------------------------------------------
PUBLISHER_INTERVAL_S = 300   # 5 min — cheap check, just timestamps + md5

# Where to read the current narrative blob from. Reusing the same production
# API the rest of the collector reads its own blobs from keeps this writer
# decoupled from intel_writer's in-process state (so if intel_writer hasn't
# yet built a narrative this cycle, we just see what the dashboard sees).
NARRATIVE_BLOB_URL = os.environ.get(
    "NARRATIVE_BLOB_URL",
    "https://osiris.live/api/globe/narrative",
)


def _fetch_narrative_blob() -> Optional[dict]:
    """Pull the latest narrative payload from our own production API. Returns
    None on any failure (logged, non-fatal)."""
    try:
        req = _rq.Request(
            NARRATIVE_BLOB_URL,
            headers={"Accept": "application/json",
                     "User-Agent": "osiris-globe-collector/1.0 (+narrative-publisher)"},
        )
        with _rq.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read())
    except Exception as e:   # noqa: BLE001
        log.debug("narrative-publisher fetch FAIL %s", e)
        return None


async def narrative_publisher_writer():
    """Every PUBLISHER_INTERVAL_S: read the live narrative blob, decide-and-
    publish to configured destinations. No-op when no destinations are
    configured (clean dark-mode). Failure-isolated — one bad destination
    never blocks the others, an exception never crashes the loop.

    State persistence: in-process only. A collector restart resets the daily
    guard, which could result in one extra daily-trigger publish on the same
    UTC day if the restart straddles the configured hour. The md5 dedup
    prevents identical content from re-publishing, but a regenerated narrative
    (different md5, same day) WOULD fire again. Acceptable for v1.
    """
    import asyncio
    if not configured_destinations():
        log.info("narrative-publisher: no destinations configured (set %s) — dark mode",
                 " / ".join(ENV_VARS.values()))
        # Stay alive (don't return) so collector.main()'s gather doesn't see a
        # finished task; just sleep on a long cadence and re-check env vars.
        while True:
            await asyncio.sleep(PUBLISHER_INTERVAL_S * 12)
            if configured_destinations():
                log.info("narrative-publisher: destinations now configured, activating")
                break

    state: dict = {}
    while True:
        try:
            narrative = await asyncio.to_thread(_fetch_narrative_blob)
            if narrative:
                state = await asyncio.to_thread(publish_all, narrative, state)
        except Exception as e:   # noqa: BLE001 — final safety net
            log.warning("narrative-publisher cycle FAIL %s", e)
        await asyncio.sleep(PUBLISHER_INTERVAL_S)
