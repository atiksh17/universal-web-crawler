from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All knobs come from env (prefix CRAWLER_). VPS deploy = edit .env, nothing else."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="CRAWLER_", extra="ignore")

    # Tiers enabled, in escalation order. L3/L4 activate when their creds are present.
    enabled_tiers: str = "L1,L2"  # comma list of: L1,L2,L2B,L2C,L3,L4

    # Concurrency / throttle
    global_concurrency: int = 8
    per_domain_concurrency: int = 2
    per_domain_min_interval_ms: int = 500
    worker_count: int = 4

    # Browser (L2/L3)
    headless: bool = True
    chrome_path: str = ""   # explicit Chrome binary; empty = nodriver auto-detect
    browser_concurrency: int = 2   # parallel Chrome instances (raise on VPS to match vCPU)
    nav_timeout_s: float = 25.0
    settle_ms: int = 800
    block_resources: bool = True
    impersonate: str = "chrome"  # curl_cffi target

    # Classifier thresholds
    min_text_len: int = 200          # L1: below this (+ scripts) => empty shell
    min_render_text_len: int = 500   # browser tiers: minimum substantive text
    min_anchors: int = 8             # same-origin links that count as a positive "real nav" signal

    # (legacy) per-IP static residential proxies — proxy tier retired; kept for ProxyPool API
    proxies: str = ""  # "http://user:pass@ip:port,http://user:pass@ip2:port"

    # L2C — Byparr service
    byparr_url: str = ""

    # L4 — Bright Data Web Unlocker
    web_unlocker_url: str = "https://api.brightdata.com/request"
    web_unlocker_key: str = ""
    web_unlocker_zone: str = ""

    # Public-door auth. Required on requests that arrive through the Traefik route
    # (crawl.lrc-limited.com); callers on the internal docker network are unaffected.
    # See the auth block in app/api.py. Empty = the public door is shut (503), never open.
    api_key: str = ""

    # Queue / store
    queue_backend: str = "memory"   # memory | redis
    redis_url: str = "redis://localhost:6379"
    store_backend: str = "sqlite"   # sqlite | postgres
    db_url: str = "crawler.db"

    # --- Per-job bounds -----------------------------------------------------
    # This is a shared worker in a container with a hard 4 GiB cap, so a single
    # job must not be able to cost unbounded memory or disk. Sep 2026: one bulk
    # job did exactly that and the kernel OOM-killed uvicorn mid-crawl, taking
    # the n8n executions waiting on it down with it. Each knob below bounds one
    # of the ways a job grows. 0 disables that particular bound.
    max_urls_per_job: int = 5_000          # URLs accepted by one /crawl/bulk call
    max_page_html_bytes: int = 2_000_000   # 2 MB — a single page beyond this is truncated
    max_job_html_bytes: int = 300_000_000  # 300 MB — past this a job stops storing bodies
    results_page_size: int = 500           # default page size for /jobs/{id}/results
    max_results_page_size: int = 2_000     # ceiling a caller may request
    stream_chunk_rows: int = 100           # rows held in memory per chunk when streaming
    job_retention_hours: float = 24.0      # purge jobs (and their rows) older than this
    sweep_interval_s: float = 900.0        # how often the retention/tmp sweep runs

    @property
    def tiers(self) -> list[str]:
        return [t.strip().upper() for t in self.enabled_tiers.split(",") if t.strip()]

    @property
    def proxy_list(self) -> list[str]:
        return [p.strip() for p in self.proxies.split(",") if p.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
