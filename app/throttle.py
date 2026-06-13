from __future__ import annotations

import asyncio
import contextlib
import time
from urllib.parse import urlsplit


class Throttler:
    """Two-level politeness: a global concurrency cap plus per-domain concurrency and
    minimum spacing. This is the "client never worries about rate limiting" promise —
    the system spaces requests so it never hammers one site into banning us.
    """

    def __init__(self, global_concurrency: int, per_domain_concurrency: int,
                 per_domain_min_interval_ms: int):
        self._global = asyncio.Semaphore(global_concurrency)
        self._pdc = per_domain_concurrency
        self._interval = per_domain_min_interval_ms / 1000.0
        self._domain_sems: dict[str, asyncio.Semaphore] = {}
        self._domain_last: dict[str, float] = {}
        self._domain_locks: dict[str, asyncio.Lock] = {}
        self._reg_lock = asyncio.Lock()

    @staticmethod
    def _domain(url: str) -> str:
        return urlsplit(url).netloc.lower()

    async def _domain_objs(self, domain: str) -> tuple[asyncio.Semaphore, asyncio.Lock]:
        async with self._reg_lock:
            if domain not in self._domain_sems:
                self._domain_sems[domain] = asyncio.Semaphore(self._pdc)
                self._domain_locks[domain] = asyncio.Lock()
            return self._domain_sems[domain], self._domain_locks[domain]

    @contextlib.asynccontextmanager
    async def slot(self, url: str):
        domain = self._domain(url)
        sem, lock = await self._domain_objs(domain)
        await self._global.acquire()
        await sem.acquire()
        try:
            # enforce min spacing per domain
            async with lock:
                last = self._domain_last.get(domain, 0.0)
                wait = self._interval - (time.monotonic() - last)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._domain_last[domain] = time.monotonic()
            yield
        finally:
            sem.release()
            self._global.release()
