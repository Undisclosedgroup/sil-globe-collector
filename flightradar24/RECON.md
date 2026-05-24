# flightradar24 — RECON

Real-time global flight scraper for FR24's keyless JSON feed, plus per-flight
detail (airline, airport names, aircraft model, **photo**). Built to enrich the
OSIRIS globe `flights` layer far beyond the adsb.lol mil/position data.

All fetches go through `_shared/proxy_fetcher.ProxyFetcher` (ProxyRack residential,
curl_cffi chrome impersonation, IP rotation). No direct-home-IP path.

## Endpoints

| Purpose | URL | Notes |
|---|---|---|
| Live feed | `https://data-cloud.flightradar24.com/zones/fcgi/feed.js?bounds=N,S,W,E&...` | JSON object keyed by FR24 id; values are positional arrays. ~1500 flights/call cap → tile the world. |
| Flight detail + photo | `https://data-live.flightradar24.com/clickhandler/?version=1.5&flight=<fr24_id>` | Rich detail incl. `aircraft.images` (jetphotos.com URLs), `airline.name`, `airport.origin/destination`. |

### Host gotcha (the load-bearing finding)

The **documented** feed host `data-live.flightradar24.com` **soft-blocks** the
residential proxy: it returns **HTTP 200** with `{"aircraft":[],"full_count":0}`
— no 403, just an empty payload, on every IP/impersonation tried. The
`data-cloud.flightradar24.com` host serves the **real** feed through the same
proxy. Conversely `/clickhandler/` 404s on `data-cloud` and works on
`data-live`. So:

- **feed → `data-cloud`**, **detail → `data-live`**. Verified live 2026-05-21.

This is why "FR24 returns empty rows" — it's the host, not a hard block.

## Feed array layout (verified 2026-05-21)

```
[0]  modeS hex          [10] timestamp (epoch)
[1]  lat                [11] origin_iata
[2]  lng                [12] dest_iata
[3]  track (deg)        [13] flight_number
[4]  alt_ft             [14] on_ground (0/1)
[5]  speed_kt           [15] vert_rate (fpm)
[6]  squawk             [16] callsign
[7]  radar/source tag   [17] (reserved)
[8]  aircraft_type      [18] airline_icao
[9]  registration
```

## Clickhandler fields used

- `aircraft.model.text` → human aircraft model ("Boeing 777-F").
- `aircraft.registration` → tail number.
- `aircraft.images.{medium,large,thumbnails}[0].src` → photo URL (jetphotos CDN).
  FR24 sometimes double-prefixes the scheme (`https:https://…`) on the static
  sideview fallback — `_norm_photo()` strips it. Real jetphotos URLs are clean.
- `airline.name` → operator ("FedEx", "VistaJet").
- `airport.origin.name` / `airport.destination.name` → full airport names.

## Anti-bot

- FR24 fingerprints TLS (JA3) and requires a `flightradar24.com` `Referer` +
  `Origin`. Plain curl_cffi **chrome146 / chrome131** through ProxyRack passes.
- No auth/token needed for either endpoint.
- Soft-block manifests as `full_count:0`; `_fetch_tile` retries on a fresh
  rotating IP. In testing `data-cloud` answered on the first IP every time.
- **Block status: NOT blocked.** Both endpoints serve full data through
  ProxyRack residential. adsb.lol is retained only as a defensive fallback in the
  collector if FR24 ever returns empty for a whole cycle.

## Performance (measured 2026-05-21, through ProxyRack)

- Live feed: **16,261 unique flights in 3.9 s** across 44 world tiles
  (`Semaphore(8)`), unioned by FR24 id.
- Detail enrichment is heavier (one clickhandler call/flight) so the collector
  enriches a bounded subset per cycle (`enrich_subset`, default 60) and caches
  `photo_url` by registration across cycles — most repeat flights are then
  enriched for free.

## Files

- `scraper.py` — `fetch_live()` (tiled feed union), `enrich_subset()` (detail +
  photo, registration cache), `parse_feed()` / `parse_detail()` (pure parsers).
  `python3 flightradar24/scraper.py` runs a dry-run printing counts + a sample.
- `samples/feed_sample.json`, `samples/clickhandler_sample.json` — raw captures.

## Collector wiring

`osiris-globe-collector/layers.py::fetch_flights()` now **prefers FR24**: it
calls `fr24_scraper.fetch_live()` + `enrich_subset()`, maps records to the globe
point shape (keeping the commercial/private/jet/military `category`), and emits
the extra fields `airline, origin, destination, flight_number, aircraft_type,
registration, photo_url`. If FR24 yields 0 flights, it falls back to the
original adsb.lol `/v2/lat-lon` tiles + `/v2/mil` union.

## Run

```bash
cd /Users/office/Desktop/recon-out
set -a; . ~/.proxyrack.env; set +a
python3 flightradar24/scraper.py
```
