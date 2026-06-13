from __future__ import annotations

import time

from ..models import FetchResult
from .base import Fetcher

try:
    from curl_cffi.requests import AsyncSession
    _HAS = True
except Exception:  # pragma: no cover
    _HAS = False


class L1Curl(Fetcher):
    """Tier 1 — wire-level browser impersonation. No browser. Near-free, fast.

    Beats plain HTTP and TLS/JA3-fingerprint Cloudflare configs. Cannot run JS,
    so JS-rendered pages fall through to the browser tiers via the classifier.
    """

    name = "curl_cffi"
    tier = "L1"

    def __init__(self, impersonate: str = "chrome", timeout: float = 20.0):
        self.impersonate = impersonate
        self.timeout = timeout
        self.enabled = _HAS
        if not _HAS:
            self.disabled_reason = "curl_cffi not installed"

    async def fetch(self, url: str, *, proxy: str | None = None) -> FetchResult:
        if not _HAS:
            return FetchResult(url=url, tier=self.tier, ok=False, reason="curl_cffi_not_installed")
        t0 = time.monotonic()
        kwargs: dict = dict(impersonate=self.impersonate, timeout=self.timeout, allow_redirects=True)
        if proxy:
            kwargs["proxies"] = {"http": proxy, "https": proxy}
        try:
            async with AsyncSession() as s:
                r = await s.get(url, **kwargs)
            html = r.text or ""
            return FetchResult(
                url=url, tier=self.tier, status=r.status_code, html=html,
                content_length=len(html), elapsed_ms=int((time.monotonic() - t0) * 1000),
                final_url=str(r.url),
            )
        except Exception as e:
            return FetchResult(
                url=url, tier=self.tier, ok=False, reason=f"error:{type(e).__name__}",
                elapsed_ms=int((time.monotonic() - t0) * 1000),
            )
