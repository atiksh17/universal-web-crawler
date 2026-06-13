from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import FetchResult


class Fetcher(ABC):
    name: str = "base"
    tier: str = "BASE"
    enabled: bool = True          # False => tier is configured but not usable (missing dep/creds)
    disabled_reason: str = ""

    @abstractmethod
    async def fetch(self, url: str, *, proxy: str | None = None) -> FetchResult:
        ...

    async def aclose(self) -> None:
        return None
