"""Shared proxy-mandatory fetcher. Refuses to fetch on home IP.

Usage (sync):
    from proxy_fetcher import fetcher, ProxyFetcher
    r = fetcher().get("https://example.com")
    if r.tier == "refused_no_proxy": raise SystemExit("proxy not loaded")

Optimizations (v2):
- Reuses a `curl_cffi.Session` (HTTP/2 keepalive, connection pool).
- Enables TCP_NODELAY + libcurl DNS cache (300s) + TCP keepalive.
- `Accept-Encoding: gzip, deflate, br, zstd` is already injected by chrome131
  impersonation. Brotli is auto-decompressed by libcurl.
- Async path: `await fetcher().aget(url)` uses one shared AsyncSession per
  ProxyFetcher instance (HTTP/2 multiplexing over a single tunnel).

Backward-compat:
- `.get()` still returns FetchResult exactly as before.
- Old `proxies=` kwarg, `impersonate=`, `headers=`, `timeout=` all preserved.
- Pre-existing scrapers that pass only positional `url` keep working.
"""
import os, time, random, logging, asyncio, dataclasses, pathlib, threading
from typing import Optional, Any

log = logging.getLogger("proxy_fetcher")
log.setLevel(logging.INFO)
if not log.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    log.addHandler(h)


def _load_env():
    p = pathlib.Path.home() / ".proxyrack.env"
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip().strip('"').strip("'")
        os.environ.setdefault(k.strip(), v)


_load_env()

PROXYRACK_HOST = os.getenv("PROXYRACK_HOST", "unmetered.residential.proxyrack.net")
PROXYRACK_PORT = os.getenv("PROXYRACK_PORT", "10000")
PROXYRACK_USERNAME = os.getenv("PROXYRACK_USERNAME", "")
PROXYRACK_PASSWORD = os.getenv("PROXYRACK_PASSWORD", "")


# ---------------------------------------------------------------------------
# Webshare datacenter pool — alternate provider for cloud-IP environments
# (GitHub Actions, Fly.io) where ProxyRack residential refuses CONNECT from
# AWS/Azure/GH ASNs. When WEBSHARE_POOL_B64 is set, ProxyFetcher's proxy_url
# returns a random Webshare proxy from the pool *instead of* building a
# ProxyRack URL. Format of each decoded line: host:port:user:pass (the
# standard webshare_1000.txt format we already maintain).
#
# Trade-off: datacenter IPs are blocked by some anti-bot stacks that allow
# residential (TikTok, Cloudflare strict, Akamai Bot Manager). Most state
# DOT / weather / USGS / Overpass endpoints don't care, so this is a good
# tier for the collector's bulk workload.
# ---------------------------------------------------------------------------
_WEBSHARE_POOL: list = []
_WEBSHARE_POOL_B64 = os.getenv("WEBSHARE_POOL_B64")
if _WEBSHARE_POOL_B64:
    try:
        import base64
        raw = base64.b64decode(_WEBSHARE_POOL_B64).decode("utf-8", "replace")
        for ln in raw.splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            parts = ln.split(":")
            if len(parts) >= 4:
                _WEBSHARE_POOL.append(tuple(parts[:4]))  # (host, port, user, pass)
        log.info("webshare pool loaded: %d IPs", len(_WEBSHARE_POOL))
    except Exception as e:
        log.warning("webshare pool decode failed: %s", e)


def _webshare_random_url():
    """Return a random webshare proxy URL, or None if pool unavailable."""
    if not _WEBSHARE_POOL:
        return None
    h, p, u, w = random.choice(_WEBSHARE_POOL)
    return f"http://{u}:{w}@{h}:{p}"


@dataclasses.dataclass
class FetchResult:
    status: int
    body: bytes = b""
    headers: dict = dataclasses.field(default_factory=dict)
    tier: str = ""
    url: str = ""
    elapsed_ms: int = 0
    proxy_ip: Optional[str] = None
    error: Optional[str] = None

    @property
    def text(self) -> str:
        try:
            return self.body.decode("utf-8", errors="replace")
        except Exception:
            return ""


def _perf_curl_options() -> dict:
    """libcurl options that give us TCP_NODELAY + DNS cache + keepalive.

    `curl_cffi` accepts a dict of `CurlOpt.X -> value` via `curl_options=`.
    The numeric option codes are stable across curl versions.
    """
    try:
        from curl_cffi import CurlOpt
    except Exception:
        return {}
    return {
        CurlOpt.TCP_NODELAY: 1,           # ship small writes immediately (no Nagle)
        CurlOpt.TCP_KEEPALIVE: 1,         # keep idle connections alive across calls
        CurlOpt.TCP_KEEPIDLE: 60,         # start probing after 60s idle
        CurlOpt.TCP_KEEPINTVL: 30,
        CurlOpt.DNS_CACHE_TIMEOUT: 300,   # 5-min DNS cache in libcurl (per-easy/share)
    }


class ProxyFetcher:
    """Refuse-without-proxy fetcher. Reuses a `curl_cffi.Session` per instance.

    The session reuses TCP/TLS connections to the proxy CONNECT tunnel, so
    subsequent calls skip the ~600ms handshake. Single-threaded inside;
    serialize calls via PROXY_SLOTS if multi-coro.
    """

    def __init__(self, country: Optional[str] = None, session: Optional[str] = None,
                 uptime: Optional[str] = None, allow_direct_ip: bool = False,
                 impersonate: str = "chrome146"):
        # LESSON FROM 172-target audit: country-XX modifier triggers CONNECT 565
        # on most .gov / Akamai / Imperva edges. Default OFF.
        # uptime="10m" sticky-IP gets caught by Tyler/Socrata residential blocklists.
        # Default = no modifiers + fresh session per request (set via .with_fresh_session()).
        # chrome146 is the latest Chrome impersonation profile (May 2026); chrome131/safari17_0
        # available via impersonate= kwarg per call.
        self.allow_direct_ip = allow_direct_ip
        self.country = country
        self.session = session
        self.uptime = uptime
        self.impersonate = impersonate
        # "Configured" means we have ANY proxy provider — ProxyRack creds OR
        # a Webshare pool loaded. Without one of these, refuse fetches (per
        # CLAUDE.md never-burn-home-IP rule) unless explicitly opted in.
        self._configured = bool(
            (PROXYRACK_USERNAME and PROXYRACK_PASSWORD) or _WEBSHARE_POOL
        )
        if not self._configured and not allow_direct_ip:
            log.error("proxy creds missing — every fetch will be refused")
        # Lazy sessions; cached per (impersonate, proxy_url) tuple.
        self._sessions: dict = {}
        self._async_sessions: dict = {}
        self._lock = threading.Lock()

    def proxy_url(self) -> Optional[str]:
        # Webshare pool takes priority when configured (cloud-IP environments
        # where ProxyRack residential refuses CONNECT). Random per call gives
        # the same "fresh IP per request" semantic ProxyRack provides via
        # session-N modifiers.
        ws = _webshare_random_url()
        if ws:
            return ws
        if not self._configured:
            return None
        mods = []
        if self.country:
            mods.append(f"country-{self.country}")
        if self.uptime:
            mods.append(f"proxyUptime-{self.uptime}")
        # Fresh-per-request session is the default behavior — proven critical for
        # bypassing Tyler/Socrata residential blocklists. Pass session="sticky-N"
        # to override when you need cookie/auth persistence.
        if self.session:
            mods.append(f"session-{self.session}")
        else:
            mods.append(f"session-{random.randint(1, 9_999_999)}")
        user = PROXYRACK_USERNAME + ("-" + "-".join(mods) if mods else "")
        return f"http://{user}:{PROXYRACK_PASSWORD}@{PROXYRACK_HOST}:{PROXYRACK_PORT}"

    def _sticky_proxy_url(self) -> Optional[str]:
        """Stable proxy URL for the LIFETIME of this fetcher.

        When `self.session` is None, callers want IP rotation per request, so
        we still synthesize a sticky session ID *once* per fetcher instance.
        This lets us reuse the curl Session (TCP/TLS handshake amortized) while
        the IP is held for the fetcher's lifetime — a good balance for scrapers
        that want speed but also some anonymity.

        If the caller wants TRUE fresh-IP-per-request, set `rotate_per_call=True`
        on the fetcher or use the legacy `.get_rotating()` path.
        """
        if not hasattr(self, "_sticky_session_id"):
            self._sticky_session_id = (
                self.session or f"reuse_{random.randint(1, 9_999_999)}"
            )
        # Webshare path: pick ONE proxy per fetcher lifetime so the cached
        # curl Session reuses its TCP/TLS handshake. The pool is small (~1k)
        # but each call goes through the same exit IP for this fetcher,
        # mirroring the ProxyRack "sticky session" semantic.
        if _WEBSHARE_POOL:
            if not hasattr(self, "_sticky_webshare_url"):
                h, p, u, w = random.choice(_WEBSHARE_POOL)
                self._sticky_webshare_url = f"http://{u}:{w}@{h}:{p}"
            return self._sticky_webshare_url
        if not self._configured:
            return None
        mods = []
        if self.country:
            mods.append(f"country-{self.country}")
        if self.uptime:
            mods.append(f"proxyUptime-{self.uptime}")
        mods.append(f"session-{self._sticky_session_id}")
        user = PROXYRACK_USERNAME + ("-" + "-".join(mods) if mods else "")
        return f"http://{user}:{PROXYRACK_PASSWORD}@{PROXYRACK_HOST}:{PROXYRACK_PORT}"

    def _get_session(self, impersonate: str):
        """Return a cached curl_cffi.Session keyed by impersonate string only.

        We bind the cached Session to the fetcher-lifetime sticky proxy URL,
        so subsequent calls reuse TCP/TLS handshake to the proxy.
        """
        with self._lock:
            s = self._sessions.get(impersonate)
            if s is not None:
                return s
            from curl_cffi import requests as cr
            proxy = self._sticky_proxy_url()
            proxies = {"http": proxy, "https": proxy} if proxy else None
            s = cr.Session(
                impersonate=impersonate,
                proxies=proxies,
                timeout=30,
                curl_options=_perf_curl_options(),
            )
            self._sessions[impersonate] = s
            return s

    def _get_async_session(self, impersonate: str):
        s = self._async_sessions.get(impersonate)
        if s is not None:
            return s
        from curl_cffi import requests as cr
        proxy = self._sticky_proxy_url()
        proxies = {"http": proxy, "https": proxy} if proxy else None
        s = cr.AsyncSession(
            impersonate=impersonate,
            proxies=proxies,
            timeout=30,
            curl_options=_perf_curl_options(),
        )
        self._async_sessions[impersonate] = s
        return s

    def get(self, url: str, *, impersonate: Optional[str] = None,
            headers: Optional[dict] = None, timeout: int = 25,
            allow_redirects: bool = True,
            rotate_per_call: bool = False) -> FetchResult:
        """HTTP GET via cached Session (default) or per-call rotation.

        rotate_per_call=True falls back to the v1 stateless behavior — useful
        when bypassing IP-based blocklists requires a fresh residential IP
        every request.
        """
        if not self._configured and not self.allow_direct_ip:
            return FetchResult(status=0, tier="refused_no_proxy", url=url,
                               error="proxy not configured and allow_direct_ip=False")
        imp = impersonate or self.impersonate
        if rotate_per_call:
            return self._get_stateless(url, imp, headers, timeout, allow_redirects)
        sess = self._get_session(imp)
        t0 = time.time()
        try:
            r = sess.get(url, headers=headers or {}, timeout=timeout,
                         allow_redirects=allow_redirects)
            return FetchResult(status=r.status_code, body=r.content,
                               headers=dict(r.headers),
                               tier=f"curl_cffi/{imp}/sess/proxy=y",
                               url=str(r.url), elapsed_ms=int((time.time() - t0) * 1000))
        except Exception as e:
            # Drop the (likely-broken) cached session; next call rebuilds it.
            with self._lock:
                self._sessions.pop(imp, None)
            return FetchResult(status=-1, tier=f"error/{imp}", url=url,
                               error=f"{type(e).__name__}: {e}",
                               elapsed_ms=int((time.time() - t0) * 1000))

    def _get_stateless(self, url: str, imp: str, headers: Optional[dict],
                       timeout: int, allow_redirects: bool) -> FetchResult:
        """Legacy v1 path: per-call cr.get with rotating IP. Slower but rotates."""
        from curl_cffi import requests as cr
        pxy = self.proxy_url()
        kwargs = dict(headers=headers or {}, timeout=timeout,
                      allow_redirects=allow_redirects, impersonate=imp,
                      curl_options=_perf_curl_options())
        if pxy:
            kwargs["proxies"] = {"http": pxy, "https": pxy}
        t0 = time.time()
        try:
            r = cr.get(url, **kwargs)
            return FetchResult(status=r.status_code, body=r.content,
                               headers=dict(r.headers),
                               tier=f"curl_cffi/{imp}/rot/proxy={'y' if pxy else 'n'}",
                               url=str(r.url), elapsed_ms=int((time.time() - t0) * 1000))
        except Exception as e:
            return FetchResult(status=-1, tier=f"error/{imp}", url=url,
                               error=f"{type(e).__name__}: {e}",
                               elapsed_ms=int((time.time() - t0) * 1000))

    def post(self, url: str, *, impersonate: Optional[str] = None,
             headers: Optional[dict] = None, data: Any = None, json: Any = None,
             timeout: int = 25, allow_redirects: bool = True) -> FetchResult:
        if not self._configured and not self.allow_direct_ip:
            return FetchResult(status=0, tier="refused_no_proxy", url=url,
                               error="proxy not configured and allow_direct_ip=False")
        imp = impersonate or self.impersonate
        sess = self._get_session(imp)
        t0 = time.time()
        try:
            r = sess.post(url, headers=headers or {}, data=data, json=json,
                          timeout=timeout, allow_redirects=allow_redirects)
            return FetchResult(status=r.status_code, body=r.content,
                               headers=dict(r.headers),
                               tier=f"curl_cffi/{imp}/sess/proxy=y",
                               url=str(r.url), elapsed_ms=int((time.time() - t0) * 1000))
        except Exception as e:
            with self._lock:
                self._sessions.pop(imp, None)
            return FetchResult(status=-1, tier=f"error/{imp}", url=url,
                               error=f"{type(e).__name__}: {e}",
                               elapsed_ms=int((time.time() - t0) * 1000))

    async def aget(self, url: str, *, impersonate: Optional[str] = None,
                   headers: Optional[dict] = None, timeout: int = 25,
                   allow_redirects: bool = True) -> FetchResult:
        if not self._configured and not self.allow_direct_ip:
            return FetchResult(status=0, tier="refused_no_proxy", url=url,
                               error="proxy not configured and allow_direct_ip=False")
        imp = impersonate or self.impersonate
        sess = self._get_async_session(imp)
        t0 = time.time()
        try:
            r = await sess.get(url, headers=headers or {}, timeout=timeout,
                               allow_redirects=allow_redirects)
            return FetchResult(status=r.status_code, body=r.content,
                               headers=dict(r.headers),
                               tier=f"curl_cffi/{imp}/asess/proxy=y",
                               url=str(r.url), elapsed_ms=int((time.time() - t0) * 1000))
        except Exception as e:
            self._async_sessions.pop(imp, None)
            return FetchResult(status=-1, tier=f"aerror/{imp}", url=url,
                               error=f"{type(e).__name__}: {e}",
                               elapsed_ms=int((time.time() - t0) * 1000))

    def close(self):
        with self._lock:
            for s in self._sessions.values():
                try:
                    s.close()
                except Exception:
                    pass
            self._sessions.clear()
        self._async_sessions.clear()  # AsyncSession close needs event loop; skip

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def fetcher(**kw) -> ProxyFetcher:
    return ProxyFetcher(**kw)


# Single global asyncio semaphore — recon agents should respect 4-slot cap collectively.
# Each scraper imports this and wraps requests in `async with PROXY_SLOTS:`.
PROXY_SLOTS = asyncio.Semaphore(1)  # default to 1 inside any single scraper process
