# sil-globe-collector

GitHub-Actions-driven data collector for the OSIRIS globe at
[social-intelligence-labs.vercel.app/globe](https://social-intelligence-labs.vercel.app/globe).

A cron workflow (`.github/workflows/tick.yml`) runs every 5 minutes,
fetches every registered layer, and PUTs the normalized blob to
Vercel Blob at `globe/<layer>.json`. The Next.js site reads those
blobs — the collector and the site are decoupled.

## Why GitHub Actions
This started life as a Mac launchd daemon (`com.sil.globe-collector`).
It worked, until DNS flapped one morning and the daemon hung silently
for 16 hours blanking the globe. GH Actions cron gives us:

- No host to maintain — no launchd, no plist secrets, no
  `Errno 8 nodename nor servname provided`
- Free + unlimited minutes (public repo)
- Built-in secret management via `gh secret set`
- Per-run isolation — a stuck layer can't deadlock the next run

## What's NOT in here
- **boats** — needs a persistent AISStream WebSocket; runs on a daemon
  elsewhere (or stays disabled until we add one).
- **intel / forecast / predict / anomalies** — analytical layers that
  depend on other layers being fresh in the same process. Possible to
  add later; for now they live in the daemon-mode of `collector.py`.

## Layers covered
~40 layers including: flights, satellites, military_air, military_naval,
markets, news, events, frontlines, cctv (28k+ cameras), wildfire,
gdacs, tornado_warnings, aurora, submarine_cables, sec_filings,
power_plants, hospitals, nuclear, military_bases, geo_zones,
sanctioned_vessels, port_congestion, ndbc_buoys, metar, tides, etc.

## Cadence
GH Actions cron minimum is 5 minutes (often delayed). High-frequency
layers (flights/satellites at 10-15s in daemon mode) become 5-min
cadence here — the trade-off for not needing a host.
