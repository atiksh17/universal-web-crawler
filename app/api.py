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

Auth: calls arriving through the public route (crawl.lrc-limited.com) need
`X-API-Key: $CRAWLER_API_KEY`; /health is exempt and in-network callers are not
affected at all. See the auth block below for why it is split that way.
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import secrets
import shutil
import tempfile
import time
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
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
    # How anchors render in the markdown: true/"inline" = `[text](href)`, false/"text" = the label
    # alone, "strip" = drop the anchor entirely (no href, no label — link-only blocks vanish).
    # Unset = auto: "text" when `endpoints` is on (the link tree already carries the URLs),
    # "inline" otherwise. "strip" is never automatic — it can remove words a sentence needs.
    mdLinks: bool | Literal["inline", "text", "strip"] | None = None


class ScrapeRequest(ShapeFlags):
    url: str


class BulkRequest(ShapeFlags):
    # The hard ceiling here is a backstop against a pathological request body; the
    # ceiling that actually applies is CRAWLER_MAX_URLS_PER_JOB, checked in the
    # handler so it stays tunable without a code change. See the per-job bounds
    # note in config.py.
    urls: list[str] = Field(..., min_length=1, max_length=100_000)


def _opts(f: ShapeFlags) -> ShapeOptions:
    if f.selector and not f.html:
        raise HTTPException(400, "`selector` requires `html: true`")
    return ShapeOptions(endpoints=f.endpoints, meta=f.meta, footerHtml=f.footerHtml,
                        html=f.html, selector=f.selector, md_links=f.mdLinks)


# ----------------------------- lifecycle -----------------------------
async def _sweep_loop(app: FastAPI) -> None:
    """Retention sweep: expire old jobs and the temp Chrome profiles they left behind.

    Both leak without it. The store grew to 3.4 GB of HTML across 13 jobs, and
    nodriver's per-fetch profile dirs had piled up 3,146 deep (1.5 GB) because
    Chromium keeps writing into the directory after the fetch path rmtree's it.
    """
    s = app.state.s
    while True:
        await asyncio.sleep(s.sweep_interval_s)
        try:
            dropped = await app.state.store.purge_jobs_older_than(
                s.job_retention_hours * 3600)
            for job_id in dropped:
                app.state.job_opts.pop(job_id, None)
        except Exception:
            pass
        try:
            _sweep_temp_profiles(s.sweep_interval_s)
        except Exception:
            pass


def _sweep_temp_profiles(min_age_s: float) -> None:
    """Remove `cr-prof-*` dirs no longer in use. Age-gated so a profile belonging to
    an in-flight fetch is never pulled out from under a live browser."""
    cutoff = time.time() - max(min_age_s, 300)
    for p in glob.glob(os.path.join(tempfile.gettempdir(), "cr-prof-*")):
        try:
            if os.path.getmtime(p) < cutoff:
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Build the runtime from .env, start the worker pool, tear it all down on exit."""
    s = get_settings()
    store = Store(s.db_url)
    await store.init()
    escalator = Escalator(s)
    throttler = Throttler(s.global_concurrency, s.per_domain_concurrency,
                          s.per_domain_min_interval_ms)
    jobs = JobManager(escalator, store, throttler, worker_count=s.worker_count,
                      max_page_html_bytes=s.max_page_html_bytes,
                      max_job_html_bytes=s.max_job_html_bytes)
    jobs.start()
    app.state.s = s
    app.state.store = store
    app.state.escalator = escalator
    app.state.jobs = jobs
    # Shaping flags per bulk job, applied at /jobs/{id}/results. In-memory: a restart
    # drops them and /results falls back to markdown-only (re-submit to re-shape).
    # Evicted alongside the job itself by the sweep, or it outlives every job it
    # describes and becomes a slow leak of its own.
    app.state.job_opts = {}
    _sweep_temp_profiles(0)  # clear whatever the last run leaked before serving
    sweeper = asyncio.create_task(_sweep_loop(app))
    try:
        yield
    finally:
        sweeper.cancel()
        try:
            await sweeper
        except asyncio.CancelledError:
            pass
        await jobs.stop()
        await escalator.aclose()
        await store.close()


app = FastAPI(title="Universal Crawler", version="1.0.0", lifespan=lifespan)


# ----------------------------- auth -----------------------------
# The crawler has no per-user auth and never should: it is a worker, not a product.
# docker-compose.coolify.yaml publishes no host port for exactly that reason, so the
# only way in from outside this box is the Traefik route on crawl.lrc-limited.com —
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
    cap = app.state.s.max_urls_per_job
    if cap and len(req.urls) > cap:
        raise HTTPException(
            413,
            f"{len(req.urls)} URLs exceeds the {cap}-URL per-job limit; split the "
            f"batch across jobs (raise CRAWLER_MAX_URLS_PER_JOB to change this)",
        )
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
    """Progress + lightweight per-URL summary (poll this).

    Deliberately free of HTML. Callers poll this once a second for the life of a job,
    so its cost has to stay flat in the size of what has been crawled — see the note
    on Store.get_results for what happens when it doesn't.
    """
    job = await app.state.store.get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    job["results"] = await app.state.store.get_results(job_id, include_html=False)
    return job


def _shape_row(r: dict, opts: ShapeOptions) -> dict:
    item = {**shape_response(r["url"], r.get("html", ""), r["ok"],
                             r.get("reason", ""), opts),
            "url": r["url"], "tier": r["tier"]}
    # Say so when the stored body is not the whole page, rather than letting a
    # budget-trimmed result read as a genuinely short one.
    if r["html_state"] != "full":
        item["html_state"] = r["html_state"]
        item["content_length"] = r["content_length"]
    r["html"] = ""  # release the source HTML as soon as it has been shaped
    return item


async def _stream_results(job_id: str, total: int, opts: ShapeOptions, chunk: int):
    """Emit the full result set as JSON without ever holding all of it.

    Callers (the n8n Web Crawler workflow, the enrichment-engine) ask for a job's
    results in one GET and expect one array back, so paginating by default would
    silently truncate them. Streaming keeps that contract exactly while bounding
    server memory to one `chunk` of rows: shape a slice, write it, drop it.
    """
    head = json.dumps({"job_id": job_id, "total": total})[:-1]  # trim the closing brace
    yield f'{head},"results":['
    first = True
    offset = 0
    while offset < total:
        rows = await app.state.store.get_results(
            job_id, include_html=True, limit=chunk, offset=offset)
        if not rows:
            break
        for r in rows:
            yield ("" if first else ",") + json.dumps(_shape_row(r, opts))
            first = False
        offset += len(rows)
    yield "]}"


@app.get("/jobs/{job_id}/results")
async def job_results(
    job_id: str,
    limit: int | None = Query(None, ge=1),
    offset: int = Query(0, ge=0),
):
    """Final shaped results — shaped with the flags sent at submit time.

    Default: the whole set, streamed (same `{job_id, results:[...]}` shape callers
    already parse). Pass `limit` to get one explicit page instead, plus a
    `next_offset` to walk with — useful when the caller wants to bound its own
    memory too.

    Either way the server never materialises the whole corpus: shaping holds a
    page's HTML *and* its markdown at once, so doing a few thousand pages in one
    go was several GB of peak RSS.
    """
    s = app.state.s
    job = await app.state.store.get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    opts = app.state.job_opts.get(job_id, ShapeOptions())
    total = await app.state.store.count_results(job_id)

    if limit is not None:
        limit = min(limit, s.max_results_page_size)
        raw = await app.state.store.get_results(job_id, include_html=True,
                                                limit=limit, offset=offset)
        shaped = [_shape_row(r, opts) for r in raw]
        nxt = offset + len(shaped)
        return {"job_id": job_id, "results": shaped, "total": total,
                "offset": offset, "limit": limit,
                "next_offset": nxt if nxt < total else None}

    return StreamingResponse(
        _stream_results(job_id, total, opts, s.stream_chunk_rows),
        media_type="application/json",
    )


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
