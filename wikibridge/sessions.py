"""Sessioni utente e limitazione dei tentativi di login.

Dopo un login riuscito il server rilascia un token opaco casuale; lo
memorizza solo come hash, così un dump della memoria o dei log non espone
token utilizzabili. Il token è revocabile (logout, invalidazione) — a
differenza di un JWT firmato — e scade per inattività e in assoluto.

Sessioni in memoria del processo: un riavvio del server richiede un nuovo
login. Con più processi/istanze servirebbe uno store condiviso.
"""
from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Callable

from .identity import Identity


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class _Session:
    identity: Identity
    created: float
    last_seen: float


class SessionStore:
    def __init__(self, idle_s: int, max_s: int,
                 clock: Callable[[], float] = time.monotonic):
        self.idle_s = idle_s
        self.max_s = max_s
        self._clock = clock
        self._lock = threading.Lock()
        self._sessions: dict[str, _Session] = {}

    def create(self, identity: Identity) -> str:
        token = secrets.token_urlsafe(32)
        now = self._clock()
        with self._lock:
            self._purge(now)
            self._sessions[_digest(token)] = _Session(identity, now, now)
        return token

    def get(self, token: str) -> Identity | None:
        now = self._clock()
        key = _digest(token)
        with self._lock:
            s = self._sessions.get(key)
            if s is None:
                return None
            if now - s.last_seen > self.idle_s or now - s.created > self.max_s:
                del self._sessions[key]
                return None
            s.last_seen = now
            return s.identity

    def revoke(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(_digest(token), None)

    def revoke_subject(self, subject: str) -> int:
        with self._lock:
            keys = [k for k, s in self._sessions.items()
                    if s.identity.subject == subject]
            for k in keys:
                del self._sessions[k]
        return len(keys)

    def _purge(self, now: float) -> None:
        expired = [k for k, s in self._sessions.items()
                   if now - s.last_seen > self.idle_s or now - s.created > self.max_s]
        for k in expired:
            del self._sessions[k]


class LoginThrottle:
    """Finestra scorrevole di tentativi falliti per username e per IP.

    La soglia per username va tenuta SOTTO quella di blocco account di AD:
    così un attaccante non riesce a far bloccare account reali passando da
    wikibridge. Resta possibile bloccare temporaneamente il login a
    wikibridge di un utente (per la durata della finestra)."""

    def __init__(self, max_user: int, max_ip: int, window_s: int,
                 clock: Callable[[], float] = time.monotonic):
        self.max_user = max_user
        self.max_ip = max_ip
        self.window_s = window_s
        self._clock = clock
        self._lock = threading.Lock()
        self._fail_user: dict[str, deque] = defaultdict(deque)
        self._fail_ip: dict[str, deque] = defaultdict(deque)

    def _trim(self, q: deque, now: float) -> None:
        while q and now - q[0] > self.window_s:
            q.popleft()

    def retry_after(self, username: str, ip: str) -> int:
        """Secondi da attendere, 0 se il tentativo è consentito."""
        now = self._clock()
        with self._lock:
            waits = []
            for q, limit in ((self._fail_user[username.lower()], self.max_user),
                             (self._fail_ip[ip], self.max_ip)):
                self._trim(q, now)
                if len(q) >= limit:
                    waits.append(int(self.window_s - (now - q[0])) + 1)
            return max(waits, default=0)

    def failure(self, username: str, ip: str) -> None:
        now = self._clock()
        with self._lock:
            self._fail_user[username.lower()].append(now)
            self._fail_ip[ip].append(now)

    def success(self, username: str) -> None:
        with self._lock:
            self._fail_user.pop(username.lower(), None)
