import json, pathlib, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from layers import (
    normalize_earthquakes, normalize_flights, normalize_cctv,
    normalize_satellites, normalize_wildfire, normalize_natural_events,
    normalize_frontlines, normalize_infrastructure, normalize_cyber,
    normalize_news, normalize_markets, normalize_boats,
    normalize_flights_fr24,
    normalize_military_air, normalize_events, normalize_nav_warnings,
    normalize_military_bases, normalize_military_naval,
)


def _usgs_fixture():
    return {"features": [{"id": "x", "geometry": {"coordinates": [-117.8, 38.1, 1.7]},
            "properties": {"mag": 2.6, "place": "NV", "time": 1779301024105, "url": "u",
                           "tsunami": 0, "alert": None}}]}


def test_normalize_earthquakes_shape():
    p = normalize_earthquakes(_usgs_fixture())
    assert p["layer"] == "earthquakes" and p["count"] == 1
    it = p["items"][0]
    assert {"id", "lat", "lng", "label", "magnitude"} <= it.keys()
    assert it["lat"] == 38.1 and it["lng"] == -117.8


def test_normalize_flights_uses_adsb_ac_array():
    raw = {"ac": [{"hex": "a6", "flight": "ASA519 ", "lat": 59.1, "lon": -141.5,
                   "alt_baro": 10973, "t": "B738", "r": "N528AS"}]}
    p = normalize_flights(raw)
    assert p["count"] == 1
    it = p["items"][0]
    assert it["id"] == "a6" and it["lat"] == 59.1 and it["lng"] == -141.5 and it["label"] == "ASA519"
    assert it["category"] == "commercial"  # airliner -> default bucket


def test_normalize_flights_skips_positionless():
    raw = {"ac": [{"hex": "n1", "lat": None, "lon": None}]}
    assert normalize_flights(raw)["count"] == 0


def test_normalize_flights_categories_and_counts():
    raw = {"ac": [
        {"hex": "c1", "lat": 1, "lon": 1, "t": "A320"},                 # commercial
        {"hex": "j1", "lat": 2, "lon": 2, "t": "GLF6"},                 # jet (set)
        {"hex": "j2", "lat": 3, "lon": 3, "t": "C560"},                 # jet (prefix C5)
        {"hex": "p1", "lat": 4, "lon": 4, "t": "C172"},                 # private (set)
        {"hex": "p2", "lat": 5, "lon": 5, "t": "PA28"},                 # private (prefix PA)
        {"hex": "m1", "lat": 6, "lon": 6, "t": "F16", "dbFlags": 1},    # military (bit)
        {"hex": "m2", "lat": 7, "lon": 7, "t": "C130"},                 # military (mil feed)
    ], "mil_hexes": ["M2"]}
    p = normalize_flights(raw)
    cats = {it["id"]: it["category"] for it in p["items"]}
    assert cats == {"c1": "commercial", "j1": "jet", "j2": "jet",
                    "p1": "private", "p2": "private",
                    "m1": "military", "m2": "military"}
    assert p["counts_by_category"] == {
        "commercial": 1, "private": 2, "jet": 2, "military": 2}


def test_normalize_flights_fr24_shape_and_enrichment():
    flights = {
        # commercial: airline icao + flight number, enriched w/ airline + photo.
        "fa1": {"fr24_id": "fa1", "lat": 51.0, "lng": -0.9, "track": 87,
                "alt_ft": 39000, "speed_kt": 515, "aircraft_type": "B77L",
                "registration": "N841FD", "origin_iata": "MEM", "dest_iata": "CGN",
                "flight_number": "FX4", "callsign": "FDX4", "airline_icao": "FDX",
                "airline": "FedEx", "aircraft_model": "Boeing 777-F",
                "origin_name": "Memphis Intl", "dest_name": "Cologne Bonn",
                "photo_url": "https://cdn.jetphotos.com/x.jpg"},
        # jet: bizjet type designator.
        "j1": {"fr24_id": "j1", "lat": 1, "lng": 1, "aircraft_type": "GLF5",
               "airline_icao": "", "flight_number": ""},
        # military: operator ICAO in the mil set.
        "m1": {"fr24_id": "m1", "lat": 2, "lng": 2, "aircraft_type": "C17",
               "airline_icao": "RCH", "callsign": "RCH123"},
        # positionless -> skipped.
        "x1": {"fr24_id": "x1", "lat": None, "lng": None},
    }
    p = normalize_flights_fr24(flights)
    assert p["layer"] == "flights" and p["source"] == "fr24" and p["count"] == 3
    by_id = {it["id"]: it for it in p["items"]}
    com = by_id["fa1"]
    assert com["category"] == "commercial" and com["airline"] == "FedEx"
    assert com["origin"] == "MEM" and com["destination"] == "CGN"
    assert com["flight_number"] == "FX4" and com["registration"] == "N841FD"
    assert com["photo_url"] == "https://cdn.jetphotos.com/x.jpg"
    assert com["aircraft_type"] == "B77L" and com["aircraft_model"] == "Boeing 777-F"
    assert by_id["j1"]["category"] == "jet"
    assert by_id["m1"]["category"] == "military"
    assert p["counts_by_category"]["commercial"] == 1
    assert p["counts_by_category"]["jet"] == 1
    assert p["counts_by_category"]["military"] == 1


def test_normalize_boats_shape():
    # 235... MID -> United Kingdom (GB); Type 70 -> Cargo; full static merge.
    raw = [{"mmsi": 235012345, "lat": 51.5, "lng": -0.1, "name": "TEST VESSEL ",
            "ship_type": 70, "destination": "ROTTERDAM ", "draught": 8.4,
            "dimension": {"A": 100, "B": 50, "C": 10, "D": 12}, "imo": 9876543,
            "callsign": "GABC", "eta": "06-01 14:30", "speed": 12.3,
            "heading": 270, "ts": 1779301024},
           {"mmsi": 456, "lat": None, "lng": 5.0}]  # skipped (no lat)
    p = normalize_boats(raw)
    assert p["layer"] == "boats" and p["count"] == 1
    it = p["items"][0]
    assert it["id"] == 235012345 and it["lat"] == 51.5 and it["label"] == "TEST VESSEL"
    assert it["name"] == "TEST VESSEL"
    assert it["flag"] == "United Kingdom" and it["country_code"] == "GB"
    assert it["ship_type"] == "Cargo"
    assert it["destination"] == "ROTTERDAM" and it["draught"] == 8.4
    assert it["size"] == "150x22m" and it["imo"] == 9876543
    assert it["callsign"] == "GABC" and it["speed"] == 12.3 and it["heading"] == 270
    assert it["color"] == "#48dbfb"


def test_ais_ship_type_and_country_helpers():
    from layers import ais_ship_type_label, ais_country_from_mmsi
    assert ais_ship_type_label(70) == "Cargo"
    assert ais_ship_type_label(80) == "Tanker"
    assert ais_ship_type_label(60) == "Passenger"
    assert ais_ship_type_label(30) == "Fishing"
    assert ais_ship_type_label(52) == "Tug"
    assert ais_ship_type_label(0) is None
    assert ais_country_from_mmsi(366981910) == ("United States", "US")
    assert ais_country_from_mmsi(257047800) == ("Norway", "NO")
    assert ais_country_from_mmsi(None) == (None, None)


def test_normalize_cctv_passthrough_recon_records():
    raw = [{"network": "tfl", "camera_id": "c1", "name": "A406", "lat": 51.6,
            "lng": -0.01, "image_url": "i"}]
    p = normalize_cctv(raw)
    assert p["count"] == 1 and p["items"][0]["id"] == "c1" and p["items"][0]["lat"] == 51.6


def test_normalize_satellites_computes_positions():
    # One real GP element set (ISS); sgp4 must compute a sub-point.
    raw = [{
        "OBJECT_NAME": "ISS (ZARYA)", "NORAD_CAT_ID": 25544, "OBJECT_ID": "1998-067A",
        "EPOCH": "2026-05-19T12:00:00.000000", "MEAN_MOTION": 15.50103472,
        "ECCENTRICITY": 0.0007976, "INCLINATION": 51.6416, "RA_OF_ASC_NODE": 247.4627,
        "ARG_OF_PERICENTER": 130.5360, "MEAN_ANOMALY": 325.0288, "BSTAR": 0.00016717,
        "MEAN_MOTION_DOT": 0.00002182, "MEAN_MOTION_DDOT": 0.0,
    }]
    p = normalize_satellites(raw)
    assert p["layer"] == "satellites" and p["count"] == 1
    it = p["items"][0]
    assert it["id"] == 25544 and it["label"] == "ISS (ZARYA)"
    assert -90 <= it["lat"] <= 90 and -180 <= it["lng"] <= 180
    assert it["alt"] is not None and it["alt"] > 100  # ISS ~400 km


def test_normalize_wildfire_nifc_geojson():
    raw = {"features": [{"properties": {"IncidentName": "Test Fire", "IncidentSize": 120,
            "PercentContained": 30, "POOState": "US-CA"},
            "geometry": {"coordinates": [-120.1, 38.5]}}]}
    p = normalize_wildfire(raw)
    assert p["count"] == 1
    it = p["items"][0]
    assert it["lat"] == 38.5 and it["lng"] == -120.1 and it["label"] == "Test Fire"


def test_normalize_natural_events_uses_last_geometry():
    raw = {"events": [{"id": "EONET_1", "title": "Volcano X",
            "categories": [{"title": "Volcanoes"}],
            "geometry": [{"coordinates": [10.0, 20.0], "date": "2026-05-19T00:00:00Z"}],
            "sources": [{"url": "s"}]}]}
    p = normalize_natural_events(raw)
    assert p["count"] == 1
    it = p["items"][0]
    assert it["lat"] == 20.0 and it["lng"] == 10.0 and it["label"] == "Volcano X"


def test_normalize_natural_events_skips_polygon_only():
    raw = {"events": [{"id": "p", "title": "Storm", "categories": [],
            "geometry": [{"coordinates": [[[1, 2], [3, 4]]]}]}]}
    assert normalize_natural_events(raw)["count"] == 0


def test_normalize_frontlines_keeps_geojson_features():
    feat = {"type": "Feature", "id": 7,
            "properties": {"name": {"en": "Sector A"}},
            "geometry": {"type": "Polygon", "coordinates": [[[37.0, 48.0], [37.1, 48.1]]]}}
    p = normalize_frontlines({"type": "FeatureCollection", "features": [feat]})
    assert p["layer"] == "frontlines" and p["count"] == 1
    out = p["items"][0]
    assert out["type"] == "Feature"
    assert out["geometry"]["type"] == "Polygon"
    assert out["properties"]["name"] == "Sector A"


def test_normalize_infrastructure_overpass_elements():
    raw = {"elements": [
        {"type": "node", "id": 1, "lat": 47.5, "lon": 34.5,
         "tags": {"name": "Zaporizhzhia NPP", "operator": "Energoatom"}},
        {"type": "way", "id": 2, "center": {"lat": 51.4, "lon": -0.1},
         "tags": {"name:en": "Plant B"}},
    ]}
    p = normalize_infrastructure(raw)
    assert p["count"] == 2
    assert p["items"][0]["lat"] == 47.5 and p["items"][0]["label"] == "Zaporizhzhia NPP"
    assert p["items"][1]["lat"] == 51.4 and p["items"][1]["label"] == "Plant B"


def test_normalize_cyber_record_array():
    raw = {"vulnerabilities": [
        {"cveID": "CVE-2026-1", "vendorProject": "Acme", "product": "Widget",
         "vulnerabilityName": "RCE", "dateAdded": "2026-05-01",
         "knownRansomwareCampaignUse": "Known"}]}
    p = normalize_cyber(raw)
    assert p["layer"] == "cyber" and p["count"] == 1
    it = p["items"][0]
    assert it["id"] == "CVE-2026-1" and it["label"] == "RCE"


def test_normalize_news_record_array_with_risk():
    raw = [{"source_feed": "BBC World", "title": "Strike kills", "link": "l",
            "published": "p", "summary": "s", "risk_score": 7, "risk_keywords": ["strike", "killed"]}]
    p = normalize_news(raw)
    assert p["layer"] == "news" and p["count"] == 1
    it = p["items"][0]
    assert it["title"] == "Strike kills" and it["risk_score"] == 7


def test_normalize_military_air_points():
    raw = {"ac": [
        {"hex": "ae0802", "flight": "TREK127 ", "t": "C17", "r": "98-0056",
         "lat": 59.5, "lon": -169.7, "alt_baro": 34000, "gs": 459.1,
         "track": 248.8, "squawk": "3410", "dbFlags": 1},
        {"hex": "n0", "lat": None, "lon": None},  # skipped (no position)
    ]}
    p = normalize_military_air(raw)
    assert p["layer"] == "military_air" and p["count"] == 1
    it = p["items"][0]
    assert it["id"] == "ae0802" and it["lat"] == 59.5 and it["lng"] == -169.7
    assert it["label"] == "TREK127" and it["callsign"] == "TREK127"
    assert it["type"] == "C17" and it["reg"] == "98-0056" and it["alt"] == 34000
    assert it["speed"] == 459.1 and it["track"] == 248.8 and it["squawk"] == "3410"
    assert it["category"] == "military" and it["color"] == "#ff5a5a"


def test_normalize_events_geolocates_by_source_country():
    raw = {"articles": [
        {"url": "u1", "title": "Missile strike kills troops near border",
         "domain": "x.com", "language": "English", "sourcecountry": "Ukraine",
         "seendate": "20260521T223000Z", "socialimage": "img"},
        {"url": "u2", "title": "Quiet day", "sourcecountry": "Atlantis"},  # no centroid -> skipped
        {"url": "u3", "title": "Peace talks", "sourcecountry": ""},        # blank -> skipped
    ]}
    p = normalize_events(raw)
    assert p["layer"] == "events" and p["count"] == 1
    it = p["items"][0]
    assert it["id"] == "u1" and it["source_country"] == "Ukraine"
    assert it["lat"] == 49.0 and it["lng"] == 32.0
    assert it["headline"].startswith("Missile strike")
    assert it["tone"] >= 3 and "missile" in it["risk_keywords"]
    assert it["color"] == "#ff793f"


def test_normalize_nav_warnings_parses_text_coords():
    raw = {"broadcast-warn": [
        {"msgYear": 2024, "msgNumber": 449, "navArea": "4", "subregion": "24",
         "issueDate": "191944Z APR 2024", "authority": "TRINIDAD 79/24",
         "text": "EASTERN CARIBBEAN SEA.\nCHACACHACARE LIGHT 10-41.9N 061-45.1W.\n"
                 "PUNTA DEL ARENAL 10-02.9N 061-55.6W."},
        {"msgYear": 2024, "msgNumber": 1, "navArea": "4",
         "text": "NO COORDINATES HERE, JUST PROSE."},  # skipped (no coords)
    ]}
    p = normalize_nav_warnings(raw)
    assert p["layer"] == "nav_warnings" and p["count"] == 1
    it = p["items"][0]
    assert it["id"] == "nav-4-2024-449" and it["nav_area"] == "4"
    assert it["msg"] == "449/2024"
    # first coord: 10 + 41.9/60 = 10.6983 ; -(61 + 45.1/60) = -61.7517
    assert it["lat"] == 10.6983 and it["lng"] == -61.7517
    assert len(it["coords"]) == 2
    assert it["color"] == "#f7b731"


def test_normalize_military_bases_overpass_elements():
    raw = {"elements": [
        {"type": "node", "id": 1, "lat": 36.6, "lon": -76.3,
         "tags": {"military": "naval_base", "name": "Naval Station Norfolk",
                  "operator": "US Navy"}},
        {"type": "way", "id": 2, "center": {"lat": 51.5, "lon": -0.1},
         "tags": {"military": "airfield", "name:en": "RAF Northolt"}},
        {"type": "node", "id": 3, "tags": {"military": "danger_area"}},  # no coords -> skipped
    ]}
    p = normalize_military_bases(raw, military_type="Naval Base")
    assert p["layer"] == "military_bases" and p["count"] == 2
    it = p["items"][0]
    assert it["id"] == "node-1" and it["label"] == "Naval Station Norfolk"
    assert it["military_type"] == "Naval Base" and it["osm_military"] == "naval_base"
    assert it["lat"] == 36.6 and it["lng"] == -76.3 and it["color"] == "#c8a951"
    # way center is used when node lat/lon absent
    assert p["items"][1]["lat"] == 51.5 and p["items"][1]["label"] == "RAF Northolt"


def test_normalize_military_naval_filters_naval_vessels():
    vessels = [
        {"mmsi": 211000001, "lat": 54.0, "lng": 8.0, "name": "FGS BAYERN",
         "ship_type": None},                                   # naval name -> keep
        {"mmsi": 366000002, "lat": 32.7, "lng": -117.1, "name": "WARSHIP 60",
         "ship_type": 35},                                     # mil ship_type -> keep
        {"mmsi": 538000003, "lat": 1.2, "lng": 103.8, "name": "EVER GIVEN",
         "ship_type": 70},                                     # cargo -> drop
        {"mmsi": 244000004, "lat": None, "lng": 4.5, "name": "HNLMS X"},  # no pos -> drop
    ]
    p = normalize_military_naval(vessels)
    assert p["layer"] == "military_naval" and p["count"] == 2
    names = {it["name"] for it in p["items"]}
    assert names == {"FGS BAYERN", "WARSHIP 60"}
    by_name = {it["name"]: it for it in p["items"]}
    assert by_name["WARSHIP 60"]["match"] == "ship_type=military"
    assert by_name["FGS BAYERN"]["match"] == "naval_name"
    assert all(it["naval"] is True and it["color"] == "#5a9bff" for it in p["items"])


def test_normalize_markets_record_array():
    raw = [{"symbol": "RTX", "name": "RTX Corp", "price": 120.5,
            "change_percent": 1.2, "up": True, "source": "yahoo"}]
    p = normalize_markets(raw)
    assert p["layer"] == "markets" and p["count"] == 1
    it = p["items"][0]
    assert it["symbol"] == "RTX" and it["price"] == 120.5 and it["up"] is True
