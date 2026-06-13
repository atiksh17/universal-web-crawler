from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


@dataclass
class FetchResult:
    """One tier's attempt at one URL."""

    url: str
    tier: str
    ok: bool = False
    status: int | None = None
    html: str = ""
    reason: str = ""          # failure reason when not ok
    elapsed_ms: int = 0
    content_length: int = 0
    text_len: int = 0
    final_url: str | None = None

    def summary(self) -> dict:
        return {
            "url": self.url,
            "tier": self.tier,
            "ok": self.ok,
            "status": self.status,
            "reason": self.reason,
            "elapsed_ms": self.elapsed_ms,
            "content_length": self.content_length,
            "text_len": self.text_len,
            "final_url": self.final_url,
        }


@dataclass
class Outcome:
    """Final result for one URL after the escalation walk."""

    url: str
    ok: bool = False
    tier: str = ""
    status: int | None = None
    reason: str = ""
    elapsed_ms: int = 0
    html: str = ""
    attempts: list[dict] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "url": self.url,
            "ok": self.ok,
            "tier": self.tier,
            "status": self.status,
            "reason": self.reason,
            "elapsed_ms": self.elapsed_ms,
            "content_length": len(self.html),
            "attempts": self.attempts,
        }


class JobState(str, Enum):
    pending = "pending"
    running = "running"
    done = "done"
