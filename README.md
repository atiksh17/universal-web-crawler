# Universal Crawler

Managed, tiered web crawler behind an HTTP API. Give it a URL (or a list); it returns clean
content (markdown by default; raw HTML / metas / footer / link-tree on request). The client
never handles batching, rate-limiting, retries, or escalation — the system owns all of it. A
deterministic (no-LLM) classifier decides, per tier, whether it got real content or must
escalate, and enforces **zero false data** (a 200 that's really a checkpoint never counts).

**Client guide:** [`usage.md`](usage.md) — single + bulk, all parameters, output-shaping flags,
response formats.

**Benchmark (honest, random lead list):** ~72% scraped on the **free tiers alone** across a
truly-random 1000-domain sample (zero false data). On curated business lists it's higher
(~76–84%). Measured over ~12k URLs in production: **85.8%** verified-real, free tiers only.
The residue (persistent Vercel/CF checkpoints, captchas, dead/thin pages) is what the paid
L4 catch-all is for.

---

## Quick start

```bash
git clone https://github.com/atiksh17/universal-web-crawler crawler && cd crawler

bash setup.sh                 # venv + pinned deps + Chrome + .env
nano .env                     # set CRAWLER_CHROME_PATH; add L4 key/zone if you have them
.venv/bin/python smoke.py     # sanity check (no server needed)

.venv/bin/uvicorn app.api:app --host 0.0.0.0 --port 8000
```

```bash
curl -X POST localhost:8000/scrape -H 'content-type: application/json' \
  -d '{"url":"https://example.com"}'
```

Docker instead:

```bash
cp .env.example .env && nano .env
docker compose up -d --build      # image bundles chromium at /usr/bin/chromium
```

> **Requires Python 3.12.** nodriver fails to import on 3.14. The Dockerfile pins it; for
> native installs use `python3.12`.
>
> **Do NOT use snap chromium on a bare VPS.** Snap's cold start (squashfs mount + first-run)
> is slow — under concurrent launches it exceeds nodriver's ~3s connect window, so cold
> browsers throw "Failed to connect to browser" (warm works — that asymmetry is the tell).
> Standalone Chrome cold-starts sub-second and fits the window. `setup.sh` installs it:
> ```bash
> wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
> apt-get install -y ./google-chrome-stable_current_amd64.deb
> # then set CRAWLER_CHROME_PATH=/usr/bin/google-chrome-stable
> ```
> The Docker image uses the non-snap apt `chromium`, so containers are unaffected.

Run it as a service so it survives reboots:

```bash
sudo tee /etc/systemd/system/crawler.service >/dev/null <<EOF
[Unit]
Description=Universal Crawler
After=network.target
[Service]
WorkingDirectory=/opt/crawler
ExecStart=/opt/crawler/.venv/bin/uvicorn app.api:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
EOF
sudo systemctl enable --now crawler
```

---

## The ladder

| Tier | Tool | Cost | Beats |
|------|------|------|-------|
| **L1** | curl_cffi (browser-impersonation, no browser) | free | plain fetch + TLS-fingerprint Cloudflare |
| **L2** | Nodriver (headless Chrome, stealth) | free | JS render + JS challenges + most bot walls |
| **L4** | Bright Data Web Unlocker (catch-all) | paid, **pay-on-success** | the hard residue (DataDome/Turnstile/etc.) |

L1 carries the large majority with no browser at all (~0.7s/URL). L2 only fires when L1 can't
render or gets blocked (~5–10× slower). L4 catches the remainder — and only bills on success.

**Note:** there is no L3. A residential-proxy tier and a Camoufox tier were both tried and
removed (proxy can't beat challenges; Camoufox gave ~1% unreliable lift for heavy cost). The
free system is L1+L2; the paid catch-all is L4. `L2B`/`L2C` remain dormant boilerplate.

### Turn on L4 (Bright Data Web Unlocker)

Edit `.env`, no code change:

```
CRAWLER_WEB_UNLOCKER_KEY=<key>
CRAWLER_WEB_UNLOCKER_ZONE=<zone>
CRAWLER_ENABLED_TIERS=L1,L2,L4
```

Sites that fall through L1+L2 then route to L4 and get unlocked (billed only on success).
With L4 off, hard-walled sites simply return `bot_blocked` — never fake content.

---

## How escalation decides (no LLM) — negative-first, then positive-confirm

The classifier ([`app/classifier.py`](app/classifier.py)) enforces **zero false data**:

1. **Negative phase** — interstitial `<title>` match (ungated, e.g. "Vercel Security
   Checkpoint", "Just a moment") → block; then challenge-host / body signatures, but **only
   when the page isn't independently proven real** (a real page can embed a captcha widget).
   Signatures in [`app/signatures.py`](app/signatures.py) (Cloudflare, Vercel, DataDome,
   PerimeterX, Imperva, Akamai, AWS WAF, Kasada, Queue-it, reCAPTCHA, hCaptcha, …).
2. **Positive phase** (only if negatives are clean) — require real content: substantive text
   **and** a structural signal (same-origin link count / landmark / structured-data), or all
   three structural signals for design-heavy thin-text pages.

Any failure escalates to the next enabled tier. The same gate judges L4 output too, so a
paid-but-still-blocked page is never counted as a scrape.

Routing is failure-aware: a **captcha** signature (Turnstile/hCaptcha/reCAPTCHA/DataDome/
PerimeterX/AWS WAF/Kasada) skips straight to a solver tier — a browser hop can't beat a human
captcha. JS challenges (Cloudflare/Vercel) do escalate to the browser tier, because a
browser + wait clears them.

Concurrency: global cap + per-domain spacing ([`app/throttle.py`](app/throttle.py)); L2
launches a fresh Chrome per fetch (no shared-session collisions) capped by
`CRAWLER_BROWSER_CONCURRENCY`.

## Browser stealth (validated)

No chromedriver/Selenium. Verified live headless: `navigator.webdriver=False`, `HeadlessChrome`
stripped from the UA → reports as normal `Chrome`, real plugins/languages/`window.chrome`.

---

## Scale knobs (the only things you change per box)

All live in `.env` — no code edits to scale. Measured on a 6-vCPU box, tuning
`BROWSER_CONCURRENCY` 2→5 + `NAV_TIMEOUT_S` 25→12 gave **2.4× throughput** (24.5→10.3 min for
1000 URLs) at the same coverage; CPU (not memory) is the ceiling there.

| Var | Laptop | 6-vCPU VPS (tuned) |
|-----|--------|--------------------|
| `CRAWLER_GLOBAL_CONCURRENCY` | 4 | 32 |
| `CRAWLER_WORKER_COUNT` | 4 | 32 |
| `CRAWLER_BROWSER_CONCURRENCY` | 2 | 5 (Chrome is CPU+RAM-heavy; ~match vCPU) |
| `CRAWLER_NAV_TIMEOUT_S` | 25 | 12 (lead lists have many dead/parked domains) |
| `CRAWLER_PER_DOMAIN_CONCURRENCY` | 2 | 2–4 (politeness) |

---

## API

Single + bulk, with output-shaping flags (markdown/html/metas/footer/link-tree). **Full client
guide: [`usage.md`](usage.md).** Quick reference:

```bash
# single — synchronous; returns shaped payload (domain, markdown, quality, bot_blocked, tier)
curl -X POST localhost:8000/scrape -H 'content-type: application/json' \
  -d '{"url":"https://example.com", "meta":true, "endpoints":true}'

# bulk — dump URLs, get a job_id; shaping flags remembered for /results
curl -X POST localhost:8000/scrape/bulk -H 'content-type: application/json' \
  -d '{"urls":["https://a.com","https://b.com"], "html":true}'

curl localhost:8000/jobs/<job_id>             # progress: state/total/done/ok_count
curl localhost:8000/jobs/<job_id>/results     # final shaped results
curl localhost:8000/health                    # tier status
```

Output-shaping flags (all default off; markdown always returned): `endpoints`, `meta`,
`footerHtml`, `html`, `selector` (scopes `html`, requires `html:true`). See `usage.md`.

**Mounting behind a gateway / into another app:** `app/api.py` is a plain FastAPI app —
put any reverse proxy in front of it, or import its router pieces into a larger service.

### Live progress / benchmarking

`scripts/bench_progress.py` submits a URL file and renders a live progress bar with
per-interval throughput (to verify speed is constant):

```bash
python scripts/bench_progress.py your_urls.txt http://localhost:8000 5
```

Benchmark your own list without the server:

```bash
.venv/bin/python benchmark.py your_urls.txt   # one URL per line
```

Prints free-tier coverage %, tier funnel, latency, page weight, and every failure with its
reason. Run this on your real target list to get production coverage + cost numbers.

---

## Layout

```
app/
  api.py          FastAPI server — routes + lifecycle (the entrypoint)
  escalator.py    walks the tiers for one URL; failure-signature-aware routing
  classifier.py   deterministic success/fail gate (zero false data)
  signatures.py   block-page fingerprints: interstitial titles, challenge hosts, body strings
  shape.py        HTML -> markdown + optional metas/footer/link-tree/scoped-html
  jobs.py         async queue + worker pool for bulk jobs
  store.py        SQLite job/result store
  throttle.py     global + per-domain concurrency and spacing
  config.py       all knobs, from env (CRAWLER_ prefix)
  models.py       FetchResult / Outcome / JobState
  fetchers/       one module per tier: l1_curl, l2_nodriver, l4_unlocker are live.
                  l2b_camoufox / l2c_byparr / l3_proxy are dormant — kept as reference
                  implementations, NOT wired into build_fetchers(); enabling them in
                  CRAWLER_ENABLED_TIERS does nothing until you add their branch back.
  proxy/          proxy pool (retired tier; interface kept)
benchmark.py      offline coverage benchmark over a URL file
smoke.py          no-server sanity check
scripts/          bench_progress.py — live progress bar against a running server
```

## What's tested vs not

- **Tested live:** L1, L2 (stealth + JS render + concurrent bulk), API single/bulk + output
  shaping, queue + throttle + SQLite store, classifier (negative+positive), 1000-URL random
  benchmark with resource monitoring, ~12k URLs in production.
- **Built but not live-verified:** L4 (needs your Bright Data creds), L2B/L2C (dormant
  boilerplate).

## Known limits

- **The SQLite store keeps every scraped page's HTML forever.** At volume `crawler.db` grows
  into the gigabytes — add a retention/prune job, or move HTML to object storage, before
  running long-lived bulk workloads.
- **Bulk shaping flags are held in memory.** Restart the server between submit and
  `/results` and the job falls back to markdown-only; re-submit to re-shape.
- **The queue is in-process** (`asyncio.Queue`). A restart mid-job loses pending URLs and the
  job stays `running`. `CRAWLER_QUEUE_BACKEND=redis` is a stub, not implemented — swap
  `app/jobs.py` to arq when you need durability.

## Why there's no L3 (free escalation tier)

Two candidates were built and **removed** after testing:

- **Residential proxy** — the residue past L2 is *challenge* walls (DataDome/Turnstile/Vercel),
  which an IP swap doesn't solve. Too weak for the real residue, redundant where L2 wins.
- **Camoufox (hardened Firefox)** — gave ~1/100 lift, probabilistic, overlapped L4's job, and
  added latency + a playwright-FF crash to maintain. Not worth it.

Conclusion: free = L1+L2; the catch-all is L4 (Bright Data). Persistent Vercel/CF checkpoints
and captchas are genuinely L4 work, not a free win.

## Docs

- [`usage.md`](usage.md) — client API guide (single/bulk, parameters, output shaping).
- [`features.md`](features.md) — planned: client feedback loop for production-driven accuracy.
