"""Shared Camoufox-via-proxy fetcher for Pass-1 retry agents.

Wraps AsyncCamoufox + ProxyRack US residential into a single helper that
returns the rendered HTML (after networkidle). Refuses without proxy.

Usage:
    import asyncio
    from camoufox_fetcher import camoufox_fetch
    res = asyncio.run(camoufox_fetch("https://example.com"))
    print(res.status, len(res.body or ""))
"""
import os
import time
import asyncio
import dataclasses
import pathlib
from typing import Optional, List


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


@dataclasses.dataclass
class CamoufoxResult:
    url: str
    status: int = 0
    body: str = ""
    title: str = ""
    final_url: str = ""
    elapsed_ms: int = 0
    error: Optional[str] = None
    proxy_user: Optional[str] = None
    challenge_detected: bool = False
    challenge_kind: str = ""


def proxy_url(country: str = "US", session: Optional[str] = None,
              uptime: str = "10m") -> Optional[str]:
    if not (PROXYRACK_USERNAME and PROXYRACK_PASSWORD):
        return None
    mods: List[str] = []
    if country:
        mods.append(f"country-{country}")
    if uptime:
        mods.append(f"proxyUptime-{uptime}")
    if session:
        mods.append(f"session-{session}")
    user = PROXYRACK_USERNAME + ("-" + "-".join(mods) if mods else "")
    return f"http://{user}:{PROXYRACK_PASSWORD}@{PROXYRACK_HOST}:{PROXYRACK_PORT}"


def _detect_challenge(html: str, title: str) -> tuple[bool, str]:
    low = (html or "").lower()
    t = (title or "").lower()
    if "datadome" in low or "captcha-delivery.com" in low:
        return True, "datadome"
    if "bot or not" in low or "akam/" in low or "_abck" in low and "challenge" in low:
        return True, "akamai"
    if "cf-mitigated" in low or "just a moment" in low or "cf_chl_opt" in low:
        return True, "cloudflare"
    if "turnstile" in low and "cf-turnstile" in low:
        return True, "cf_turnstile"
    if t in ("just a moment...", "bot or not?"):
        return True, "challenge_title"
    return False, ""


async def camoufox_fetch(
    url: str,
    *,
    session: Optional[str] = None,
    uptime: str = "10m",
    country: str = "US",
    wait_until: str = "networkidle",
    extra_wait_ms: int = 1500,
    nav_timeout_ms: int = 45000,
    allow_direct_ip: bool = False,
    headless: bool = True,
    geoip: bool = False,
    locale: str = "en-US",
    humanize: bool = False,
) -> CamoufoxResult:
    """Fetch a URL with Camoufox routed through ProxyRack. Returns rendered HTML."""
    from camoufox.async_api import AsyncCamoufox

    if not (PROXYRACK_USERNAME and PROXYRACK_PASSWORD) and not allow_direct_ip:
        return CamoufoxResult(url=url, status=0, error="proxy not configured and allow_direct_ip=False")

    px = proxy_url(country=country, session=session, uptime=uptime)
    proxy_cfg = None
    if px:
        # camoufox uses Playwright's proxy dict
        # parse the user:pass@host:port form
        from urllib.parse import urlparse
        u = urlparse(px)
        proxy_cfg = {
            "server": f"http://{u.hostname}:{u.port}",
            "username": u.username,
            "password": u.password,
        }

    t0 = time.time()
    try:
        async with AsyncCamoufox(
            headless=headless,
            proxy=proxy_cfg,
            geoip=geoip,
            locale=locale,
            humanize=humanize,
        ) as browser:
            page = await browser.new_page()
            try:
                resp = await page.goto(url, wait_until=wait_until, timeout=nav_timeout_ms)
            except Exception as e:
                # Try a softer wait if networkidle timed out
                try:
                    resp = await page.goto(url, wait_until="domcontentloaded", timeout=nav_timeout_ms)
                except Exception as e2:
                    return CamoufoxResult(
                        url=url, status=-1, error=f"goto failed: {type(e2).__name__}: {e2}",
                        elapsed_ms=int((time.time() - t0) * 1000),
                        proxy_user=(proxy_cfg or {}).get("username"),
                    )
            if extra_wait_ms:
                await asyncio.sleep(extra_wait_ms / 1000)
            try:
                html = await page.content()
            except Exception as e:
                html = ""
            try:
                title = await page.title()
            except Exception:
                title = ""
            try:
                final_url = page.url
            except Exception:
                final_url = url
            status = resp.status if resp else 0
            ch, kind = _detect_challenge(html, title)
            return CamoufoxResult(
                url=url, status=status, body=html or "", title=title or "",
                final_url=final_url, elapsed_ms=int((time.time() - t0) * 1000),
                proxy_user=(proxy_cfg or {}).get("username"),
                challenge_detected=ch, challenge_kind=kind,
            )
    except Exception as e:
        return CamoufoxResult(
            url=url, status=-2, error=f"{type(e).__name__}: {e}",
            elapsed_ms=int((time.time() - t0) * 1000),
            proxy_user=(proxy_cfg or {}).get("username") if proxy_cfg else None,
        )


# Single in-flight slot per agent (shared 4-slot pool across 4 retry agents)
CAMOUFOX_SLOT = asyncio.Semaphore(1)
