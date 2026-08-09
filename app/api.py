"""HTTP API — the standalone server for the crawler.

Self-contained: `uvicorn app.api:app` is all a fresh clone needs. Everything the API
touches (escalator, fetchers, classifier, jobs, store, shaping) lives in this package.

Routes:
  GET  /health                  tier status + escalation order
  POST /crawl                   single URL, synchronous, shaped payload
  POST /crawl/bulk              many URLs, async job -> job_id
  GET  /jobs/{id}               progress (state/total/done/ok_count + per-URL summary)
  GET  /jobs/{id}/results       final shaped results

Output shaping mirrors `app/shape.py`: markdown is always returned; endpoints/meta/
footerHtml/html are opt-in per request. See usage.md for the client guide.

Auth: calls arriving through the public route (crawl.goautofusion.com) need
`X-API-Key: $CRAWLER_API_KEY`; /health is exempt and in-network callers are not
affected at all. See the auth block below for why it is split that way.
"""
from __future__ import annotations

import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .config import get_settings
from .escalator import Escalator
from .jobs import JobManager
from .shape import ShapeOptions, shape_response
from .store import Store
from .throttle import Throttler


# ----------------------------- request models -----------------------------
class ShapeFlags(BaseModel):
    """Output-shaping flags. Always returned: domain, markdown, quality, bot_blocked.
    Each flag adds an optional field. `selector` requires `html=true`."""

    endpoints: bool = False
    meta: bool = False
    footerHtml: bool = False
    html: bool = False
    selector: str | None = None
    # Inline `[text](href)` in the markdown. Unset = auto: OFF when `endpoints` is on (the link
    # tree already carries the URLs), ON otherwise. Set explicitly to override either way.
    mdLinks: bool | None = None


class ScrapeRequest(ShapeFlags):
    url: str


class BulkRequest(ShapeFlags):
    urls: list[str] = Field(..., min_length=1, max_length=100_000)


def _opts(f: ShapeFlags) -> ShapeOptions:
    if f.selector and not f.html:
        raise HTTPException(400, "`selector` requires `html: true`")
    return ShapeOptions(endpoints=f.endpoints, meta=f.meta, footerHtml=f.footerHtml,
                        html=f.html, selector=f.selector, md_links=f.mdLinks)


# ----------------------------- lifecycle -----------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Build the runtime from .env, start the worker pool, tear it all down on exit."""
    s = get_settings()
    store = Store(s.db_url)
    await store.init()
    escalator = Escalator(s)
    throttler = Throttler(s.global_concurrency, s.per_domain_concurrency,
                          s.per_domain_min_interval_ms)
    jobs = JobManager(escalator, store, throttler, worker_count=s.worker_count)
    jobs.start()
    app.state.s = s
    app.state.store = store
    app.state.escalator = escalator
    app.state.jobs = jobs
    # Shaping flags per bulk job, applied at /jobs/{id}/results. In-memory: a restart
    # drops them and /results falls back to markdown-only (re-submit to re-shape).
    app.state.job_opts = {}
    try:
        yield
    finally:
        await jobs.stop()
        await escalator.aclose()
        await store.close()


app = FastAPI(title="Universal Crawler", version="1.0.0", lifespan=lifespan)


# ----------------------------- auth -----------------------------
# The crawler has no per-user auth and never should: it is a worker, not a product.
# docker-compose.coolify.yaml publishes no host port for exactly that reason, so the
# only way in from outside this box is the Traefik route on crawl.goautofusion.com —
# and that route is public. Unguarded it is an open crawl proxy: anyone can drive the
# headless-Chrome fleet, and /crawl/bulk takes 100k URLs a call. So: require a key.
#
# Enforced ONLY on requests that came through the proxy. Traefik always sets
# X-Forwarded-For on what it forwards, while an in-network caller reaching us by
# container name (the API's /web room, the enrichment-engine) connects straight to
# the port and so never has it. That distinction is what keeps the internal callers
# working untouched — the enrichment-engine's client is kept verbatim and sends no
# headers at all (see the /web-crawl note below), so it could not send a key even if
# we wanted it to. A public caller cannot forge its way past this by omitting the
# header: Traefik appends X-Forwarded-For itself, after the request is already in.
#
# /health stays open so Docker's healthcheck and any uptime probe keep working. To put
# the docs behind the key too, they already are — add them here to let them out again.
_OPEN_PATHS = frozenset({"/health", "/web-crawl/health"})


@app.middleware("http")
async def require_api_key(request: Request, call_next):
    if "x-forwarded-for" in request.headers and request.url.path not in _OPEN_PATHS:
        expected = get_settings().api_key
        if not expected:
            # Fail closed. An unset key must never silently mean "open to the world".
            return JSONResponse({"detail": "public access is not configured"}, 503)
        if not secrets.compare_digest(request.headers.get("x-api-key", ""), expected):
            return JSONResponse({"detail": "missing or invalid X-API-Key"}, 401)
    return await call_next(request)


# ----------------------------- routes -----------------------------
@app.get("/health")
async def health():
    esc = app.state.escalator
    return {"ok": True, "tiers": esc.tier_status(), "order": esc.order}


@app.post("/crawl")
async def crawl(req: ScrapeRequest):
    """Single URL, synchronous. Returns the shaped payload."""
    opts = _opts(req)
    outcome = await app.state.jobs.crawl_one(req.url)
    out = shape_response(req.url, outcome.html, outcome.ok, outcome.reason, opts)
    out["tier"] = outcome.tier
    return out


@app.post("/crawl/bulk", status_code=202)
async def crawl_bulk(req: BulkRequest):
    """Bulk: dump URLs, get a job_id. Shaping flags are applied at /jobs/{id}/results."""
    opts = _opts(req)
    job_id = await app.state.jobs.submit_bulk(req.urls)
    app.state.job_opts[job_id] = opts
    return {"job_id": job_id, "total": len(req.urls)}


# Back-compat: the routes were named /scrape and /scrape/bulk until 2026-07-27.
# "Scrape" overloaded a term that means something else in our pipeline, so the
# canonical names are now /crawl and /crawl/bulk. These aliases keep any existing
# caller working; they are deprecated and can be dropped once nothing uses them.
app.add_api_route(
    "/scrape", crawl, methods=["POST"], include_in_schema=False, deprecated=True
)
app.add_api_route(
    "/scrape/bulk",
    crawl_bulk,
    methods=["POST"],
    status_code=202,
    include_in_schema=False,
    deprecated=True,
)


# ---------------------------------------------------------------------------
# /web-crawl/* — the path shape the enrichment-engine calls.
#
# That engine (world/projects/enrichment-engine) hardcodes `{base}/web-crawl/...`
# in engine/clients/web_crawl.py, because it was written against the Nubeam
# platform where this crawler sat under a /web-crawl namespace. Its source is
# kept unmodified on purpose, so the namespace is provided HERE instead — five
# additive routes, no behaviour of their own.
#
# Registered after the canonical handlers so /crawl stays the documented name;
# these are hidden from the schema for the same reason as /scrape above.
# ---------------------------------------------------------------------------
app.add_api_route(
    "/web-crawl/scrape", crawl, methods=["POST"], include_in_schema=False
)
app.add_api_route(
    "/web-crawl/scrape/bulk",
    crawl_bulk,
    methods=["POST"],
    status_code=202,
    include_in_schema=False,
)
app.add_api_route(
    "/web-crawl/health", health, methods=["GET"], include_in_schema=False
)


@app.get("/jobs/{job_id}")
async def job_status(job_id: str):
    """Progress + lightweight per-URL summary (poll this)."""
    job = await app.state.store.get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    job["results"] = await app.state.store.get_results(job_id, include_html=False)
    return job


@app.get("/jobs/{job_id}/results")
async def job_results(job_id: str):
    """Final shaped results — shaped with the flags sent at submit time."""
    job = await app.state.store.get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    opts = app.state.job_opts.get(job_id, ShapeOptions())
    raw = await app.state.store.get_results(job_id, include_html=True)
    shaped = [
        {**shape_response(r["url"], r.get("html", ""), r["ok"], r.get("reason", ""), opts),
         "url": r["url"], "tier": r["tier"]}
        for r in raw
    ]
    return {"job_id": job_id, "results": shaped}


# Job routes under the same /web-crawl namespace (see the note above). Declared
# here rather than with the block above because the handlers do not exist yet at
# that point in the module.
app.add_api_route(
    "/web-crawl/jobs/{job_id}", job_status, methods=["GET"], include_in_schema=False
)
app.add_api_route(
    "/web-crawl/jobs/{job_id}/results",
    job_results,
    methods=["GET"],
    include_in_schema=False,
)
