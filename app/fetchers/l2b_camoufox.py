from __future__ import annotations

import time

from ..models import FetchResult
from .base import Fetcher

try:
    from camoufox.async_api import AsyncCamoufox
    _HAS = True
except Exception:  # pragma: no cover
    _HAS = False

_BLOCK_TYPES = {"image", "media", "font", "stylesheet"}


class L2BCamoufox(Fetcher):
    """Tier 2b — hardened Firefox (Camoufox). Fingerprint diversity when Chrome is flagged.

    Disabled until `camoufox` is installed (`pip install camoufox[geoip]` +
    `python -m camoufox fetch`). Then add L2B to CRAWLER_ENABLED_TIERS.
    """

    name = "camoufox"
    tier = "L2B"

    def __init__(self, headless: bool = True, nav_timeout_s: float = 25.0, block_resources: bool = True):
        self.headless = headless
        self.nav_timeout_s = nav_timeout_s
        self.block_resources = block_resources
        self.enabled = _HAS
        if not _HAS:
            self.disabled_reason = "camoufox not installed"

    async def fetch(self, url: str, *, proxy: str | None = None) -> FetchResult:
        if not _HAS:
            return FetchResult(url=url, tier=self.tier, ok=False, reason="camoufox_not_installed")
        t0 = time.monotonic()
        try:
            kwargs: dict = dict(headless=self.headless)
            if proxy:
                kwargs["proxy"] = {"server": proxy}
            async with AsyncCamoufox(**kwargs) as browser:
                page = await browser.new_page()
                if self.block_resources:
                    async def _route(route):
                        if route.request.resource_type in _BLOCK_TYPES:
                            await route.abort()
                        else:
                            await route.continue_()
                    await page.route("**/*", _route)
                await page.goto(url, timeout=self.nav_timeout_s * 1000, wait_until="networkidle")
                html = await page.content()
            return FetchResult(url=url, tier=self.tier, status=200 if html else None, html=html or "",
                               content_length=len(html or ""), elapsed_ms=int((time.monotonic() - t0) * 1000))
        except Exception as e:
            return FetchResult(url=url, tier=self.tier, ok=False, reason=f"error:{type(e).__name__}",
                               elapsed_ms=int((time.monotonic() - t0) * 1000))
