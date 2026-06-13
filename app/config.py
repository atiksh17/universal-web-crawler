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
    min_render_text_len: int = 120   # browser tiers: minimum substantive text

    # L3 — per-IP static residential proxies
    proxies: str = ""  # "http://user:pass@ip:port,http://user:pass@ip2:port"

    # L2C — Byparr service
    byparr_url: str = ""

    # L4 — Bright Data Web Unlocker
    web_unlocker_url: str = "https://api.brightdata.com/request"
    web_unlocker_key: str = ""
    web_unlocker_zone: str = ""

    # Queue / store
    queue_backend: str = "memory"   # memory | redis
    redis_url: str = "redis://localhost:6379"
    store_backend: str = "sqlite"   # sqlite | postgres
    db_url: str = "crawler.db"

    @property
    def tiers(self) -> list[str]:
        return [t.strip().upper() for t in self.enabled_tiers.split(",") if t.strip()]

    @property
    def proxy_list(self) -> list[str]:
        return [p.strip() for p in self.proxies.split(",") if p.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
