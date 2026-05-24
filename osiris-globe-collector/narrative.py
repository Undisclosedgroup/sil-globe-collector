"""LLM-generated daily intel brief — a NARRATIVE prose summary by Claude.

The existing /api/globe/brief is a structured "BLUF + tells board + watchboard"
— useful but skeletal. This module produces a richer narrative brief: 200-400
words of flowing prose written by Claude, generated from the same structured
intel (forecast / brief / alerts / anomalies / corroborate) the dashboard
already has. The output is what a senior analyst would dictate as a one-page
read for a busy human consumer.

Activation: set ANTHROPIC_API_KEY in the collector's environment. Until then
this module is a no-op that publishes an empty `narrative` blob — the
NarrativeBrief.tsx section shows an "unavailable — set ANTHROPIC_API_KEY"
empty state. Same dark-mode pattern as gfw.py.

Cache: an md5 of the salient inputs (top-5 hotspot ids+scores, last-5 alert
ids, last-5 anomaly ids) gates regeneration. If the hash matches the previous
hash AND the cached narrative is < CACHE_TTL old, we return the cache (no API
call). At the current change rate this means ~1-4 Claude calls per day.

Function contract:

    build_narrative(forecast, brief, alerts, anomalies, corroborate,
                    *, prev_state=None, now=None,
                    _api_call=None) -> (payload, new_state)

`prev_state` carries {hash, narrative, model, generatedAt, wordCount} from the
last cycle (the collector persists it). On a cache hit, the returned payload is
identical to last cycle's but stamped with `cached: True`.

`_api_call` is an injectable hook for tests — when None we use the real
Anthropic SDK (or stdlib urllib fallback) with retry on 429/5xx.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Optional
from urllib import request as _rq
from urllib.error import URLError, HTTPError


# ---------------------------------------------------------------------------
# Anthropic API config
# ---------------------------------------------------------------------------
MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 700                # ~400 words headroom + a little slack
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
HTTP_TIMEOUT = 60
MAX_RETRIES = 3
RETRY_STATUS = {429, 500, 502, 503, 504}

# Cache TTL — even if inputs don't change much, regenerate every 6h so the
# narrative feels alive and reflects time-of-day context.
CACHE_TTL = timedelta(hours=6)

SOURCES = ["forecast", "alerts", "anomalies", "corroborate"]


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "You are a strategic intelligence analyst writing a SHORT daily brief. "
    "Be direct, specific, sourced, no hedging language. 200-400 words. No "
    "bullet points — flowing prose. Do NOT mention 'the data', 'the system', "
    "'based on the reports', or any meta-reference to your inputs — write as "
    "if a senior analyst dictated this from their own knowledge. Use specific "
    "names, scores, and signal evidence. Structure: one BLUF paragraph, then "
    "2-3 paragraphs of hotspot reads, then one paragraph of 'what to watch "
    "next.'"
)

USER_PROMPT_TEMPLATE = """\
Current global intelligence picture as of {now}:

TOP HOTSPOTS (ranked by escalation score, 0-100):
{hotspots_block}

RECENT ESCALATION ALERTS (last cycle, newest first):
{alerts_block}

RECENT PER-ENTITY ANOMALIES (dropped vessels, MMSI swaps, emergency squawks):
{anomalies_block}

CROSS-SOURCE CORROBORATION (hotspots with multiple kinetic signals lit;
emerging convergences outside the predefined hotspots):
{corroborate_block}

Write the brief now. Plain prose only. No headers, no bullets, no markdown.
"""


# ---------------------------------------------------------------------------
# Input formatting — compact, prose-friendly blocks for the prompt
# ---------------------------------------------------------------------------
def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat()


def _top_hotspots(forecast: dict, n: int = 5) -> list[dict]:
    hs = list((forecast or {}).get("hotspots") or [])
    hs.sort(key=lambda h: h.get("score", 0), reverse=True)
    return hs[:n]


def _format_hotspots_block(forecast: dict) -> str:
    top = _top_hotspots(forecast, 5)
    if not top:
        return "  (no hotspot data this cycle)"
    lines = []
    for h in top:
        sigs = sorted(
            (s for s in h.get("signals", []) if s.get("score", 0) > 0),
            key=lambda s: s.get("score", 0), reverse=True)[:2]
        sig_bits = []
        for s in sigs:
            note = (s.get("note") or "").strip()
            sig_bits.append(
                f"{s.get('label', s.get('key'))}={s.get('score', 0)}"
                + (f" ({note})" if note else ""))
        sig_str = "; ".join(sig_bits) if sig_bits else "no lit signals"
        lines.append(
            f"  - {h.get('name', h.get('id'))}: "
            f"score={h.get('score', 0)}/100 level={h.get('level', '?')}; "
            f"top signals: {sig_str}")
    return "\n".join(lines)


def _format_alerts_block(alerts: dict) -> str:
    events = list((alerts or {}).get("alerts") or [])[:5]
    if not events:
        return "  (no escalation alerts this cycle)"
    lines = []
    for e in events:
        lines.append(
            f"  - [{e.get('severity', '?')}] {e.get('title', '?')} "
            f"({e.get('kind', '?')}); {e.get('detail', '')}".rstrip())
    return "\n".join(lines)


def _format_anomalies_block(anomalies: dict) -> str:
    events = list((anomalies or {}).get("events") or [])[:5]
    if not events:
        return "  (no per-entity anomalies this cycle)"
    lines = []
    for e in events:
        ctx = e.get("context") or {}
        ctx_bits = []
        if ctx.get("nearby_hotspot"):
            ctx_bits.append(f"near {ctx['nearby_hotspot']}")
        if ctx.get("in_eez_of"):
            ctx_bits.append(f"EEZ {ctx['in_eez_of']}")
        ctx_str = f" ({', '.join(ctx_bits)})" if ctx_bits else ""
        lines.append(
            f"  - [{e.get('severity', '?')}] {e.get('kind', '?')}: "
            f"{e.get('label', '?')} — {e.get('detail', '')}"
            f"{ctx_str}".rstrip())
    return "\n".join(lines)


def _format_corroborate_block(corroborate: dict) -> str:
    if not corroborate:
        return "  (no corroboration data this cycle)"
    multi_lit = [
        h for h in (corroborate.get("hotspots") or [])
        if (h.get("kinetic_lit") or 0) >= 2
    ]
    multi_lit.sort(key=lambda h: (h.get("kinetic_lit", 0),
                                   h.get("compound_score", 0)), reverse=True)
    convs = list(corroborate.get("convergences") or [])[:3]
    lines = []
    if multi_lit:
        for h in multi_lit[:5]:
            sigs = ", ".join((h.get("lit_signals") or [])[:4])
            lines.append(
                f"  - {h.get('name', h.get('id'))}: "
                f"{h.get('kinetic_lit', 0)} kinetic signals lit "
                f"[{sigs}], compound={h.get('compound_score', 0)}/100")
    if convs:
        lines.append("  emerging convergences (outside named hotspots):")
        for c in convs:
            sigs = ", ".join((c.get("lit_signals") or [])[:4])
            lines.append(
                f"    - lat {c.get('lat', 0):.1f}, "
                f"lng {c.get('lng', 0):.1f}: [{sigs}] "
                f"score={c.get('score', 0)}/100; "
                f"{c.get('summary', '')}".rstrip())
    if not lines:
        return "  (no multi-signal convergence this cycle)"
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Cache key
# ---------------------------------------------------------------------------
def _input_hash(forecast: dict, alerts: dict, anomalies: dict) -> str:
    """Stable md5 over the salient input fields. Re-generates only when the
    intel materially changes — not on every collector cycle."""
    top = _top_hotspots(forecast, 5)
    key = {
        "hotspots": [(h.get("id"), int(h.get("score", 0))) for h in top],
        "alerts": [e.get("id") for e in
                   ((alerts or {}).get("alerts") or [])[:5]],
        "anomalies": [e.get("id") for e in
                      ((anomalies or {}).get("events") or [])[:5]],
    }
    blob = json.dumps(key, sort_keys=True, separators=(",", ":"))
    return hashlib.md5(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Anthropic API call (SDK if available, else stdlib urllib)
# ---------------------------------------------------------------------------
def _call_anthropic(system: str, user: str, *, api_key: str) -> dict:
    """Call the Anthropic Messages API with retry/backoff. Returns the parsed
    JSON response. Raises on unrecoverable failure — caller handles."""
    # Prefer the official SDK if installed (cleaner + benefits from upstream
    # transport tuning); fall back to stdlib urllib so the module works with
    # zero external deps.
    try:
        import anthropic  # type: ignore
    except ImportError:
        anthropic = None  # noqa: N806

    last_err: Optional[Exception] = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            if anthropic is not None:
                client = anthropic.Anthropic(api_key=api_key)
                resp = client.messages.create(
                    model=MODEL,
                    max_tokens=MAX_TOKENS,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                )
                # Mirror the JSON shape the urllib path returns so the rest of
                # the pipeline doesn't care which path ran.
                content = resp.content[0].text if resp.content else ""
                return {
                    "content": [{"type": "text", "text": content}],
                    "model": getattr(resp, "model", MODEL),
                    "usage": {
                        "input_tokens": getattr(resp.usage, "input_tokens", 0),
                        "output_tokens": getattr(resp.usage, "output_tokens", 0),
                    },
                }
            # urllib fallback
            body = json.dumps({
                "model": MODEL,
                "max_tokens": MAX_TOKENS,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            }).encode("utf-8")
            req = _rq.Request(
                ANTHROPIC_URL, data=body, method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "x-api-key": api_key,
                    "anthropic-version": ANTHROPIC_VERSION,
                    "User-Agent": "osiris-globe-collector/1.0 (+narrative)",
                })
            with _rq.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                if resp.status == 200:
                    return json.loads(resp.read())
                if resp.status not in RETRY_STATUS:
                    raise RuntimeError(
                        f"Anthropic API HTTP {resp.status}")
                last_err = RuntimeError(f"HTTP {resp.status}")
        except HTTPError as e:
            if e.code not in RETRY_STATUS:
                raise
            last_err = e
        except (URLError, TimeoutError, ValueError, OSError) as e:
            last_err = e
        # Handle SDK-raised retryable errors. The SDK exposes a base
        # APIStatusError with a .status_code attribute; we treat the same set
        # as retryable. Anything else bubbles.
        except Exception as e:  # noqa: BLE001
            code = getattr(e, "status_code", None)
            if code in RETRY_STATUS:
                last_err = e
            else:
                raise
        if attempt < MAX_RETRIES:
            time.sleep(2 ** attempt)   # 1s, 2s, 4s
    raise last_err or RuntimeError("Anthropic API failed after retries")


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------
def _empty(now_iso: str, *, error: Optional[str] = None,
           reason: Optional[str] = None) -> dict:
    p = {
        "updatedAt": now_iso,
        "generatedFrom": None,
        "model": MODEL,
        "narrative": "",
        "wordCount": 0,
        "sources": list(SOURCES),
        "cached": False,
    }
    if error:
        p["error"] = error
    if reason:
        p["reason"] = reason
    return p


def build_narrative(
    forecast: dict,
    brief: dict,
    alerts: dict,
    anomalies: dict,
    corroborate: dict,
    *,
    prev_state: Optional[dict] = None,
    now: Optional[datetime] = None,
    _api_call: Optional[Callable[..., dict]] = None,
) -> tuple[dict, dict]:
    """Build the LLM narrative brief payload.

    Returns (payload, new_state). `new_state` should be persisted by the caller
    and passed back as `prev_state` next cycle so we hit the cache.

    Dark-mode (no ANTHROPIC_API_KEY): returns an empty payload, no error noise,
    and an empty new_state. The frontend renders the "unavailable" empty state.
    """
    now = now or _now()
    now_iso = _iso(now)
    api_key = os.environ.get("ANTHROPIC_API_KEY")

    if not api_key:
        return _empty(now_iso, reason="ANTHROPIC_API_KEY not set"), {}

    cur_hash = _input_hash(forecast, alerts, anomalies)

    # ----- cache hit? -----
    prev = prev_state or {}
    prev_hash = prev.get("hash")
    prev_gen_at = prev.get("generatedAt")
    if prev_hash == cur_hash and prev.get("narrative") and prev_gen_at:
        try:
            gen_dt = datetime.fromisoformat(prev_gen_at)
            if now - gen_dt < CACHE_TTL:
                return ({
                    "updatedAt": now_iso,
                    "generatedFrom": (forecast or {}).get("updatedAt"),
                    "model": prev.get("model", MODEL),
                    "narrative": prev["narrative"],
                    "wordCount": prev.get("wordCount",
                                          len(prev["narrative"].split())),
                    "sources": list(SOURCES),
                    "cached": True,
                    "generatedAt": prev_gen_at,
                }, prev)
        except (ValueError, TypeError):
            pass   # stale state -> regenerate

    # ----- compose prompt -----
    user_prompt = USER_PROMPT_TEMPLATE.format(
        now=now_iso,
        hotspots_block=_format_hotspots_block(forecast),
        alerts_block=_format_alerts_block(alerts),
        anomalies_block=_format_anomalies_block(anomalies),
        corroborate_block=_format_corroborate_block(corroborate),
    )

    # ----- call API (with retry inside) -----
    caller = _api_call or _call_anthropic
    try:
        resp = caller(SYSTEM_PROMPT, user_prompt, api_key=api_key)
    except Exception as e:  # noqa: BLE001
        # Surface as a soft error in the payload; never raise out of here.
        return (_empty(now_iso,
                       error=f"{type(e).__name__}: {e}"),
                prev_state or {})

    # ----- parse text -----
    text = ""
    try:
        for block in resp.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")
    except (AttributeError, TypeError):
        text = ""
    text = text.strip()
    word_count = len(text.split()) if text else 0
    model_used = resp.get("model", MODEL) if isinstance(resp, dict) else MODEL

    if not text:
        return (_empty(now_iso, error="empty response from Anthropic"),
                prev_state or {})

    payload = {
        "updatedAt": now_iso,
        "generatedFrom": (forecast or {}).get("updatedAt"),
        "model": model_used,
        "narrative": text,
        "wordCount": word_count,
        "sources": list(SOURCES),
        "cached": False,
        "generatedAt": now_iso,
    }
    new_state = {
        "hash": cur_hash,
        "narrative": text,
        "model": model_used,
        "generatedAt": now_iso,
        "wordCount": word_count,
    }
    return payload, new_state
