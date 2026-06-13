"""Benchmark — run a URL set through the real escalator concurrently and report.

    python benchmark.py            # built-in diverse sample
    python benchmark.py urls.txt   # one URL per line

Reports: free-tier (L1+L2) coverage %, tier funnel, latency per tier, page weight,
and every failure with the reason (= which paid tier it would need). This is the
go/no-go instrument for the VPS — point it at your 300 real URLs.
"""
from __future__ import annotations

import asyncio
import statistics
import sys
import time

from app.config import get_settings
from app.escalator import Escalator

SAMPLE = [
    # static / simple
    "https://example.com", "https://example.org", "https://httpbin.org/html",
    "https://www.iana.org/", "https://books.toscrape.com",
    # JS-rendered
    "https://quotes.toscrape.com/js/", "https://quotes.toscrape.com/js/page/2/",
    # news / content
    "https://news.ycombinator.com", "https://www.bbc.com", "https://www.theverge.com",
    "https://techcrunch.com", "https://arstechnica.com",
    # SaaS / dev / company
    "https://stripe.com", "https://vercel.com", "https://github.com",
    "https://www.python.org", "https://fastapi.tiangolo.com", "https://nodejs.org",
    "https://www.cloudflare.com", "https://www.docker.com", "https://openai.com",
    "https://en.wikipedia.org/wiki/Web_scraping", "https://blog.cloudflare.com",
    "https://realpython.com",
    # commonly bot-protected (expected to stress / fail free tier)
    "https://www.amazon.com", "https://www.nike.com", "https://www.walmart.com",
    "https://www.bestbuy.com", "https://www.g2.com", "https://www.crunchbase.com",
    "https://www.indeed.com", "https://www.zillow.com",
]


async def main():
    urls = SAMPLE
    if len(sys.argv) > 1:
        with open(sys.argv[1]) as f:
            urls = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]

    s = get_settings()
    esc = Escalator(s)
    print(f"tiers={esc.order}  global_concurrency={s.global_concurrency}  "
          f"browser_concurrency={s.browser_concurrency}  urls={len(urls)}\n")

    sem = asyncio.Semaphore(s.global_concurrency)
    rows = []

    async def run(u):
        async with sem:
            o = await esc.crawl(u)
            last = o.attempts[-1] if o.attempts else {}
            rows.append({
                "url": u, "ok": o.ok, "tier": o.tier, "ms": o.elapsed_ms,
                "bytes": len(o.html), "text": last.get("text_len", 0),
                "reason": o.reason, "hops": len(o.attempts),
                "path": "->".join(a["tier"] for a in o.attempts),
            })

    t0 = time.monotonic()
    await asyncio.gather(*(run(u) for u in urls))
    wall = time.monotonic() - t0
    await esc.aclose()

    rows.sort(key=lambda r: (not r["ok"], r["tier"]))
    print(f"{'ok':5}{'tier':5}{'ms':>7}{'KB':>8}  {'path':10} url")
    print("-" * 90)
    for r in rows:
        print(f"{str(r['ok']):5}{r['tier']:5}{r['ms']:>7}{r['bytes']/1024:>8.1f}  "
              f"{r['path']:10} {r['url']}"
              + (f"  [{r['reason']}]" if not r['ok'] else ""))

    n = len(rows)
    ok = [r for r in rows if r["ok"]]
    fail = [r for r in rows if not r["ok"]]
    by_tier = {}
    for r in ok:
        by_tier.setdefault(r["tier"], []).append(r)

    print("\n" + "=" * 50)
    print(f"COVERAGE (free L1+L2): {len(ok)}/{n} = {100*len(ok)/n:.0f}%")
    print(f"wall time: {wall:.0f}s  ({wall/n:.1f}s/url avg)")
    print("\nFUNNEL (where successes landed):")
    for tier in esc.order:
        g = by_tier.get(tier, [])
        if g:
            lat = [r["ms"] for r in g]
            wt = statistics.mean(r["bytes"] for r in g) / 1024
            print(f"  {tier}: {len(g):2} sites  "
                  f"median {statistics.median(lat):>6.0f}ms  avg {wt:>6.1f}KB/page")
    if ok:
        allwt = statistics.mean(r["bytes"] for r in ok) / 1024
        print(f"  avg page weight (blocked): {allwt:.1f}KB")
    print(f"\nFAILURES ({len(fail)}) — would need paid tier (L3/L4):")
    for r in fail:
        print(f"  {r['url']}  -> {r['reason']}  (path {r['path']})")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
