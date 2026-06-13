from __future__ import annotations

import asyncio
import shutil
import tempfile
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


async def _wait_ready(tab, timeout_s: float) -> None:
    """Poll document.readyState until 'complete' (or timeout). page.navigate returns
    when nav *starts*, not when loaded — without this, get_content races the render."""
    steps = max(1, int(timeout_s / 0.1))
    for _ in range(steps):
        try:
            state = await tab.evaluate("document.readyState")
        except Exception:
            state = None
        if state == "complete":
            return
        await asyncio.sleep(0.1)


class L2Nodriver(Fetcher):
    """Tier 2 — headless Chrome via Nodriver (datacenter IP, no proxy).

    Renders JS, beats JS challenges. Stealth: no chromedriver, navigator.webdriver=False,
    HeadlessChrome stripped from the UA. Resource blocking loads HTML+JS only.
    """

    name = "nodriver"
    tier = "L2"

    def __init__(self, headless: bool = True, nav_timeout_s: float = 25.0,
                 settle_ms: int = 800, block_resources: bool = True, chrome_path: str = "",
                 browser_concurrency: int = 2):
        self.headless = headless
        self.nav_timeout_s = nav_timeout_s
        self.settle_ms = settle_ms
        self.block_resources = block_resources
        self.chrome_path = chrome_path or None
        self.enabled = _HAS
        if not _HAS:
            self.disabled_reason = "nodriver not installed"
        self._ua: str | None = None
        self._ua_lock = asyncio.Lock()
        # Concurrency cap. Each fetch launches a FRESH browser and stops it after —
        # no reuse, so no stale-session collisions. Slower, but bulletproof under load.
        # (VPS optimization: pool with proper per-use tab reset.)
        self._sema = asyncio.Semaphore(max(1, browser_concurrency))

    async def _launch(self, user_data_dir: str):
        # Explicit temp profile per launch + cleanup on stop. nodriver already makes a temp
        # profile, but passing our own guarantees teardown -> no leaked profiles at volume.
        # This is HYGIENE, not the cure for the VPS "Failed to connect to browser" bug.
        # That bug is timing: snap chromium's slow cold start (squashfs mount + first-run)
        # exceeds nodriver's ~3s connect window, badly under concurrent CPU/IO contention
        # (warm = sub-second = fine; cold-concurrent = fails). Real fix = non-snap Chrome
        # (standalone google-chrome-stable), which cold-starts sub-second. See README.
        args = ["--disable-dev-shm-usage", "--disable-gpu",
                "--no-first-run", "--no-default-browser-check"]
        return await uc.start(headless=self.headless, sandbox=False, browser_args=args,
                              browser_executable_path=self.chrome_path,
                              user_data_dir=user_data_dir)

    async def _stealth(self, tab) -> None:
        """Enable network + strip 'HeadlessChrome' from the UA (the #1 headless tell)."""
        try:
            await tab.send(cdp.network.enable())
        except Exception:
            return
        try:
            if self._ua is None:
                async with self._ua_lock:
                    if self._ua is None:
                        real = await tab.evaluate("navigator.userAgent")
                        self._ua = (real or "").replace("HeadlessChrome", "Chrome")
            if self._ua:
                await tab.send(cdp.network.set_user_agent_override(user_agent=self._ua))
        except Exception:
            pass

    async def _do(self, browser, url: str) -> FetchResult:
        tab = await browser.get("about:blank")
        await self._stealth(tab)
        if self.block_resources:
            try:
                await tab.send(cdp.network.set_blocked_urls(urls=BLOCKED_URL_PATTERNS))
            except Exception:
                pass
        await tab.send(cdp.page.navigate(url=url))
        await _wait_ready(tab, self.nav_timeout_s)
        await asyncio.sleep(self.settle_ms / 1000)  # let client-side JS render after load
        html = await tab.get_content()
        try:
            await tab.close()
        except Exception:
            pass
        return FetchResult(url=url, tier=self.tier, status=200 if html else None,
                           html=html or "", content_length=len(html or ""))

    async def fetch(self, url: str, *, proxy: str | None = None) -> FetchResult:
        if not _HAS:
            return FetchResult(url=url, tier=self.tier, ok=False, reason="nodriver_not_installed")
        t0 = time.monotonic()
        async with self._sema:
            browser = None
            profile = tempfile.mkdtemp(prefix="cr-prof-")
            try:
                browser = await self._launch(profile)
                res = await asyncio.wait_for(self._do(browser, url), timeout=self.nav_timeout_s + 10)
                res.elapsed_ms = int((time.monotonic() - t0) * 1000)
                return res
            except asyncio.TimeoutError:
                return FetchResult(url=url, tier=self.tier, ok=False, reason="nav_timeout",
                                   elapsed_ms=int((time.monotonic() - t0) * 1000))
            except Exception as e:
                return FetchResult(url=url, tier=self.tier, ok=False, reason=f"error:{type(e).__name__}",
                                   elapsed_ms=int((time.monotonic() - t0) * 1000))
            finally:
                if browser is not None:
                    try:
                        browser.stop()
                    except Exception:
                        pass
                shutil.rmtree(profile, ignore_errors=True)  # don't leak temp profiles

    async def aclose(self) -> None:
        return None
