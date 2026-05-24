"""Phase-E backtest: compare kinetic_detector v1 vs v2.

Run modes:
  python3 _backtest_kinetic.py --synthetic     # synthetic scenarios only (fast, deterministic)
  python3 _backtest_kinetic.py --prod-snapshot # pull live production blobs, run both
  python3 _backtest_kinetic.py --all           # both (default)

Output: a comparison table to stdout + a markdown file alongside the run log.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

import kinetic_detector as v1
import kinetic_detector_v2 as v2

OUT_DIR = Path(__file__).resolve().parent / "_kinetic_backtest_out"
OUT_DIR.mkdir(exist_ok=True)

API = "https://social-intelligence-labs.vercel.app/api/globe"

# Layers the detector actually reads.
KINETIC_LAYERS = (
    "flights",
    "military_air",
    "military_naval",
    "cctv",
    "notams",
    "frontlines",
    "nav_warnings",
    "internet_outages",
    "dark_fleet",
    "hurricanes",
    "tornado_warnings",
    "tsunami",
    "natural-events",
    "earthquakes",
    "gdacs",
    "wildfire",
    "volcanoes",
)


def _fetch_layer(layer: str) -> dict | None:
    try:
        with urllib.request.urlopen(f"{API}/{layer}", timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"  warn: {layer} fetch failed: {e}", file=sys.stderr)
        return None


def fetch_prod_snapshot() -> dict:
    """Pull all kinetic-relevant layers from production into a layers dict."""
    print("[backtest] fetching production layer snapshot...", flush=True)
    out: dict = {}
    for lid in KINETIC_LAYERS:
        b = _fetch_layer(lid)
        if b is not None:
            out[lid] = b
    print(f"[backtest] got {len(out)}/{len(KINETIC_LAYERS)} layers")
    return out


# --- Synthetic scenarios ----------------------------------------------------
def _flight(lat, lng, category="commercial"):
    return {"lat": lat, "lng": lng, "category": category}


def _entity(lat, lng, **extra):
    d = {"lat": lat, "lng": lng}
    d.update(extra)
    return d


def synth_airspace_denial():
    """Cell (35, 51) Tehran-ish: 200 civ flights drop to 30 + 5 mil air + NOTAM."""
    prev = {
        "flights": {"items": [_flight(35.0 + (i % 10) * 0.01, 51.0 + (i % 10) * 0.01) for i in range(200)]},
        "military_air": {"items": []},
    }
    now = {
        "flights": {"items": [_flight(35.0 + (i % 10) * 0.01, 51.0 + (i % 10) * 0.01) for i in range(30)]},
        "military_air": {"items": [_flight(35.0, 51.0, "military") for _ in range(5)]},
        "notams": {"items": [{"lat": 35.0, "lng": 51.0, "radius_km": 200}]},
    }
    return prev, now


def synth_surveillance_blackout():
    """5 CCTV cams go dark + outage + military rises."""
    cams = [_entity(40.7, -74.0, id=f"cam-{i}") for i in range(10)]
    prev = {
        "cctv": {"items": cams},
        "military_air": {"items": []},
    }
    now = {
        "cctv": {"items": cams[:5]},  # 5 went silent
        "military_air": {"items": [_flight(40.7, -74.0, "military") for _ in range(3)]},
        "internet_outages": {"items": [_entity(40.7, -74.0, country_code="US", label="ATT major outage")]},
    }
    return prev, now


def synth_quiet():
    """Boring background — should produce no insights v1 or v2."""
    prev = {
        "flights": {"items": [_flight(40.7, -74.0) for _ in range(150)]},
    }
    now = {
        "flights": {"items": [_flight(40.7, -74.0) for _ in range(148)]},
    }
    return prev, now


SYNTH = [
    ("airspace_denial",       synth_airspace_denial),
    ("surveillance_blackout", synth_surveillance_blackout),
    ("quiet_background",      synth_quiet),
]


# --- Comparison utility -----------------------------------------------------
def _run(mod, layers, prev_layers, prev_state=None, prev_insights=None, *, now=None):
    return mod.detect_kinetic_insights(layers, prev_layers, prev_state, prev_insights, now=now)


def _summary(payload: dict) -> dict:
    items = payload.get("items") or []
    sigs = {}
    for it in items:
        s = it.get("signature", "?")
        sigs[s] = sigs.get(s, 0) + 1
    confs = [it.get("confidence", 0) for it in items if isinstance(it.get("confidence"), (int, float))]
    return {
        "count": payload.get("count"),
        "new": payload.get("new"),
        "by_sig": sigs,
        "max_conf": max(confs) if confs else 0.0,
        "mean_conf": (sum(confs) / len(confs)) if confs else 0.0,
    }


def run_synthetic():
    rows = []
    for name, fn in SYNTH:
        prev, now = fn()
        # warmup: run v2 a few times with the SAME prev so the ToD bucket
        # accumulates enough samples for Z-score gating to engage
        v2_state = None
        v1_state = None
        for warm in range(8):
            v1_payload, v1_state = _run(v1, prev, None, v1_state, None,
                                         now=datetime(2026, 5, 23, 12, warm * 5, 0, tzinfo=timezone.utc))
            v2_payload, v2_state = _run(v2, prev, None, v2_state, None,
                                         now=datetime(2026, 5, 23, 12, warm * 5, 0, tzinfo=timezone.utc))
        # the onset cycle
        v1_p, _ = _run(v1, now, prev, v1_state, v1_payload,
                       now=datetime(2026, 5, 23, 12, 45, 0, tzinfo=timezone.utc))
        v2_p, _ = _run(v2, now, prev, v2_state, v2_payload,
                       now=datetime(2026, 5, 23, 12, 45, 0, tzinfo=timezone.utc))
        rows.append({
            "scenario": name,
            "v1": _summary(v1_p),
            "v2": _summary(v2_p),
        })
    return rows


def run_prod_snapshot():
    """One-snapshot run — exercises both detectors against live blobs.
    Without a temporal pair we can't trigger delta-based signatures, but
    we can verify cold-start behavior + that v2 doesn't crash on real data."""
    layers = fetch_prod_snapshot()
    if not layers:
        return [{"scenario": "prod_snapshot", "v1": {"error": "no layers"}, "v2": {"error": "no layers"}}]
    v1_p, _ = _run(v1, layers, None, None, None)
    v2_p, _ = _run(v2, layers, None, None, None)
    return [{
        "scenario": "prod_snapshot",
        "v1": _summary(v1_p),
        "v2": _summary(v2_p),
    }]


def _render(rows: list[dict]) -> str:
    out = ["# kinetic_detector v1 vs v2 — backtest", "", f"_Run at {datetime.now(timezone.utc).isoformat()}_", ""]
    out.append("| Scenario | v1 count | v1 sigs | v1 max conf | v2 count | v2 sigs | v2 max conf |")
    out.append("|---|--:|---|--:|--:|---|--:|")
    for r in rows:
        v1s = r["v1"]; v2s = r["v2"]
        out.append(
            f"| {r['scenario']} | {v1s.get('count','-')} | "
            f"{v1s.get('by_sig',{})} | {v1s.get('max_conf',0):.2f} | "
            f"{v2s.get('count','-')} | {v2s.get('by_sig',{})} | "
            f"{v2s.get('max_conf',0):.2f} |"
        )
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--prod-snapshot", action="store_true")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    if not (args.synthetic or args.prod_snapshot or args.all):
        args.all = True

    rows = []
    if args.synthetic or args.all:
        print("[backtest] synthetic scenarios...")
        rows += run_synthetic()
    if args.prod_snapshot or args.all:
        print("[backtest] live production snapshot...")
        rows += run_prod_snapshot()

    md = _render(rows)
    out = OUT_DIR / f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H%MZ')}_backtest.md"
    out.write_text(md)
    print()
    print(md)
    print()
    print(f"[backtest] wrote {out}")


if __name__ == "__main__":
    main()
