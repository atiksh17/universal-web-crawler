# Usage — calling the crawler API

Client-facing guide. Give the crawler a URL (or a list); it returns clean content. The
system owns batching, rate-limiting, retries, and tier escalation — the caller only sends
URLs and (optionally) picks how the output is shaped.

Start the server (`uvicorn app.api:app --host 0.0.0.0 --port 8000`) and every endpoint below
hangs off its root.

**Base URL: `http://localhost:8000`** — substitute your own host/port, or whatever path prefix
your reverse proxy adds if you put one in front.

> **Endpoints:** `POST /crawl` (single, sync), `POST /crawl/bulk` (many, async),
> `GET /jobs/{id}` (progress), `GET /jobs/{id}/results` (final), `GET /health` (tier status).

---

## Authentication

Depends on which door you come in by — there are two, and only one needs a key.

| Calling from | Base URL | Key |
|---|---|---|
| **Outside the box** (your laptop, a Vercel app, anything on the internet) | `https://crawl.lrc-limited.com` | **required** — `X-API-Key: <key>` |
| **Inside the box** (another container on the `coolify` network) | `http://web-crawler:8000` | none — the port is not published, so the docker network is the boundary |

```bash
curl -X POST https://crawl.lrc-limited.com/crawl \
  -H "X-API-Key: $CRAWLER_API_KEY" \
  -H 'content-type: application/json' \
  -d '{"url":"https://example.com"}'
```

A public call with a missing or wrong key gets `401`. `GET /health` is exempt from both,
so it stays usable as an uptime check. The examples below use `localhost` for local dev —
against the public URL, add the header to every one of them.

The key lives in Coolify → **Projects → web-crawler → Environment Variables** as
`CRAWLER_API_KEY`. Rotating it is: change it there, redeploy, update callers.

---

## Option 1 — single URL (synchronous)

Best for one-off lookups. Blocks until the page is scraped, returns the shaped payload.

```bash
curl -X POST http://localhost:8000/crawl \
  -H 'content-type: application/json' \
  -d '{"url":"https://example.com"}'
```

Response (default = markdown only):
```json
{
  "domain": "example.com",
  "markdown": "# Example...\n\n- [link](https://...)",
  "quality": "ok",
  "bot_blocked": false,
  "tier": "L1"
}
```

## Option 2 — bulk list (asynchronous job)

Best for lead lists. Submit all URLs, get a `job_id` back immediately, poll for progress,
then fetch the shaped results. The system batches/throttles/escalates internally.

```bash
# 1. submit
curl -X POST http://localhost:8000/crawl/bulk \
  -H 'content-type: application/json' \
  -d '{"urls":["https://a.com","https://b.com"], "meta":true, "endpoints":true}'
# -> {"job_id":"abc123...","total":2}

# 2. poll progress
curl http://localhost:8000/jobs/abc123
# -> {"state":"running","total":2,"done":1,"ok_count":1, ...}

# 3. fetch final shaped results (uses the flags sent at submit)
curl http://localhost:8000/jobs/abc123/results
```

`state` goes `running` -> `done`. Each result in `/results` also carries `url` and `tier`
so you can map it back.

### Per-job limits

A job is bounded so one big list cannot exhaust the service (see `.env.example`):

| Limit | Default | What happens at the limit |
|---|---|---|
| URLs per job | 5,000 | `POST /crawl/bulk` returns **413** — split the batch |
| Bytes per page | 2 MB | page stored truncated, `html_state: "truncated"` |
| Bytes per job | 300 MB | later rows keep their metadata but drop the body, `html_state: "dropped"` |
| Job retention | 24 h | the job and its rows are purged |

`content_length` always reports the page's **true** size, so a trimmed result never
reads as a genuinely short one — compare it against `html_state` to tell them apart.

`GET /jobs/{id}` is safe to poll at any rate: it never reads page bodies, so its cost
does not grow with the job.

`GET /jobs/{id}/results` returns **every** row by default, streamed — the response is
the same `{"job_id", "results": [...]}` object it always was, just written incrementally
so neither side has to hold the whole corpus. Pass `?limit=` to page through it
explicitly instead:

```bash
curl 'http://localhost:8000/jobs/abc123/results?limit=500&offset=0'
# -> {..., "total": 1200, "offset": 0, "limit": 500, "next_offset": 500}
```

Follow `next_offset` until it comes back `null`.

---

## Output shaping (parsing options)

Every response **always** includes:

| field | meaning |
|-------|---------|
| `domain` | hostname of the URL |
| `markdown` | the page content as clean markdown (headings, paragraphs, links, lists) |
| `quality` | `ok` \| `needs_retry` \| `bot_blocked` |
| `bot_blocked` | `true` if a provider challenge/block was detected |
| `error` | failure reason — present only when `quality != ok` (e.g. `block:vercel_checkpoint`, `thin_content_after_render`, `nav_timeout`) |
| `tier` | which tier produced it (`L1`/`L2`/`L4`) |

Add **optional** fields by setting flags in the request body. All default `false`:

| flag | adds field | contents |
|------|-----------|----------|
| `endpoints: true` | `endpoints` | on-site link tree — unique same-origin links as `[{url, text}]` |
| `meta: true` | `metas` | array of `<meta>` tags (`name`/`property`/`content`/…) |
| `footerHtml: true` | `footerHtml` | raw `<footer>` HTML, or `null` if none |
| `html: true` | `html` | full rendered HTML |
| `selector: "<css>"` | (scopes `html`) | returns only the HTML of elements matching the CSS selector. **Requires `html: true`** (else `400`). |

`mdLinks` shapes the always-on `markdown` instead of adding a field, so it's the one flag that
isn't a plain `false` default:

| value | anchor `<a href="/team">leadership team</a>` renders as |
|---|---|
| unset (default) | **auto** — `text` when `endpoints: true`, `inline` when it's off |
| `true` / `"inline"` | `[leadership team](/team)` — the full markdown link |
| `false` / `"text"` | `leadership team` — label kept, `(href)` dropped |
| `"strip"` | *(nothing)* — no href, no label |

**`"strip"` is for pages you're reading as prose, not as a site map.** A block that was only
links — nav bar, footer menu, link list — collapses to nothing and disappears, which is the point.
But an anchor inside a sentence takes its words with it: *"Meet our leadership team, and read the
news."* becomes *"Meet our, and read the."* (spacing is closed up, the sense isn't). Use it when
`endpoints: true` is already giving you the URLs and you want the chrome gone; use `false` when the
markdown still has to read as English. It is never applied automatically.

### Examples

Markdown + meta tags + link tree:
```bash
curl -X POST http://localhost:8000/crawl -H 'content-type: application/json' \
  -d '{"url":"https://example.com", "meta":true, "endpoints":true}'
```

Full raw HTML:
```bash
curl -X POST http://localhost:8000/crawl -H 'content-type: application/json' \
  -d '{"url":"https://example.com", "html":true}'
```

Only a scoped slice of HTML (e.g. just the pricing block):
```bash
curl -X POST http://localhost:8000/crawl -H 'content-type: application/json' \
  -d '{"url":"https://example.com", "html":true, "selector":".prices"}'
```

Bulk with shaping (flags remembered, applied at `/results`):
```bash
curl -X POST http://localhost:8000/crawl/bulk -H 'content-type: application/json' \
  -d '{"urls":["https://a.com","https://b.com"], "footerHtml":true, "html":true}'
```

---

## Interpreting `quality` / what counts as a successful scrape

The system enforces **zero false data**: a `200` that's actually a provider checkpoint or
an empty shell is **not** reported as success.

- `quality: "ok"` — real content was verified (substantive text + structure). Trust `markdown`.
- `quality: "bot_blocked"` — a challenge/captcha wall was hit (`error` names the provider,
  e.g. `block:recaptcha`). Free tiers can't pass it; needs the paid catch-all (L4).
- `quality: "needs_retry"` — no usable content (thin/unrendered page, timeout, dead domain).

`markdown` is empty when content couldn't be retrieved.

---

## Notes / limits

- **Health/tiers:** `GET /health` shows which tiers are enabled, their escalation order, and
  why any disabled tier is disabled (e.g. missing L4 creds).
- **Free vs paid:** L1 (fast) + L2 (headless Chrome) are free and carry the large majority.
  L4 (Bright Data Web Unlocker) is the paid catch-all — enabled by the operator via creds;
  when off, hard-walled sites return `bot_blocked`.
- **Throughput:** L1 is ~0.7s/URL; L2 is several seconds. Bulk throughput depends on the
  operator's concurrency settings. Poll `/jobs/{id}` for live `done`/`total`.
- **Bulk shaping is set at submit time** and applied when you GET `/results`. To re-shape
  the same job differently, re-submit.
- **Results expire.** Jobs are purged 24 h after submit (`CRAWLER_JOB_RETENTION_HOURS`),
  so fetch `/results` in the same run rather than coming back for them tomorrow.
