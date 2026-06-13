from __future__ import annotations

import time


class ProxyPool:
    """Pool of residential exits (rented ISP IPs + later Tailscale-laptop proxies).

    Round-robins across currently-available IPs and applies a cooldown to banned ones,
    keeping each IP's per-domain footprint low to protect reputation.
    """

    def __init__(self, proxies: list[str]):
        self._proxies = list(proxies)
        self._idx = 0
        self._cooldown: dict[str, float] = {}

    def available(self) -> list[str]:
        now = time.monotonic()
        return [p for p in self._proxies if self._cooldown.get(p, 0.0) <= now]

    def pick(self) -> str | None:
        avail = self.available()
        if not avail:
            return None
        p = avail[self._idx % len(avail)]
        self._idx += 1
        return p

    def mark_ban(self, proxy: str, cooldown_s: float = 300.0) -> None:
        self._cooldown[proxy] = time.monotonic() + cooldown_s

    def __bool__(self) -> bool:
        return bool(self._proxies)

    def __len__(self) -> int:
        return len(self._proxies)
