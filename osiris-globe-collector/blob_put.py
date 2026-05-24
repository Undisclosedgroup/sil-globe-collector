"""Vercel Blob REST upload helper for the OSIRIS globe collector.

Uploads each layer's normalized payload to a STABLE path
`globe/<layer>.json` (no random suffix), so the Next.js read path can list
by a fixed prefix and always serve the latest snapshot in place.

Docs: PUT https://blob.vercel-storage.com/<path>  (Bearer <BLOB_READ_WRITE_TOKEN>)
"""
import json, os
from urllib import request as _rq

BLOB_API = "https://blob.vercel-storage.com"


def build_put_request(layer: str, payload: dict, token: str) -> dict:
    """Pure builder (unit-testable, no network): returns url/headers/body."""
    body = json.dumps(payload, separators=(",", ":"), default=str)
    return {
        "url": f"{BLOB_API}/globe/{layer}.json",
        "headers": {
            "authorization": f"Bearer {token}",
            "x-content-type": "application/json",
            "x-add-random-suffix": "0",        # stable path, overwrite in place
            "x-cache-control-max-age": "10",
        },
        "body": body,
    }


def put_blob(layer: str, payload: dict, token: str | None = None) -> str:
    """PUT the payload to Vercel Blob; returns the public blob URL."""
    token = token or os.environ["BLOB_READ_WRITE_TOKEN"]
    r = build_put_request(layer, payload, token)
    req = _rq.Request(r["url"], data=r["body"].encode(), headers=r["headers"], method="PUT")
    with _rq.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())["url"]   # public blob URL
