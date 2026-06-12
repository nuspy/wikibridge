"""Limiti di import e rate limiting.

ImportLimits raccoglie tutti i tetti di un import (profondità, pagine,
dimensioni, velocità); ogni import può sovrascrivere i default del Config.
RateLimiter è un token bucket per host condiviso dalla sessione HTTP.
"""
from __future__ import annotations

import fnmatch
import threading
import time
from dataclasses import dataclass, field

from .config import Config


@dataclass
class ImportLimits:
    max_depth: int = 2
    max_pages: int = 200
    max_page_mb: float = 2.0
    max_total_mb: float = 200.0
    max_attachments_mb: float = 0.0
    rate_limit_rps: float = 1.0
    request_timeout_s: int = 30
    respect_robots: bool = True
    max_runtime_min: int = 30
    include: list[str] = field(default_factory=list)   # glob su titolo/path
    exclude: list[str] = field(default_factory=list)

    @classmethod
    def from_config(cls, cfg: Config, **overrides) -> "ImportLimits":
        lim = cls(
            max_depth=cfg.max_depth,
            max_pages=cfg.max_pages,
            max_page_mb=cfg.max_page_mb,
            max_total_mb=cfg.max_total_mb,
            max_attachments_mb=cfg.max_attachments_mb,
            rate_limit_rps=cfg.rate_limit_rps,
            request_timeout_s=cfg.request_timeout_s,
            respect_robots=cfg.respect_robots,
            max_runtime_min=cfg.max_runtime_min,
        )
        for k, v in overrides.items():
            if v is None:
                continue
            if not hasattr(lim, k):
                raise ValueError(f"limite sconosciuto: {k}")
            setattr(lim, k, v)
        lim.max_depth = min(lim.max_depth, cfg.hard_max_depth)
        return lim

    def title_allowed(self, title_or_path: str) -> bool:
        t = title_or_path or ""
        if self.include and not any(fnmatch.fnmatch(t, g) for g in self.include):
            return False
        return not any(fnmatch.fnmatch(t, g) for g in self.exclude)

    def to_dict(self) -> dict:
        return {
            "max_depth": self.max_depth, "max_pages": self.max_pages,
            "max_page_mb": self.max_page_mb, "max_total_mb": self.max_total_mb,
            "max_attachments_mb": self.max_attachments_mb,
            "rate_limit_rps": self.rate_limit_rps,
            "request_timeout_s": self.request_timeout_s,
            "respect_robots": self.respect_robots,
            "max_runtime_min": self.max_runtime_min,
            "include": self.include, "exclude": self.exclude,
        }


class RateLimiter:
    """Token bucket per host: blocca finché non è disponibile un token."""

    def __init__(self, rps: float):
        self.rps = max(rps, 0.05)
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, host: str) -> None:
        interval = 1.0 / self.rps
        while True:
            with self._lock:
                now = time.monotonic()
                last = self._last.get(host, 0.0)
                if now - last >= interval:
                    self._last[host] = now
                    return
                sleep_for = interval - (now - last)
            time.sleep(min(sleep_for, 1.0))
