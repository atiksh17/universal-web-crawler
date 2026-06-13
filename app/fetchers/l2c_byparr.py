from __future__ import annotations

import time

from ..models import FetchResult
from .base import Fetcher

try:
    import httpx
    _HAS_HTTPX = True
except Exception:  # pragma: no cover
    _HAS_HTTPX = False


class L2CByparr(Fetcher):
    """Tier 2c — Byparr (Camoufox-based FlareSolverr replacement). Turnstile worker.

    Byparr runs as a separate service exposing a FlareSolverr-compatible API.
    Set CRAWLER_BYPARR_URL (e.g. http://localhost:8191) and add L2C to ENABLED_TIERS.
    Higher latency — used only for hard captcha targets, which are rare.
    """

    name = "byparr"
    tier = "L2C"

    def __init__(self, byparr_url: str = "", timeout: float = 60.0):
        self.byparr_url = byparr_url.rstrip("/")
        self.timeout = max(timeout, 60.0)  # Byparr is slow; give it room
        self.enabled = bool(byparr_url) and _HAS_HTTPX
        if not byparr_url:
            self.disabled_reason = "CRAWLER_BYPARR_URL not set"
        elif not _HAS_HTTPX:
            self.disabled_reason = "httpx not installed"

    async def fetch(self, url: str, *, proxy: str | None = None) -> FetchResult:
        if not self.enabled:
            return FetchResult(url=url, tier=self.tier, ok=False,
                               reason=f"disabled:{self.disabled_reason}")
        t0 = time.monotonic()
        payload = {"cmd": "request.get", "url": url, "maxTimeout": int(self.timeout * 1000)}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.post(f"{self.byparr_url}/v1", json=payload)
                data = r.json()
            sol = data.get("solution", {}) or {}
            html = sol.get("response", "") or ""
            status = sol.get("status") or (200 if html else None)
            return FetchResult(url=url, tier=self.tier, status=status, html=html,
                               content_length=len(html), elapsed_ms=int((time.monotonic() - t0) * 1000),
                               final_url=sol.get("url"))
        except Exception as e:
            return FetchResult(url=url, tier=self.tier, ok=False, reason=f"error:{type(e).__name__}",
                               elapsed_ms=int((time.monotonic() - t0) * 1000))
