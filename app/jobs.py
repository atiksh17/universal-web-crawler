from __future__ import annotations

import asyncio
import time
import uuid

from .escalator import Escalator
from .models import JobState
from .store import Store
from .throttle import Throttler


class JobManager:
    """In-memory async queue + worker pool (dev default).

    Swap to Redis+arq for prod by setting CRAWLER_QUEUE_BACKEND=redis and enqueuing
    (job_id, url) tuples to arq instead of the local asyncio.Queue — the worker body
    (`_process`) stays identical.
    """

    def __init__(self, escalator: Escalator, store: Store, throttler: Throttler,
                 worker_count: int = 4):
        self.escalator = escalator
        self.store = store
        self.throttler = throttler
        self.worker_count = worker_count
        self._queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []

    def start(self) -> None:
        for _ in range(self.worker_count):
            self._workers.append(asyncio.create_task(self._worker_loop()))

    async def stop(self) -> None:
        for w in self._workers:
            w.cancel()
        for w in self._workers:
            try:
                await w
            except asyncio.CancelledError:
                pass

    async def submit_bulk(self, urls: list[str]) -> str:
        job_id = uuid.uuid4().hex
        await self.store.create_job(job_id, total=len(urls), created=time.time())
        await self.store.set_state(job_id, JobState.running)
        for u in urls:
            await self._queue.put((job_id, u))
        return job_id

    async def _worker_loop(self) -> None:
        while True:
            job_id, url = await self._queue.get()
            try:
                await self._process(job_id, url)
            except Exception:
                pass
            finally:
                self._queue.task_done()
                await self._maybe_finish(job_id)

    async def _process(self, job_id: str, url: str) -> None:
        async with self.throttler.slot(url):
            outcome = await self.escalator.crawl(url)
        await self.store.save_result(job_id, outcome)

    async def _maybe_finish(self, job_id: str) -> None:
        job = await self.store.get_job(job_id)
        if job and job["done"] >= job["total"] and job["state"] != JobState.done.value:
            await self.store.set_state(job_id, JobState.done)

    async def crawl_one(self, url: str):
        """Synchronous single-URL path (still throttled)."""
        async with self.throttler.slot(url):
            return await self.escalator.crawl(url)
