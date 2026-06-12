"""Sessione HTTP condivisa: retry, timeout, user-agent, rate limit.

Tutte le richieste degli adapter passano da qui; il RateLimiter per host
è agganciato come hook, quindi nessun adapter può superare il tetto.
"""
from __future__ import annotations

from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import Config
from .limits import ImportLimits, RateLimiter


class ThrottledSession(requests.Session):
    def __init__(self, limiter: RateLimiter, timeout_s: int, user_agent: str):
        super().__init__()
        self._limiter = limiter
        self._timeout_s = timeout_s
        self.headers["User-Agent"] = user_agent
        retry = Retry(total=3, backoff_factor=1.5,
                      status_forcelist=(429, 500, 502, 503, 504),
                      respect_retry_after_header=True)
        adapter = HTTPAdapter(max_retries=retry)
        self.mount("https://", adapter)
        self.mount("http://", adapter)

    def request(self, method, url, **kwargs):  # noqa: D102
        host = urlparse(url).netloc
        if host:
            self._limiter.wait(host)
        kwargs.setdefault("timeout", self._timeout_s)
        return super().request(method, url, **kwargs)


def make_session(cfg: Config, limits: ImportLimits) -> ThrottledSession:
    return ThrottledSession(RateLimiter(limits.rate_limit_rps),
                            limits.request_timeout_s, cfg.user_agent)
