# Deployment — how this crawler runs on the Contabo VPS

Infrastructure notes for *our* deployment. The upstream product docs are
[`README.md`](README.md) (how it works) and [`usage.md`](usage.md) (the client API).

---

## The shape: a private room behind the one public door

```
   internet                    coolify docker network (private)
      │
      ▼
  api.lrc-limited.com  ──/web/*──▶  web-crawler:8000
  (Coolify app "api")               (Coolify app "web-crawler")
   public · X-API-Key                no domain · no auth · not reachable from outside
```

The crawler is a **separate Coolify resource in its own container**. It has **no
public domain and no authentication of its own** — it is reachable only over the
internal `coolify` Docker network, and the only way in is `/web/*` on the API,
which enforces `X-API-Key`.

Stopping `web-crawler` in Coolify closes exactly one room: `/web/*` starts
answering **503** with a message naming the cause, and every other route on the
API is untouched. The proxy holds no in-process state, so the crawler can't take
the API down with it either.

## Why it's a separate container, not a router in the API

`world/api/README.md` says projects contribute *routers* to the one API server
rather than running their own. This is the documented exception, for reasons that
are specific and not stylistic:

- It **launches a fresh headless Chrome per fetch**. Crashes, memory spikes and
  zombie processes must not be able to touch the API process that fronts every
  other project.
- It needs `--shm-size=1g` and a chromium install — container-level concerns the
  API image has no business carrying.
- It must be **independently stoppable**, which is the whole point.

The rule it still honours: there is exactly **one public API server**. The crawler
is a private backing service, like Postgres — not a second front door.

---

## Coolify resource

| | |
|---|---|
| Project | **Projects** (`zvlqskt5cxzoqcwu67kwmwvr`) — ours, not `Apps`/`Core` |
| Environment | production (`ni5rponq8106e2wxa50mts4l`) |
| Application | **web-crawler** (`pq63yae6rk2uj6bvv38po9yb`) |
| Source | `github.com/atiksh17/universal-web-crawler`, branch **`master`**, push-to-deploy |
| Build | **Docker Compose** — `/docker-compose.coolify.yaml` |
| Internal hostname | **`web-crawler`** — stable across redeploys |
| Port | 8000, **not published to the host** |
| Domain | **none — deliberately private** |
| Health check | `GET /health` (needs `curl` in the image — it's there) |

### Three settings that are load-bearing

- **Compose build pack, not Dockerfile.** This is what makes the hostname
  stable. Coolify names Dockerfile-buildpack containers `<uuid>-<timestamp>` and
  the timestamp changes on **every** deploy, so anything pointing at it breaks on
  the next redeploy. Compose adds the **service name** as a network alias, so
  `web-crawler` resolves forever. `--network-alias` in custom docker options does
  **not** work — Coolify generates the alias list itself (verified in its
  generated compose).
- **`shm_size: 1gb`.** Docker's default `/dev/shm` is 64 MB. Chrome uses shared
  memory for renderer IPC and crashes or hangs under concurrency without it.
  Symptom if lost: L2 fails while L1 keeps working.
- **`curl` in the image.** Coolify's healthcheck shells out to `curl`/`wget`;
  `python:3.12-slim` ships neither, so a perfectly healthy app gets marked
  unhealthy and the deploy **rolls back**. Added to the Dockerfile.

---

## Tuning — conservative on purpose

The upstream README's "6-vCPU VPS (tuned)" column assumes a **dedicated** box.
This box also runs Supabase, Coolify, Traefik and Beszel, with ~6.4 GB RAM free
of 11 GB. Chrome is the heavy tenant, so the browser knobs start below upstream's
recommendation:

| Var | Upstream tuned | **Here** | Why |
|---|---|---|---|
| `CRAWLER_GLOBAL_CONCURRENCY` | 32 | **16** | shared CPU |
| `CRAWLER_WORKER_COUNT` | 32 | **16** | shared CPU |
| `CRAWLER_BROWSER_CONCURRENCY` | 5 | **3** | each Chrome is ~200-400 MB; 3 ≈ 1.2 GB peak |
| `CRAWLER_NAV_TIMEOUT_S` | 12 | **12** | lead lists carry many dead/parked domains |
| `CRAWLER_PER_DOMAIN_CONCURRENCY` | 2 | **2** | politeness |

Raise `BROWSER_CONCURRENCY` toward 5 once you've watched real load in Beszel.
Upstream measured **2.4x throughput** from tuning these, so there's headroom —
take it with evidence, not optimism.

### L4 is drop-in — nothing to build

`CRAWLER_ENABLED_TIERS=L1,L2` today: free tiers only, no credentials. The **paid
catch-all is fully wired and dormant** — `build_fetchers()` has the `L4` branch,
`config.py` has the settings, and `escalator` reports on `/health` why it's off.
Turning it on is three env vars in Coolify and a redeploy, **no code change**:

```
CRAWLER_WEB_UNLOCKER_KEY=<key>
CRAWLER_WEB_UNLOCKER_ZONE=<zone>
CRAWLER_ENABLED_TIERS=L1,L2,L4
```

With it off, hard-walled sites return `bot_blocked` — never fabricated content.

---

## The store is ephemeral — by decision

`CRAWLER_DB_URL=/srv/crawler.db` — **inside the container, no volume.** A redeploy
or restart wipes job history. **No retention policy, by decision** (2026-07-27).

It's also the safe default: the upstream README's own known limits state the
SQLite store **keeps every scraped page's HTML forever** and grows into gigabytes
with no prune job, so a volume without retention would make unbounded disk growth
permanent. Ephemeral self-limits. It costs little because the queue is in-process
anyway — a restart already loses in-flight jobs — and callers consume results
within minutes of submitting.

If that changes, a volume needs a retention rule first. Until then:
- Fetch `/web/jobs/{id}/results` promptly; don't treat the crawler as storage.
- A restart between submit and results means re-submitting.

Two related upstream limits that also survive here: **bulk shaping flags live in
memory** (a restart between submit and `/results` silently degrades that job to
markdown-only), and **the queue is an in-process `asyncio.Queue`** (a restart
mid-job strands the job in `running`). `CRAWLER_QUEUE_BACKEND=redis` is a stub —
swapping `app/jobs.py` to arq is the fix when durability is needed.

---

## Routes

Renamed from `/scrape` to `/crawl` on 2026-07-27 — "scrape" overloaded a term
that means something else in the event-os pipeline. `/scrape` and `/scrape/bulk`
remain as hidden, deprecated aliases so nothing breaks; drop them once nothing
calls them.

| Public (via the API) | Crawler internal |
|---|---|
| `POST /web/crawl` | `POST /crawl` |
| `POST /web/crawl/bulk` | `POST /crawl/bulk` |
| `GET /web/jobs/{id}` | `GET /jobs/{id}` |
| `GET /web/jobs/{id}/results` | `GET /jobs/{id}/results` |
| `GET /web/health` | `GET /health` |

`/web` is a **transparent proxy** — whatever follows `/web` is forwarded
unchanged, so a new crawler route needs no change on the API side.

---

## Operating it

```bash
# is the room open?
curl -H "X-API-Key: <key>" https://api.lrc-limited.com/web/health

# one page (synchronous — can take tens of seconds on a hard site)
curl -X POST https://api.lrc-limited.com/web/crawl \
  -H "X-API-Key: <key>" -H 'content-type: application/json' \
  -d '{"url":"https://example.com"}'

# many pages (returns immediately, then poll)
curl -X POST https://api.lrc-limited.com/web/crawl/bulk \
  -H "X-API-Key: <key>" -H 'content-type: application/json' \
  -d '{"urls":["https://a.com","https://b.com"]}'
```

Use `bulk` for anything beyond a handful of URLs: single `/crawl` is synchronous
and walks L1 → L2 (→ L4) with a browser navigation per step, so a slow page can
legitimately take tens of seconds and risks a gateway timeout.

**Deploying a change:** push to `main` → Coolify builds and deploys → visible in
the dashboard with history and rollback.
