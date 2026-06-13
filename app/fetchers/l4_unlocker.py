from __future__ import annotations

import time

from ..models import FetchResult
from .base import Fetcher

try:
    import httpx
    _HAS_HTTPX = True
except Exception:  # pragma: no cover
    _HAS_HTTPX = False


class L4Unlocker(Fetcher):
    """Tier 4 — Bright Data Web Unlocker. The paid catch-all ($1.50/1k committed).

    Activates when CRAWLER_WEB_UNLOCKER_KEY + _ZONE are set and L4 is in ENABLED_TIERS.
    No code change to switch it on — just creds in .env.
    """

    name = "web_unlocker"
    tier = "L4"

    def __init__(self, api_url: str, key: str = "", zone: str = "", timeout: float = 60.0):
        self.api_url = api_url
        self.key = key
        self.zone = zone
        self.timeout = timeout
        self.enabled = bool(key and zone) and _HAS_HTTPX
        if not (key and zone):
            self.disabled_reason = "web unlocker key/zone not set"
        elif not _HAS_HTTPX:
            self.disabled_reason = "httpx not installed"

    async def fetch(self, url: str, *, proxy: str | None = None) -> FetchResult:
        if not self.enabled:
            return FetchResult(url=url, tier=self.tier, ok=False,
                               reason=f"disabled:{self.disabled_reason}")
        t0 = time.monotonic()
        headers = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}
        payload = {"zone": self.zone, "url": url, "format": "raw"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.post(self.api_url, headers=headers, json=payload)
            html = r.text or ""
            return FetchResult(url=url, tier=self.tier, status=r.status_code, html=html,
                               content_length=len(html), elapsed_ms=int((time.monotonic() - t0) * 1000))
        except Exception as e:
            return FetchResult(url=url, tier=self.tier, ok=False, reason=f"error:{type(e).__name__}",
                               elapsed_ms=int((time.monotonic() - t0) * 1000))
