"""Fetchers — one per tier, all sharing the Fetcher interface.

The escalator does not care which tool a tier uses; it only calls `fetch(url)` and
hands the result to the classifier. That is what lets L3/L4 drop in by config alone.
"""
from __future__ import annotations

from ..config import Settings
from .base import Fetcher
from .l1_curl import L1Curl
from .l2_nodriver import L2Nodriver
from .l2b_camoufox import L2BCamoufox
from .l2c_byparr import L2CByparr
from .l3_proxy import L3ProxyBrowser
from .l4_unlocker import L4Unlocker


def build_fetchers(s: Settings) -> dict[str, Fetcher]:
    """Instantiate only the tiers listed in CRAWLER_ENABLED_TIERS, in order."""
    registry: dict[str, Fetcher] = {}
    for tier in s.tiers:
        if tier == "L1":
            registry[tier] = L1Curl(impersonate=s.impersonate, timeout=s.nav_timeout_s)
        elif tier == "L2":
            registry[tier] = L2Nodriver(
                headless=s.headless, nav_timeout_s=s.nav_timeout_s,
                settle_ms=s.settle_ms, block_resources=s.block_resources,
                chrome_path=s.chrome_path, browser_concurrency=s.browser_concurrency,
            )
        elif tier == "L2B":
            registry[tier] = L2BCamoufox(
                headless=s.headless, nav_timeout_s=s.nav_timeout_s,
                block_resources=s.block_resources,
            )
        elif tier == "L2C":
            registry[tier] = L2CByparr(byparr_url=s.byparr_url, timeout=s.nav_timeout_s)
        elif tier == "L3":
            registry[tier] = L3ProxyBrowser(
                headless=s.headless, nav_timeout_s=s.nav_timeout_s,
                settle_ms=s.settle_ms, block_resources=s.block_resources,
                chrome_path=s.chrome_path,
            )
        elif tier == "L4":
            registry[tier] = L4Unlocker(
                api_url=s.web_unlocker_url, key=s.web_unlocker_key, zone=s.web_unlocker_zone,
                timeout=s.nav_timeout_s,
            )
    return registry
