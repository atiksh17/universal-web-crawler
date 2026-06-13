from __future__ import annotations

import asyncio
import time

from ..models import FetchResult
from ..signatures import BLOCKED_URL_PATTERNS
from .base import Fetcher

try:
    import nodriver as uc
    from nodriver import cdp
    _HAS = True
except Exception:  # pragma: no cover
    _HAS = False


class L3ProxyBrowser(Fetcher):
    """OPTIONAL / DORMANT — residential-proxy browser tier. NOT in the canonical ladder.

    Deprioritized by design (see README "Why L3 is optional"): the residue that reaches the
    bottom tier is dominated by challenge walls (DataDome/PerimeterX/Turnstile) that a
    residential IP alone does not solve — L4 (Web Unlocker) does, pay-on-success. This module
    is kept as ready boilerplate: activate by setting CRAWLER_PROXIES and adding L3 to
    CRAWLER_ENABLED_TIERS. A fresh browser is launched per call bound to the pool's proxy.
    """

    name = "nodriver+residential"
    tier = "L3"

    def __init__(self, headless: bool = True, nav_timeout_s: float = 25.0,
                 settle_ms: int = 800, block_resources: bool = True, chrome_path: str = ""):
        self.headless = headless
        self.nav_timeout_s = nav_timeout_s
        self.settle_ms = settle_ms
        self.block_resources = block_resources
        self.chrome_path = chrome_path or None
        self.enabled = _HAS
        if not _HAS:
            self.disabled_reason = "nodriver not installed"

    async def _do(self, url: str, proxy: str) -> FetchResult:
        args = ["--disable-dev-shm-usage", "--disable-gpu", f"--proxy-server={proxy}"]
        browser = await uc.start(headless=self.headless, sandbox=False, browser_args=args,
                                 browser_executable_path=self.chrome_path)
        try:
            tab = await browser.get("about:blank")
            try:
                await tab.send(cdp.network.enable())
                ua = (await tab.evaluate("navigator.userAgent") or "").replace("HeadlessChrome", "Chrome")
                if ua:
                    await tab.send(cdp.network.set_user_agent_override(user_agent=ua))
            except Exception:
                pass
            if self.block_resources:
                try:
                    await tab.send(cdp.network.set_blocked_urls(urls=BLOCKED_URL_PATTERNS))
                except Exception:
                    pass
            await tab.send(cdp.page.navigate(url=url))
            from .l2_nodriver import _wait_ready
            await _wait_ready(tab, self.nav_timeout_s)
            await asyncio.sleep(self.settle_ms / 1000)
            html = await tab.get_content()
            return FetchResult(url=url, tier=self.tier, status=200 if html else None,
                               html=html or "", content_length=len(html or ""))
        finally:
            try:
                browser.stop()
            except Exception:
                pass

    async def fetch(self, url: str, *, proxy: str | None = None) -> FetchResult:
        if not _HAS:
            return FetchResult(url=url, tier=self.tier, ok=False, reason="nodriver_not_installed")
        if not proxy:
            return FetchResult(url=url, tier=self.tier, ok=False, reason="no_proxy_available")
        t0 = time.monotonic()
        try:
            res = await asyncio.wait_for(self._do(url, proxy), timeout=self.nav_timeout_s + 15)
            res.elapsed_ms = int((time.monotonic() - t0) * 1000)
            return res
        except asyncio.TimeoutError:
            return FetchResult(url=url, tier=self.tier, ok=False, reason="nav_timeout",
                               elapsed_ms=int((time.monotonic() - t0) * 1000))
        except Exception as e:
            return FetchResult(url=url, tier=self.tier, ok=False, reason=f"error:{type(e).__name__}",
                               elapsed_ms=int((time.monotonic() - t0) * 1000))
