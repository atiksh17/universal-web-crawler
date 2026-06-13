from __future__ import annotations

from .classifier import classify
from .config import Settings
from .fetchers import build_fetchers
from .models import FetchResult, Outcome
from .proxy import ProxyPool
from .signatures import CAPTCHA_SIGNATURES

# Tiers that use a residential proxy from the pool.
PROXY_TIERS = {"L3"}
# Tiers that solve captcha challenges (an IP swap won't).
CAPTCHA_TIERS = ["L2C", "L4"]


class Escalator:
    """Walks the enabled tiers for one URL, gated by the deterministic classifier.

    Routing is failure-signature-aware: a captcha block skips straight to a captcha
    solver (a residential IP can't beat a Turnstile), so we don't waste a paid L3 hop.
    """

    def __init__(self, settings: Settings):
        self.s = settings
        self.fetchers = build_fetchers(settings)
        self.order = [t for t in settings.tiers if t in self.fetchers]
        self.pool = ProxyPool(settings.proxy_list)

    def tier_status(self) -> dict:
        return {
            t: {"name": f.name, "enabled": f.enabled, "reason": f.disabled_reason}
            for t, f in self.fetchers.items()
        }

    def _next_tiers(self, current: str, last_reason: str) -> list[str]:
        """Given the current tier and why it failed, what remains to try (in order)."""
        idx = self.order.index(current)
        remaining = self.order[idx + 1:]
        # Captcha detected: jump to the first available captcha solver, skip proxy-only tiers.
        sig = last_reason.split(":", 1)[1] if last_reason.startswith("block:") else ""
        if sig in CAPTCHA_SIGNATURES:
            preferred = [t for t in remaining if t in CAPTCHA_TIERS]
            others = [t for t in remaining if t not in CAPTCHA_TIERS and t not in PROXY_TIERS]
            return preferred + others
        return remaining

    async def _run_tier(self, tier: str, url: str) -> FetchResult:
        fetcher = self.fetchers[tier]
        if not fetcher.enabled:
            return FetchResult(url=url, tier=tier, ok=False,
                               reason=f"disabled:{fetcher.disabled_reason}")
        proxy = self.pool.pick() if tier in PROXY_TIERS else None
        if tier in PROXY_TIERS and not proxy:
            return FetchResult(url=url, tier=tier, ok=False, reason="no_proxy_available")
        res = await fetcher.fetch(url, proxy=proxy)
        if proxy and not res.ok and res.reason.startswith("http_4"):
            self.pool.mark_ban(proxy)
        return res

    async def crawl(self, url: str) -> Outcome:
        outcome = Outcome(url=url)
        tier = self.order[0] if self.order else None
        visited: set[str] = set()
        while tier and tier not in visited:
            visited.add(tier)
            res = await self._run_tier(tier, url)
            ok, reason = (True, "")
            if res.ok or res.reason == "":
                ok, reason = classify(
                    res, min_text_len=self.s.min_text_len,
                    min_render_text_len=self.s.min_render_text_len,
                )
            else:
                ok, reason = False, res.reason
            res.ok = ok
            res.reason = reason
            outcome.attempts.append(res.summary())
            outcome.elapsed_ms += res.elapsed_ms
            if ok:
                outcome.ok = True
                outcome.tier = tier
                outcome.status = res.status
                outcome.html = res.html
                outcome.reason = ""
                return outcome
            # escalate
            nxt = self._next_tiers(tier, reason)
            tier = next((t for t in nxt if t not in visited), None)
            outcome.tier = res.tier
            outcome.status = res.status
            outcome.reason = reason
        return outcome  # all tiers exhausted; ok stays False

    async def aclose(self) -> None:
        for f in self.fetchers.values():
            await f.aclose()
