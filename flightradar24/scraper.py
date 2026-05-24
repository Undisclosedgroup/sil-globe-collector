"""Flightradar24 real-time scraper — global live flight feed + per-flight detail.

Two endpoints, both keyless JSON, both fetched THROUGH ProxyRack via the shared
`_shared/proxy_fetcher.ProxyFetcher` (curl_cffi chrome impersonation + browser
Referer/Origin + IP rotation):

1. LIVE FEED  — `https://data-cloud.flightradar24.com/zones/fcgi/feed.js?bounds=N,S,W,E&...`
   Returns a JSON object keyed by FR24 flight id; each value is a positional
   array (see FEED_FIELDS). We tile the world into bounding boxes (FR24 caps
   ~1500 flights per call) and union by FR24 id.

   IMPORTANT host gotcha: the documented `data-live.flightradar24.com` host
   SOFT-BLOCKS residential-proxy IPs — it returns HTTP 200 with
   `{"aircraft":[],"full_count":0}` (no 403, just empty). `data-cloud.…` serves
   the real feed through the same proxy. So: feed -> data-cloud, detail ->
   data-live (data-cloud 404s on /clickhandler/). Verified 2026-05-21.

2. FLIGHT DETAIL + PHOTO — `https://data-live.flightradar24.com/clickhandler/?version=1.5&flight=<fr24_id>`
   Rich detail: aircraft.model, aircraft.images (jetphotos.com photo URLs),
   airline.name, airport.origin/destination names. Heavier; we enrich a bounded
   subset per cycle and cache photo_url by registration.

Anti-bot: FR24 fingerprints TLS + checks Referer/Origin. curl_cffi chrome146/131
through residential ProxyRack passes; rotate IPs and retry on the rare 403/empty.

Run standalone (dry-run, prints counts + a sample enriched flight):
    cd /Users/office/Desktop/recon-out
    set -a; . ~/.proxyrack.env; set +a
    python3 flightradar24/scraper.py
"""
import sys, json, asyncio, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))
from proxy_fetcher import fetcher  # noqa: E402

# Feed host serves the real payload through residential proxy; detail host serves
# clickhandler. (They are NOT interchangeable — see module docstring.)
FEED_HOST = "https://data-cloud.flightradar24.com"
DETAIL_HOST = "https://data-live.flightradar24.com"

FEED_PARAMS = ("faa=1&satellite=1&mlat=1&flarm=1&adsb=1&gnd=1&air=1&vehicles=1"
               "&estimated=1&maxage=14400&gliders=1&stats=0")

# Browser-y headers — FR24 rejects requests without a flightradar24.com Referer/Origin.
FR24_HDR = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.flightradar24.com/",
    "Origin": "https://www.flightradar24.com",
}

# Positional layout of each feed array value (verified live 2026-05-21).
# Index -> field. Trailing indices (17,18) vary; 18 is the airline ICAO code.
FEED_FIELDS = {
    0: "modeS", 1: "lat", 2: "lng", 3: "track", 4: "alt_ft", 5: "speed_kt",
    6: "squawk", 7: "radar", 8: "aircraft_type", 9: "registration",
    10: "timestamp", 11: "origin_iata", 12: "dest_iata", 13: "flight_number",
    14: "on_ground", 15: "vert_rate", 16: "callsign", 18: "airline_icao",
}

# World grid of bounding boxes "north,south,west,east". Busy regions are tiled
# finely; the rest of the globe is covered by coarse tiles so nothing is missed.
# FR24 caps ~1500 flights/call, so finer tiles over dense airspace = more flights.
WORLD_TILES = [
    # North America — Canada / high-latitude (sparse, coarse is fine)
    "72,49,-141,-95",
    # Western / central US — split so LA, SF, Vegas, Denver, Phoenix aren't thinned.
    "49,40,-130,-114",   # Pacific NW / N California / Idaho
    "40,32,-125,-114",   # SF Bay + LA/SoCal corridor (very dense)
    "49,40,-114,-95",    # N Rockies / N plains (Denver north, Minneapolis)
    "40,31,-114,-103",   # Phoenix / Vegas / Utah / NM
    "40,29,-103,-95",    # Texas (Dallas/Houston) + Oklahoma
    # Eastern US — finely gridded; this was the single "49,37,-95,-66" tile that
    # capped out and starved the NYC/BOS/DC/Chicago/ATL metros.
    "44,41,-89,-84",     # Great Lakes / Chicago
    "44,40,-84,-78",     # Ohio valley / Detroit / Pittsburgh
    "43,42,-72,-70",     # Boston
    "42,40,-75,-72",     # NYC corridor (JFK/LGA/EWR — densest US airspace)
    "41,38,-78,-74",     # DC / Baltimore / Philadelphia
    "49,42,-78,-66",     # Upstate NY / New England / Atlantic NE fill
    "41,37,-95,-89",     # Mid-Mississippi (St. Louis / Kansas City)
    "41,36,-89,-78",     # mid-Atlantic / Appalachia / Ohio-valley south fill
    "37,33,-85,-82",     # Atlanta / Southeast
    "38,36,-82,-66",     # Carolinas / Virginia coast fill
    # Southern-central US — was "37,24,-125,-95" (capped at 1500); split E/W so
    # San Diego / S Texas / border airspace aren't thinned.
    "37,24,-125,-110",   # SoCal south / Baja border / SW Arizona
    "37,24,-110,-95",    # S Texas / S NM / N Mexico border
    # Southeast / Florida / Gulf — split so Miami, Orlando, Tampa aren't thinned.
    "33,28,-85,-77",     # Georgia / N Florida / Carolinas coast
    "28,24,-85,-79",     # S Florida (Miami / Orlando / Tampa — dense)
    "33,29,-95,-85",     # Gulf coast (New Orleans / Mobile / Tallahassee)
    "24,7,-118,-77",
    # Europe (very dense — fine grid)
    "60,52,-11,5", "60,52,5,20", "60,52,20,32", "52,45,-11,5", "52,45,5,20",
    "52,45,20,32", "45,36,-11,5", "45,36,5,20", "45,36,20,40", "72,60,5,40",
    # Middle East
    "40,24,32,48", "40,24,48,63",
    # Africa
    "36,15,-18,15", "36,15,15,44", "15,-10,-18,20", "15,-10,20,52",
    "-10,-35,10,40",
    # Asia (dense)
    "55,40,40,75", "40,24,63,90", "55,28,90,120", "40,20,100,123",
    "28,5,68,90", "28,5,90,110", "24,-10,95,120", "10,-10,100,130",
    # East Asia / Japan / Korea
    "46,30,123,146", "55,30,123,160",
    # Oceania
    "-10,-45,110,155", "-10,-50,150,179",
    # South America
    "12,-15,-82,-55", "-15,-40,-75,-50", "-40,-56,-76,-53",
    # Oceans / coarse global catch-alls. The original "40,-56,-180,-95" catch-all
    # (96° × 85° band over the eastern Pacific + Mexico/CenAm + S-America Pacific
    # coast + Antarctic Pacific) hit the per-call ~1500 cap exactly, suppressing
    # flights in that band. Split into 4 quadrants so the cap can't bite.
    "72,40,-180,-130", "72,40,-60,-11",
    "40,-8,-180,-130",   # N Pacific (Hawaii / equatorial Pacific)
    "40,-8,-130,-95",    # Mexico Pacific coast + Central America Pacific
    "-8,-56,-180,-130",  # South Pacific + Southern Ocean (west half)
    "-8,-56,-130,-95",   # S-America Pacific coast (Chile/Peru/Ecuador)
    "40,-56,-55,-18",
    "0,-56,40,180", "72,40,160,180",
]

# A registration -> photo_url cache survives across cycles (photos are static
# per airframe), so we don't re-hit clickhandler for the same plane.
_photo_cache: dict = {}

_F = fetcher(impersonate="chrome146")


def _norm_photo(src):
    """FR24 occasionally double-prefixes the scheme ('https:https://...').
    Normalize to a single clean URL; drop obviously broken values."""
    if not src or not isinstance(src, str):
        return None
    if src.startswith("https:https://") or src.startswith("http:https://"):
        src = src[src.index("https://"):]
    if src.startswith("//"):
        src = "https:" + src
    return src if src.startswith("http") else None


def parse_feed(raw: dict) -> dict:
    """Parse one feed.js response into {fr24_id: {parsed fields}}."""
    out = {}
    for fid, v in raw.items():
        if fid in ("full_count", "version", "stats") or not isinstance(v, list):
            continue
        rec = {}
        for idx, name in FEED_FIELDS.items():
            rec[name] = v[idx] if idx < len(v) else None
        if rec.get("lat") is None or rec.get("lng") is None:
            continue
        rec["fr24_id"] = fid
        out[fid] = rec
    return out


async def _fetch_tile(bounds: str, tries: int = 2, timeout: int = 6) -> dict:
    """Fetch one bounding-box tile; retry once on empty/403 with a fresh IP.

    Tight timeout/retry budget: the bulk feed is the near-real-time path and must
    finish well under the flights cycle's ~8s wall-clock cap, so a slow tile is
    abandoned quickly rather than dragging the whole cycle out.
    """
    url = f"{FEED_HOST}/zones/fcgi/feed.js?bounds={bounds}&{FEED_PARAMS}"
    for _ in range(tries):
        r = await _F.aget(url, headers=FR24_HDR, timeout=timeout)
        if r.tier == "refused_no_proxy":
            raise RuntimeError("proxy not configured (refused_no_proxy)")
        if r.status == 200 and len(r.body) > 60:
            try:
                d = json.loads(r.body)
            except Exception:
                continue
            # full_count==0 with no flights == soft-block; retry on fresh IP.
            parsed = parse_feed(d)
            if parsed or d.get("full_count"):
                return parsed
        # back off briefly; the shared fetcher rotates the proxy session.
        await asyncio.sleep(0.3)
    return {}


async def fetch_live(tiles=None, deadline: float = 6.5) -> dict:
    """Fetch all world tiles concurrently, union by FR24 id. Returns
    {fr24_id: parsed_record}. Concurrency is high (all tiles in flight at once,
    capped at 16) and the whole gather is wall-clock-bounded so the bulk feed
    stays near-real-time even if a handful of tiles are slow."""
    tiles = tiles or WORLD_TILES
    sem = asyncio.Semaphore(16)

    async def _one(b):
        async with sem:
            try:
                return await _fetch_tile(b)
            except RuntimeError:
                raise
            except Exception:
                return {}

    merged = {}
    tasks = [asyncio.ensure_future(_one(b)) for b in tiles]
    try:
        chunks = await asyncio.wait_for(asyncio.gather(*tasks), timeout=deadline)
    except asyncio.TimeoutError:
        # Bulk feed deadline hit — take whatever tiles finished, drop the rest.
        chunks = []
        for t in tasks:
            if t.done() and not t.cancelled():
                try:
                    chunks.append(t.result())
                except Exception:
                    pass
            else:
                t.cancel()
    for chunk in chunks:
        if isinstance(chunk, dict):
            for fid, rec in chunk.items():
                merged.setdefault(fid, rec)
    return merged


def parse_detail(cd: dict) -> dict:
    """Extract enrichable fields from a clickhandler response."""
    ac = cd.get("aircraft") or {}
    model = ac.get("model") or {}
    airline = cd.get("airline") or {}
    ap = cd.get("airport") or {}
    org = ap.get("origin") or {}
    dst = ap.get("destination") or {}
    imgs = ac.get("images") or {}
    photo = None
    if isinstance(imgs, dict):
        for sz in ("medium", "large", "thumbnails"):
            arr = imgs.get(sz) or []
            if arr and isinstance(arr[0], dict):
                photo = _norm_photo(arr[0].get("src"))
                if photo:
                    break
    return {
        "airline": airline.get("name"),
        "aircraft_model": model.get("text") if isinstance(model, dict) else None,
        "registration": ac.get("registration"),
        "origin_name": org.get("name"),
        "dest_name": dst.get("name"),
        "photo_url": photo,
    }


async def fetch_detail(fr24_id: str, tries: int = 1, timeout: int = 4):
    """Fetch + parse one flight's clickhandler detail (incl. photo).

    Tight defaults (1 try, 4s timeout) because this runs inside a time-boxed
    rolling-enrichment budget — a slow detail call must never stall the cycle.
    """
    url = f"{DETAIL_HOST}/clickhandler/?version=1.5&flight={fr24_id}"
    for _ in range(tries):
        r = await _F.aget(url, headers=FR24_HDR, timeout=timeout)
        if r.tier == "refused_no_proxy":
            raise RuntimeError("proxy not configured (refused_no_proxy)")
        if r.status == 200 and r.body[:1] in (b"{", b"["):
            try:
                return parse_detail(json.loads(r.body))
            except Exception:
                return None
        await asyncio.sleep(0.2)
    return None


def apply_photo_cache(flights: dict) -> None:
    """Stamp every flight that has a cached photo (by registration) with it.
    Free — no network. Lets photo coverage grow across cycles without re-fetch."""
    for f in flights.values():
        if not f.get("photo_url") and f.get("registration") in _photo_cache:
            f["photo_url"] = _photo_cache[f["registration"]]


async def enrich_subset(flights: dict, limit: int = 25,
                        time_budget: float = 2.0) -> int:
    """Enrich a SMALL rolling subset of flights with clickhandler detail (photo +
    airline + airport names), reusing the registration->photo cache so coverage
    grows across cycles. Strictly time-boxed: stops issuing new detail calls once
    `time_budget` seconds elapse, so it can never stall the near-real-time cycle.

    Mutates `flights` in place. Returns the number of NEW photos fetched.
    """
    # First, freely stamp cached photos onto this cycle's flights (no network).
    apply_photo_cache(flights)
    todo = [f for f in flights.values()
            if f.get("registration") and f["registration"] not in _photo_cache][:limit]
    if not todo:
        return 0
    sem = asyncio.Semaphore(6)
    deadline = time.monotonic() + time_budget
    photos = 0

    async def _one(f):
        nonlocal photos
        if time.monotonic() >= deadline:   # budget spent — skip remaining work
            return
        async with sem:
            if time.monotonic() >= deadline:
                return
            det = await fetch_detail(f["fr24_id"])
            if not det:
                return
            f.update({k: v for k, v in det.items() if v})
            if det.get("photo_url") and f.get("registration"):
                _photo_cache[f["registration"]] = det["photo_url"]
                photos += 1

    try:
        await asyncio.wait_for(
            asyncio.gather(*(_one(f) for f in todo)), timeout=time_budget + 1.0)
    except asyncio.TimeoutError:
        pass  # hard ceiling — whatever enriched in time stays, rest deferred.
    # Apply any photos cached this cycle to other flights sharing a registration.
    apply_photo_cache(flights)
    return photos


async def _dry_run():
    t0 = time.time()
    flights = await fetch_live()
    print(f"FR24 live feed: {len(flights)} unique flights in "
          f"{time.time()-t0:.1f}s across {len(WORLD_TILES)} tiles")
    photos = await enrich_subset(flights, limit=25, time_budget=2.0)
    print(f"enriched a subset; {photos} new photos fetched, "
          f"cache size {len(_photo_cache)}, total enrich time-boxed at 2s")
    # Print a sample fully-enriched flight.
    sample = next((f for f in flights.values()
                   if f.get("airline") and f.get("photo_url")), None)
    if sample:
        keys = ("fr24_id", "callsign", "flight_number", "airline", "registration",
                "aircraft_type", "aircraft_model", "origin_iata", "origin_name",
                "dest_iata", "dest_name", "lat", "lng", "alt_ft", "speed_kt",
                "track", "photo_url")
        print(json.dumps({k: sample.get(k) for k in keys}, indent=1))


if __name__ == "__main__":
    asyncio.run(_dry_run())
