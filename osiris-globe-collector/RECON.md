# osiris-globe-collector — RECON

Persistent async collector that refreshes the OSIRIS globe's OSINT layers
through a 20-thread ProxyRack pool and uploads JSON snapshots to Vercel Blob.
A Next.js `/globe` dashboard reads only the blobs (never the upstreams).

It reuses the fetch + parse logic of the sibling `osiris-*` scrapers; nothing
here scrapes anything those don't already cover.

## Files

- `blob_put.py` — Vercel Blob REST upload helper (`build_put_request`, `put_blob`).
- `layers.py` — layer registry `LAYERS` + per-layer async `fetch()` and pure
  `normalize_*` functions returning the normalized payload contract.
- `collector.py` — async 20-worker scheduler loop.
- `tests/` — pytest unit tests for `build_put_request` + every normalizer
  (fixture-based, no network).

## Payload contract

Every layer's `fetch()` returns:

```json
{ "layer": "<id>", "updatedAt": "<ISO8601>", "count": <int>, "items": [...], "error": "<optional>" }
```

- Point layers (`flights, satellites, earthquakes, wildfire, natural-events,
  infrastructure, cctv`): `items` are `{id, lat, lng, label, color, ...extra}`.
- `frontlines`: `items` are GeoJSON `Feature[]` (Polygon control areas).
- `news`, `markets`, `cyber`: record arrays (no coordinates; ticker / panel data).

Manifest at `globe/_manifest.json`: `{ "layers": { "<id>": { updatedAt, count, error? } } }`.

## Environment

| Var | Source | Purpose |
|---|---|---|
| `PROXYRACK_*` | `~/.proxyrack.env` (auto-loaded by the shared fetcher) | residential egress for ALL fetches |
| `BLOB_READ_WRITE_TOKEN` | Vercel project → Storage → Blob | Blob upload auth (required) |
| `FIRMS_MAP_KEY` | optional | not used here (wildfire uses keyless NIFC) |

The shared `_shared/proxy_fetcher.py` **refuses to fetch on the home IP** if
ProxyRack creds are absent. The collector exits cleanly with a clear message if
`BLOB_READ_WRITE_TOKEN` is unset.

## Run

```bash
cd /Users/office/Desktop/recon-out/osiris-globe-collector
set -a; . ~/.proxyrack.env; set +a
export BLOB_READ_WRITE_TOKEN=<token from Vercel Storage → Blob>
python3 collector.py
```

Persistent (background) options:

```bash
# nohup
nohup python3 collector.py > collector.log 2>&1 &

# pm2
pm2 start collector.py --name osiris-globe --interpreter python3

# launchd / systemd: wrap the two env loads + `python3 collector.py` in the unit.
```

## Cadence table (from the design spec)

| Layer | Upstream | Interval |
|---|---|---|
| flights | **Flightradar24** live feed (rich detail+photo) — adsb.lol `/v2/mil`+tiles fallback | 10 s |
| satellites | CelesTrak GP JSON + SGP4 (positions recomputed every cycle) | 10 s |
| markets | Yahoo Finance chart API (+ Stooq CSV fallback) | 30 s |
| earthquakes | USGS GeoJSON summary feeds | 60 s |
| news | BBC + Al Jazeera + Google News RSS (+ risk score) | 5 min |
| natural-events | NASA EONET v3 | 15 min |
| wildfire | NIFC WFIGS ArcGIS (keyless US incidents) | 15 min |
| cyber | CISA KEV catalog | 30 min |
| frontlines | DeepStateMap history GeoJSON | 30 min |
| cctv | TfL + CalTrans + 511 DataTables + 511 CARS + NYCTMC + IDOT + DDOT | 6 h (semi-static) |
| infrastructure | OSM Overpass (nuclear plants) | 24 h (static) |

Satellite TLEs come from CelesTrak on the same cadence; SGP4 sub-point
propagation is CPU-only (no network) and runs every fetch so the globe shows
moving satellites.

## Safety guarantees

- **Error isolation:** a layer fetch that raises is caught per-layer; the error
  is recorded in the manifest and the loop continues. One dead upstream never
  crashes the collector.
- **Never blank a good blob:** a fetch that returns `count == 0` does NOT
  overwrite a previously-good blob (guards against transient empty upstreams
  wiping the globe). The first-ever fetch of a layer may legitimately write 0.
- **Proxy-mandatory:** every fetch goes through ProxyRack via the shared
  fetcher; no direct-home-IP path exists in this collector.

## Tests

```bash
cd /Users/office/Desktop/recon-out/osiris-globe-collector
python3 -m pytest tests/ -q
```

20 tests: `build_put_request` (path / auth / compact body) + every normalizer
against representative fixtures (incl. a real ISS GP element set propagated by
SGP4, the FR24 flights normalizer, and the AIS ship-type/country helpers).

## Fix log (2026-05-20)

- Initial build: Phase 1 Tasks 1–3 of the OSIRIS globe plan.
- `flights` upstream chosen as adsb.lol `/v2/mil` (global military snapshot,
  keyless, `ac[]` schema) with `opendata.adsb.fi/api/v2/mil` fallback — there is
  no `osiris-flights` sibling scraper; this matches the design spec's
  "adsb.lol (+ adsb.fi fallback)" and the plan's `ac`-array test fixture.

## Fix log (2026-05-21) — CCTV expansion + DataTables restore

The live cctv blob had regressed to 8 networks (~9k cams). The DataTables 511
family was never actually wired into the collector's `fetch_cctv()` (only TfL +
CalTrans + 6 CARS sites were), so FL511/GA/NY/PA/AZ/Idaho/NewEngland/LA had
"dropped out". Restored them and added city-targeted networks. All verified
live through ProxyRack 2026-05-21.

- **Restored the 511 DataTables family** (`CCTV_DT_SITES` + `_cctv_dt_site`):
  FL511 (4707), GA511 (3938), 511NY (2282), 511PA (1472), 511LA (336),
  Idaho (455), AZ511 (643), New England (413). Plain keyless POST
  `/List/GetData/Cameras`, page size 10, WKT `POINT(lng lat)`. **No cookie-warm
  needed** — they answer through the proxy directly (the regression was purely
  that they were never called). `image_url` is the relative `/map/Cctv/{n}`
  prefixed with the site base — verified it serves a real JPEG/PNG snapshot
  (FL511 → 64KB image/jpeg, 511ny → 245KB image/png).
- **NYC**: added **nyctmc** (`webcams.nyctmc.org/api/cameras`, 960 NYC-metro
  cams with refreshing `imageUrl` snapshots) in addition to 511NY. Gotcha:
  chrome146 gets CONNECT-aborted (curl 56/565) at the proxy edge for this host;
  **chrome131 / safari17_0 work** but only intermittently, so the helper retries
  across ~6 fresh rotating IPs. NYC metro: ~998 cams w/ working image_url.
- **Chicago**: added **idot_gateway** — Illinois Gateway Traffic Cameras hosted
  ArcGIS FeatureServer (`services2.arcgis.com/aIrBD8yn1TDTEXoz/.../
  TrafficCamerasTM_Public/FeatureServer/0`, ~3618 cams). `SnapShot` field is a
  direct refreshing JPG on `cctv.travelmidwest.com`. Paginated via
  `resultOffset`.
- **SF Bay**: CalTrans D4 already covers it (~629 cams w/ image). Added optional
  **511_sfbay** (`api.511.org/traffic/cameras`) which is **env-gated** — needs a
  free token in `SF_BAY_511_TOKEN` (or `FIVEONEONE_ORG_TOKEN`); skipped cleanly
  (0 cams, no error) when unset.
- **DC**: added **ddot_dc** — DDOT TrafficOperations CCTV-location ArcGIS layer
  (`maps2.dcgis.dc.gov/.../DDOT/TrafficOperations/MapServer/2`, 250 located
  cams). **image_url is null**: DDOT's live-image hosts (`cctv.ddot.dc.gov`,
  CHART `chartexp1.sha.maryland.gov`, VDOT 511) are all Cloudflare-gated /
  non-routable through the residential proxy with curl_cffi (565 / 403 /
  Angular-shell) — they'd need Camoufox (tier 4). DC is surfaced location-only
  (the globe shows the markers; `stream_url` points at the DDOT viewer page) so
  the metro is represented without emitting a known-broken JPG URL.
- **Robustness**: every network now runs inside a `_safe()` wrapper that logs a
  per-network camera count and isolates failures (logs + contributes 0, never
  crashes the cycle). DataTables POST runs via `asyncio.to_thread` (the shared
  fetcher has no `apost`).
- **Verified metro coverage (live, 2026-05-21)** with bbox checks: NYC ~998,
  LA ~908, SF ~629, Chicago ~3.6k (full)/53 (capped test), Miami 105 in the
  first 600 FL511 cams (full pull = all of FL511's 4707), Boston ~132, DC 250.
  All except DC carry working `image_url` snapshots.
- New aggregate (uncapped): ~9k baseline → **~25k+** cameras across 19 networks.

## Fix log (2026-05-21) — rich boat detail + Flightradar24 flights

**Boats (AIS static data merge).** `run_boats_task` now subscribes to BOTH
`PositionReport` AND `ShipStaticData` (`FilterMessageTypes`) and maintains one
merged record per MMSI:
- position (lat/lng/speed=`Sog`/heading=`TrueHeading`||`Cog`) from PositionReport;
- static (`Name`, `Type`→human label, `Destination`, `MaximumStaticDraught`,
  `Dimension`→size `LxWm`, `ImoNumber`, `CallSign`, `Eta`) from ShipStaticData.
- FLAG/country derived from the MMSI MID (first 3 digits → ITU maritime country →
  name + ISO alpha-2) via `ais_country_from_mmsi`.

`normalize_boats` now emits `{id, lat, lng, label, name, flag, country_code,
ship_type, destination, draught, size, callsign, imo, eta, speed, heading,
color:"#48dbfb"}`. 30 s snapshot cadence + 20 k cap unchanged. The expired-cert
insecure-TLS fallback is unchanged (AISStream's leaf cert is still expired).
Verified live: ~5.1 k vessels in 25 s, flags + ship-type labels + destinations
populating (Canada/Mexico/Norway/Sweden/NL; Cargo/Tanker/Passenger/Fishing/...).

> **AIS has NO cargo manifest.** There is no field for what a vessel is actually
> carrying. `ship_type` (Cargo / Tanker / Passenger / Tug / Fishing / Pleasure /
> HSC / Military / ...) is the best available proxy for "what it's carrying" and
> is included as such. We do **not** fabricate cargo contents.

**Flights (Flightradar24).** `fetch_flights` now **prefers FR24**
(`recon-out/flightradar24/scraper.py`) and falls back to adsb.lol only if FR24
returns an empty cycle. FR24 records carry the existing point shape PLUS
`airline, origin, destination, origin_name, dest_name, flight_number,
aircraft_type, aircraft_model, registration, photo_url, callsign, source` and
keep the commercial/private/jet/military `category`. Photos come from
clickhandler (`enrich_subset`, bounded subset/cycle, registration→photo cache
across cycles). Verified live: **16.2 k flights via FR24 in ~4 s**, ~11.5 k with
origin+dest from the feed alone, sample enriched with airline + jetphotos photo.

> **FR24 block status: NOT blocked through ProxyRack.** The catch is the HOST:
> the documented `data-live.flightradar24.com` feed host soft-blocks the proxy
> (HTTP 200 + `full_count:0`); `data-cloud.flightradar24.com` serves the real
> feed; clickhandler detail must use `data-live`. See `flightradar24/RECON.md`.

Env var names (unchanged): `AISSTREAM_API_KEY` (boats websocket),
`PROXYRACK_*` (egress, auto-loaded), `BLOB_READ_WRITE_TOKEN` (blob upload).
