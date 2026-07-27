#!/usr/bin/env python3
"""Black-box bulk benchmark client with live progress + speed tracking.

The server does ALL the work (queue, throttle, L1->L2 escalation, classify). This script
only submits a URL list and polls the job, rendering a live progress bar with real numbers
so you can watch throughput and confirm it's constant (not fluctuating).

Usage:
    python scripts/bench_progress.py URLS_FILE [BASE_URL] [POLL_SECONDS]
    python scripts/bench_progress.py my_urls.txt http://localhost:8000 3

BASE_URL defaults to http://localhost:8000. If the crawler sits behind a proxy that adds a
path prefix, include it in BASE_URL (e.g. http://host/web-crawl).
"""
import collections
import json
import sys
import time
import urllib.request

URLS_FILE = sys.argv[1] if len(sys.argv) > 1 else "urls.txt"
BASE = (sys.argv[2] if len(sys.argv) > 2 else "http://localhost:8000").rstrip("/")
POLL = float(sys.argv[3]) if len(sys.argv) > 3 else 3.0


def api(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def bar(frac, width=34):
    fill = int(frac * width)
    return "[" + "#" * fill + "-" * (width - fill) + "]"


def main():
    urls = [l.strip() for l in open(URLS_FILE) if l.strip()]
    n = len(urls)
    print(f"submitting {n} URLs to {BASE} ...")
    job = api("POST", "/crawl/bulk", {"urls": urls})["job_id"]
    print(f"job_id={job}\n")

    t0 = time.time()
    last_t, last_done = t0, 0
    samples = []  # (elapsed, interval_rate) — to judge speed constancy
    while True:
        time.sleep(POLL)
        st = api("GET", f"/jobs/{job}")
        done, total = st.get("done", 0), st.get("total", n)
        ok = st.get("ok_count", 0)
        now = time.time()
        elapsed = now - t0
        interval_rate = (done - last_done) / max(1e-9, now - last_t)
        avg_rate = done / max(1e-9, elapsed)
        remaining = total - done
        eta = remaining / avg_rate if avg_rate > 0 else 0
        samples.append((round(elapsed), round(interval_rate, 1)))
        print(f"{bar(done/total)} {done:>4}/{total} ({100*done//total:>3}%) | "
              f"ok={ok:<4} | now {interval_rate:5.1f} url/s | avg {avg_rate:5.1f} url/s | "
              f"ETA {eta:5.0f}s | t+{elapsed:5.0f}s", flush=True)
        last_t, last_done = now, done
        if st.get("state") == "done" or done >= total:
            break

    total_time = time.time() - t0
    res = api("GET", f"/jobs/{job}")["results"]
    ok = [r for r in res if r["ok"]]
    fail = [r for r in res if not r["ok"]]
    tiers = collections.Counter(r["tier"] for r in ok)
    reasons = collections.Counter(r["reason"] for r in fail)

    print("\n" + "=" * 60)
    print(f"DONE  {len(ok)}/{len(res)} scraped = {round(100*len(ok)/len(res))}%  (zero-false-data gate)")
    print(f"time  {total_time:.0f}s   throughput {len(res)/total_time:.1f} url/s")
    print(f"success by tier: {dict(tiers)}")
    print("\nspeed over time (elapsed_s -> url/s in that interval) — check it's flat:")
    print("  " + "  ".join(f"{e}s:{r}" for e, r in samples))
    print("\nfailure reasons:")
    for reason, c in reasons.most_common():
        print(f"  {c:>4}x  {reason}")
    print("=" * 60)


if __name__ == "__main__":
    main()
