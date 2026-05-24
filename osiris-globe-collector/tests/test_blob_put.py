import os, json
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from blob_put import build_put_request


def test_build_put_request_targets_layer_path():
    req = build_put_request("flights", {"layer": "flights", "items": []}, token="tkn_x")
    assert req["url"].endswith("/globe/flights.json")
    assert req["headers"]["authorization"] == "Bearer tkn_x"
    assert req["headers"]["x-content-type"] == "application/json"
    assert json.loads(req["body"])["layer"] == "flights"


def test_build_put_request_manifest_path():
    req = build_put_request("_manifest", {"layers": {}}, token="t")
    assert req["url"].endswith("/globe/_manifest.json")


def test_build_put_request_body_is_compact_json():
    req = build_put_request("markets", {"layer": "markets", "count": 0, "items": []}, token="t")
    # compact separators -> no ", " spacing
    assert ", " not in req["body"]
