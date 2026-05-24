"""Shared minimal fetcher with REFUSE-WITHOUT-PROXY guard.

Retries transient ProxyRack 466 (capacity) errors and connection failures
with fresh sessions; up to `max_retries` attempts.
"""
import os, random, time
from typing import Optional
import curl_cffi.requests as cr


class Fetcher:
    def __init__(self, proxy_url: Optional[str] = None, allow_direct_ip: bool = False,
                 impersonate: str = "chrome146", country: Optional[str] = None,
                 max_retries: int = 3):
        self.proxy_url = proxy_url or os.environ.get("PROXYRACK_PROXY_URL")
        self.allow_direct_ip = allow_direct_ip
        self.impersonate = impersonate
        self.country = country
        self.max_retries = max_retries
        self.user = os.environ.get("PROXYRACK_USERNAME")
        self.pwd  = os.environ.get("PROXYRACK_PASSWORD")
        self.host = os.environ.get("PROXYRACK_HOST", "unmetered.residential.proxyrack.net")
        self.port = os.environ.get("PROXYRACK_PORT", "10000")
        if not self.proxy_url and not self.user and not allow_direct_ip:
            raise RuntimeError("REFUSED: no proxy configured (set PROXYRACK_PROXY_URL or pass allow_direct_ip=True)")

    def _proxy(self) -> Optional[str]:
        if not self.user:
            return self.proxy_url
        user = self.user
        if self.country:
            user = f"{user}-country-{self.country}"
        # fresh session per request
        user = f"{user}-session-{random.randint(1, 9_999_999)}"
        return f"http://{user}:{self.pwd}@{self.host}:{self.port}"

    def _request(self, method, url, **kw):
        kw.setdefault("timeout", 30)
        last_exc = None
        for attempt in range(self.max_retries):
            p = self._proxy()
            proxies = {"http": p, "https": p} if p else None
            try:
                return cr.request(method, url, impersonate=self.impersonate,
                                  proxies=proxies, **kw)
            except Exception as e:
                last_exc = e
                msg = str(e)
                # 466 = ProxyRack transient capacity; retry on fresh session.
                # ConnectionError / Timeout — also retry.
                transient = ("466" in msg or "Connection" in msg
                             or "timeout" in msg.lower() or "tunnel" in msg.lower())
                if transient and attempt < self.max_retries - 1:
                    time.sleep(0.4 * (attempt + 1))
                    continue
                raise
        raise last_exc

    def get(self, url, **kw):
        return self._request("GET", url, **kw)

    def post(self, url, **kw):
        return self._request("POST", url, **kw)


if __name__ == "__main__":
    f = Fetcher()
    r = f.get("https://httpbin.org/ip")
    print(r.status_code, r.text[:200])
