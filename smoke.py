"""Smoke test — runs known site types straight through the escalator (no API server).

    python smoke.py

Asserts each URL lands on a sensible tier and returns substantive HTML. Use this to
sanity-check the escalation logic before pointing the benchmark at your real URL list.
"""
from __future__ import annotations

import asyncio

from app.config import get_settings
from app.escalator import Escalator

# (label, url, expectation) — verified live; these are what a healthy install prints.
SAMPLES = [
    ("real_content", "https://www.a16z.com", "ok at L1 (no browser needed)"),
    ("js_rendered", "https://quotes.toscrape.com/js/", "L1 thin -> L2 renders -> ok"),
    # Deliberate negative: a page with no same-origin links, no landmark and no structured
    # data cannot be told apart from an interstitial, so the gate rejects it rather than
    # risk false data. ok=False here is CORRECT, not a broken install.
    ("thin_page", "https://example.com", "rejected by design -> ok=False"),
]


async def main():
    s = get_settings()
    esc = Escalator(s)
    print(f"Enabled tiers: {esc.order}")
    print(f"Tier status: {esc.tier_status()}\n")
    for label, url, expect in SAMPLES:
        o = await esc.crawl(url)
        path = " -> ".join(a["tier"] + (":" + a["reason"] if a["reason"] else "") for a in o.attempts)
        print(f"[{label}] {url}")
        print(f"  expect : {expect}")
        print(f"  result : ok={o.ok} final_tier={o.tier} text_len={len(o.html)} chars")
        print(f"  path   : {path}\n")
    await esc.aclose()


if __name__ == "__main__":
    asyncio.run(main())
