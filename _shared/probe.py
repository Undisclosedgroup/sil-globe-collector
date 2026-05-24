"""Quick probe helper: hit a list of URLs and report status/size/CT."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from fetcher import Fetcher

def probe(urls, impersonate="chrome146"):
    f = Fetcher(impersonate=impersonate)
    results = []
    for u in urls:
        try:
            r = f.get(u, allow_redirects=True)
            ct = (r.headers.get('content-type') or '')[:30]
            results.append((r.status_code, len(r.content), ct, u))
            print(f"{r.status_code:3d} {len(r.content):>8d} {ct:30s} {u}")
        except Exception as e:
            results.append((0, 0, "ERR", u))
            print(f"ERR {' '*16} {str(e)[:30]:30s} {u}")
    return results

if __name__ == "__main__":
    probe(sys.argv[1:])
