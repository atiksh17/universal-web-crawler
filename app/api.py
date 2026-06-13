from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .config import get_settings
from .escalator import Escalator
from .jobs import JobManager
from .store import Store
from .throttle import Throttler


class ScrapeRequest(BaseModel):
    url: str
    include_html: bool = True


class BulkRequest(BaseModel):
    urls: list[str] = Field(..., min_length=1, max_length=100_000)


@asynccontextmanager
async def lifespan(app: FastAPI):
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
    try:
        yield
    finally:
        await jobs.stop()
        await escalator.aclose()
        await store.close()


app = FastAPI(title="Universal Crawler", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"ok": True, "tiers": app.state.escalator.tier_status(),
            "order": app.state.escalator.order}


@app.post("/scrape")
async def scrape(req: ScrapeRequest):
    """Single URL, synchronous. Returns the full HTML and which tier produced it."""
    outcome = await app.state.jobs.crawl_one(req.url)
    body = outcome.summary()
    if req.include_html:
        body["html"] = outcome.html
    return body


@app.post("/scrape/bulk", status_code=202)
async def scrape_bulk(req: BulkRequest):
    """Bulk: dump URLs, get a job_id. The system owns batching/throttling/escalation."""
    job_id = await app.state.jobs.submit_bulk(req.urls)
    return {"job_id": job_id, "total": len(req.urls)}


@app.get("/jobs/{job_id}")
async def job_status(job_id: str):
    job = await app.state.store.get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    job["results"] = await app.state.store.get_results(job_id, include_html=False)
    return job


@app.get("/jobs/{job_id}/results")
async def job_results(job_id: str):
    job = await app.state.store.get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return {"job_id": job_id, "results": await app.state.store.get_results(job_id, include_html=True)}
