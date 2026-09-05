from __future__ import annotations

import json
import time

import aiosqlite

from .models import JobState, Outcome


class Store:
    """SQLite job/result store (dev default). Swap to Postgres for prod by implementing
    the same async interface against asyncpg and setting CRAWLER_STORE_BACKEND=postgres.
    HTML blobs are stored compressed-as-text here; move to object storage at scale.

    Every read here is bounded on purpose — see the note on `get_results`. A job's HTML
    corpus routinely runs to hundreds of MB, so any query that pulls `html` for a whole
    job is a memory event, not a query.
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
                created REAL NOT NULL,
                html_bytes INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS results (
                job_id TEXT NOT NULL,
                url TEXT NOT NULL,
                ok INTEGER NOT NULL,
                tier TEXT, status INTEGER, reason TEXT,
                elapsed_ms INTEGER, html TEXT, attempts TEXT,
                content_length INTEGER NOT NULL DEFAULT 0,
                html_state TEXT NOT NULL DEFAULT 'full',
                PRIMARY KEY (job_id, url)
            );
            -- get_results/purge both filter on job_id; without this they table-scan
            -- a multi-GB results table on every poll.
            CREATE INDEX IF NOT EXISTS idx_results_job ON results(job_id);
            """
        )
        # Columns added after the first release. CREATE TABLE IF NOT EXISTS won't add
        # them to a database that predates them, so nudge each one in separately and
        # let "duplicate column" mean "already migrated".
        for ddl in (
            "ALTER TABLE jobs ADD COLUMN html_bytes INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE results ADD COLUMN content_length INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE results ADD COLUMN html_state TEXT NOT NULL DEFAULT 'full'",
        ):
            try:
                await self._db.execute(ddl)
            except Exception:
                pass
        await self._db.commit()

    async def create_job(self, job_id: str, total: int, created: float) -> None:
        await self._db.execute(
            "INSERT INTO jobs (id, state, total, done, ok_count, created, html_bytes) "
            "VALUES (?,?,?,?,?,?,0)",
            (job_id, JobState.pending.value, total, 0, 0, created),
        )
        await self._db.commit()

    async def set_state(self, job_id: str, state: JobState) -> None:
        await self._db.execute("UPDATE jobs SET state=? WHERE id=?", (state.value, job_id))
        await self._db.commit()

    async def save_result(self, job_id: str, o: Outcome, *,
                          max_page_bytes: int = 0, max_job_bytes: int = 0) -> None:
        """Persist one URL's outcome, enforcing the per-page and per-job HTML budgets.

        The budgets bound what a single job can cost in storage AND — because /results
        has to shape whatever is stored — in memory later. `content_length` always
        records the page's TRUE size, so truncating never lies to the caller about how
        much was there; `html_state` says what actually made it to disk.
        """
        html = o.html or ""
        content_length = len(html)
        html_state = "full"

        if max_page_bytes and content_length > max_page_bytes:
            html = html[:max_page_bytes]
            html_state = "truncated"

        if max_job_bytes:
            cur = await self._db.execute("SELECT html_bytes FROM jobs WHERE id=?", (job_id,))
            row = await cur.fetchone()
            spent = row[0] if row else 0
            if spent + len(html) > max_job_bytes:
                # Budget blown: keep the row (the caller still learns the URL succeeded
                # and how big it was) but drop the body. Dropping beats truncating here —
                # a half page of HTML shapes into misleading markdown.
                html = ""
                html_state = "dropped"

        await self._db.execute(
            "INSERT OR REPLACE INTO results "
            "(job_id, url, ok, tier, status, reason, elapsed_ms, html, attempts, "
            " content_length, html_state) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, o.url, int(o.ok), o.tier, o.status, o.reason, o.elapsed_ms,
             html, json.dumps(o.attempts), content_length, html_state),
        )
        await self._db.execute(
            "UPDATE jobs SET done = done + 1, ok_count = ok_count + ?, "
            "html_bytes = html_bytes + ? WHERE id=?",
            (int(o.ok), len(html), job_id),
        )
        await self._db.commit()

    async def get_job(self, job_id: str) -> dict | None:
        cur = await self._db.execute(
            "SELECT id, state, total, done, ok_count, created, html_bytes "
            "FROM jobs WHERE id=?", (job_id,)
        )
        row = await cur.fetchone()
        if not row:
            return None
        return {"job_id": row[0], "state": row[1], "total": row[2],
                "done": row[3], "ok_count": row[4], "created": row[5],
                "html_bytes": row[6]}

    async def get_results(self, job_id: str, include_html: bool = False,
                          limit: int | None = None, offset: int = 0) -> list[dict]:
        """Rows for a job. `include_html=False` never touches the blob.

        This endpoint is polled once a second while a job runs. Selecting `html` here
        and then keeping only its length — which is what this did until Sep 2026 —
        materialised the job's entire HTML corpus (231 MB on the job that OOM-killed
        the container) on EVERY poll. `content_length` is a stored column now, so the
        progress path reads bytes, not megabytes.
        """
        cols = ("url, ok, tier, status, reason, elapsed_ms, content_length, html_state"
                + (", html" if include_html else ""))
        sql = f"SELECT {cols} FROM results WHERE job_id=? ORDER BY rowid"
        params: list = [job_id]
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params += [limit, offset]
        cur = await self._db.execute(sql, params)
        rows = await cur.fetchall()
        out = []
        for r in rows:
            item = {"url": r[0], "ok": bool(r[1]), "tier": r[2], "status": r[3],
                    "reason": r[4], "elapsed_ms": r[5], "content_length": r[6],
                    "html_state": r[7]}
            if include_html:
                item["html"] = r[8]
            out.append(item)
        return out

    async def count_results(self, job_id: str) -> int:
        cur = await self._db.execute(
            "SELECT count(*) FROM results WHERE job_id=?", (job_id,))
        return (await cur.fetchone())[0]

    async def purge_jobs_older_than(self, max_age_s: float) -> list[str]:
        """Drop finished jobs past the retention window. Returns the ids removed.

        Without this the store only grows: 13 jobs had accumulated 3.4 GB of HTML in
        the container's writable layer before this existed.
        """
        if max_age_s <= 0:
            return []
        cutoff = time.time() - max_age_s
        cur = await self._db.execute("SELECT id FROM jobs WHERE created < ?", (cutoff,))
        ids = [r[0] for r in await cur.fetchall()]
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        await self._db.execute(f"DELETE FROM results WHERE job_id IN ({marks})", ids)
        await self._db.execute(f"DELETE FROM jobs WHERE id IN ({marks})", ids)
        await self._db.commit()
        return ids

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
