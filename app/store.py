from __future__ import annotations

import json

import aiosqlite

from .models import JobState, Outcome


class Store:
    """SQLite job/result store (dev default). Swap to Postgres for prod by implementing
    the same async interface against asyncpg and setting CRAWLER_STORE_BACKEND=postgres.
    HTML blobs are stored compressed-as-text here; move to object storage at scale.
    """

    def __init__(self, db_url: str = "crawler.db"):
        self.db_url = db_url
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        self._db = await aiosqlite.connect(self.db_url)
        await self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                total INTEGER NOT NULL,
                done INTEGER NOT NULL DEFAULT 0,
                ok_count INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS results (
                job_id TEXT NOT NULL,
                url TEXT NOT NULL,
                ok INTEGER NOT NULL,
                tier TEXT, status INTEGER, reason TEXT,
                elapsed_ms INTEGER, html TEXT, attempts TEXT,
                PRIMARY KEY (job_id, url)
            );
            """
        )
        await self._db.commit()

    async def create_job(self, job_id: str, total: int, created: float) -> None:
        await self._db.execute(
            "INSERT INTO jobs (id, state, total, done, ok_count, created) VALUES (?,?,?,?,?,?)",
            (job_id, JobState.pending.value, total, 0, 0, created),
        )
        await self._db.commit()

    async def set_state(self, job_id: str, state: JobState) -> None:
        await self._db.execute("UPDATE jobs SET state=? WHERE id=?", (state.value, job_id))
        await self._db.commit()

    async def save_result(self, job_id: str, o: Outcome) -> None:
        await self._db.execute(
            "INSERT OR REPLACE INTO results "
            "(job_id, url, ok, tier, status, reason, elapsed_ms, html, attempts) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (job_id, o.url, int(o.ok), o.tier, o.status, o.reason, o.elapsed_ms,
             o.html, json.dumps(o.attempts)),
        )
        await self._db.execute(
            "UPDATE jobs SET done = done + 1, ok_count = ok_count + ? WHERE id=?",
            (int(o.ok), job_id),
        )
        await self._db.commit()

    async def get_job(self, job_id: str) -> dict | None:
        cur = await self._db.execute(
            "SELECT id, state, total, done, ok_count, created FROM jobs WHERE id=?", (job_id,)
        )
        row = await cur.fetchone()
        if not row:
            return None
        return {"job_id": row[0], "state": row[1], "total": row[2],
                "done": row[3], "ok_count": row[4], "created": row[5]}

    async def get_results(self, job_id: str, include_html: bool = False) -> list[dict]:
        cur = await self._db.execute(
            "SELECT url, ok, tier, status, reason, elapsed_ms, html FROM results WHERE job_id=?",
            (job_id,),
        )
        rows = await cur.fetchall()
        out = []
        for r in rows:
            item = {"url": r[0], "ok": bool(r[1]), "tier": r[2], "status": r[3],
                    "reason": r[4], "elapsed_ms": r[5]}
            if include_html:
                item["html"] = r[6]
            else:
                item["content_length"] = len(r[6] or "")
            out.append(item)
        return out

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
