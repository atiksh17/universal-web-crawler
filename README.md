# Universal Crawler

Managed, tiered web crawler behind an API. Give it a URL (or a list); it returns the full
rendered HTML. The client never handles batching, rate-limiting, retries, or escalation —
the system owns all of it. A deterministic (no-LLM) classifier decides, per tier, whether
it got real content or must escalate.

**Benchmark:** 97% of a deliberately-hard 32-site mix (incl. Amazon, Walmart, Zillow, news,
SaaS) cleared on the free tiers alone; one DataDome site needed the paid catch-all.

---

## The ladder — 3 tiers
| Tier | Tool | Cost | Beats |
|------|------|------|-------|
| **L1** | curl_cffi (browser-impersonation, no browser) | free | plain fetch + TLS-fingerprint Cloudflare |
| **L2** | Nodriver (headless Chrome, stealth) | free | JS render + JS challenges + most bot walls |
| **L4** | Bright Data Web Unlocker (catch-all) | $1.50/1k, **pay-on-success** | the hard residue (DataDome/Turnstile/etc.) |

L1 carries ~88% of traffic with no browser at all (fast, free). L2 only fires when L1 can't
render or gets blocked. L4 catches the small remainder — and only bills when it succeeds.

Optional dormant modules (off by default, ready to switch on): `L2B` Camoufox, `L2C` Byparr,
`L3` residential-proxy browser. L3 is intentionally shelved — see end of file.

---

## Deploy on the VPS — fastest path

### Option A — native (recommended, tested)
```bash
git clone <your-repo-url> crawler && cd crawler
bash setup.sh                 # venv + pinned deps + chromium + .env
nano .env                     # set CRAWLER_CHROME_PATH, add L4 key/zone, bump concurrency
.venv/bin/python smoke.py     # sanity check (no server)
.venv/bin/uvicorn app.api:app --host 0.0.0.0 --port 8000
```
Run it as a service so it survives reboots:
```bash
sudo tee /etc/systemd/system/crawler.service >/dev/null <<EOF
[Unit]
Description=Universal Crawler
After=network.target
[Service]
WorkingDirectory=$(pwd)
ExecStart=$(pwd)/.venv/bin/uvicorn app.api:app --host 0.0.0.0 --port 8000
Restart=always
[Install]
WantedBy=multi-user.target
EOF
sudo systemctl enable --now crawler
```

### Option B — Docker (parity image; build untested locally — verify on first build)
```bash
git clone <your-repo-url> crawler && cd crawler
cp .env.example .env && nano .env     # add L4 creds, set concurrency
docker compose up -d --build
```
The image bundles chromium at `/usr/bin/chromium`. If the build trips on an apt package name,
adjust the list in `Dockerfile` (Debian bookworm names).

> **Requires Python 3.12.** nodriver fails to import on 3.14. The Dockerfile pins it; for
> native installs use `python3.12`.
>
> **Do NOT use snap chromium on a bare VPS.** Snap's cold start (squashfs mount + first-run)
> is slow — and under concurrent launches it exceeds nodriver's ~3s connect window, so cold
> browsers throw "Failed to connect to browser" (warm works — that asymmetry is the tell).
> Standalone Chrome cold-starts sub-second and fits the window. Install it instead:
> ```bash
> wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
> apt-get install -y ./google-chrome-stable_current_amd64.deb
> # then set CRAWLER_CHROME_PATH=/usr/bin/google-chrome-stable
> ```
> The Docker image already uses the non-snap apt `chromium`, so containers are unaffected.

---

## Scale knobs (the only things you change for the VPS)
All live in `.env` — no code edits to scale:
| Var | Laptop | VPS (tune to vCPU) |
|-----|--------|--------------------|
| `CRAWLER_GLOBAL_CONCURRENCY` | 4 | 16–32 |
| `CRAWLER_BROWSER_CONCURRENCY` | 2 | 4–10 (Chrome is CPU-heavy; match vCPU) |
| `CRAWLER_WORKER_COUNT` | 4 | 16–32 |
| `CRAWLER_PER_DOMAIN_CONCURRENCY` | 2 | 2–4 (politeness) |

---

## API
```bash
# single — synchronous, returns HTML + which tier produced it
curl -X POST localhost:8000/scrape -H 'content-type: application/json' \
  -d '{"url":"https://example.com"}'

# bulk — dump URLs, get a job_id; the system batches/throttles/escalates
curl -X POST localhost:8000/scrape/bulk -H 'content-type: application/json' \
  -d '{"urls":["https://a.com","https://b.com"]}'

curl localhost:8000/jobs/<job_id>             # status + per-url summary
curl localhost:8000/jobs/<job_id>/results     # full HTML
curl localhost:8000/health                    # tier status
```

## Turn on L4 (Bright Data Web Unlocker)
Edit `.env`, no code change:
```
CRAWLER_WEB_UNLOCKER_KEY=<key>
CRAWLER_WEB_UNLOCKER_ZONE=<zone>
CRAWLER_ENABLED_TIERS=L1,L2,L4
```
Sites that fall through L1+L2 then route to L4 and get unlocked (billed only on success).

## Benchmark your own URLs
```bash
.venv/bin/python benchmark.py your_urls.txt   # one URL per line
```
Prints free-tier coverage %, tier funnel, latency, page weight, and every failure with its
reason. Run this on your real target list to get production coverage + cost numbers.

---

## How escalation decides (no LLM)
Per tier, in order, any failure escalates: HTTP status → block-signature scan (gated on short
content, so a page that merely *mentions/embeds* a vendor isn't falsely flagged) → empty-shell
detection (L1 can't run JS) → render-completeness. See `app/classifier.py`.

Concurrency: global cap + per-domain spacing (`app/throttle.py`); L2 launches a fresh Chrome
per fetch (no shared-session collisions) capped by `CRAWLER_BROWSER_CONCURRENCY`.

## Browser stealth (validated)
No chromedriver/Selenium. Verified live headless: `navigator.webdriver=False`, `HeadlessChrome`
stripped from the UA → reports as normal `Chrome`, real plugins/languages/`window.chrome`.

## What's tested vs not
- **Tested live:** L1, L2 (stealth + JS render + concurrent bulk), API single/bulk, queue +
  throttle + SQLite store, classifier, benchmark (97%).
- **Built but not live-verified:** L4 (needs your Bright Data creds), L2B/L2C/L3 (dormant
  boilerplate), the Docker build (daemon was down locally).
- **Deferred (your call):** phase-2 selective extraction (body/footer/selectors via selectolax).

## Why L3 (residential browser) is shelved
The residue past L2 is dominated by *challenge* walls (DataDome/PerimeterX/Turnstile) that a
residential IP alone doesn't solve — L4 does, pay-on-success. L2 stealth already beats plain
IP-reputation blocks. So a raw residential tier is too weak for the real residue and redundant
where L2 wins, while carrying fixed IP cost + bandwidth-on-failure + pool ops. Code kept as
boilerplate (`app/fetchers/l3_proxy.py`, `app/proxy/`); enable only if a measured,
IP-reputation-heavy URL set ever justifies it.

Architecture & cost model: [`../memory/project/architecture.md`](../memory/project/architecture.md)
(if cloned standalone, that doc lives in the parent workspace).
