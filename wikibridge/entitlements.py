"""Cache in-memory (per processo) dei ruoli risolti per utente.

Risolve il problema posto per il filtro ACL: interrogare LDAP/SAML/OIDC
per ogni blocco ad ogni ricerca sarebbe troppo lento. Qui i ruoli di un
utente si risolvono una volta sola (al cache-miss) e si riusano per tutte
le query successive finché non scadono (TTL configurabile,
entitlement_cache_ttl_s) — così ogni ricerca fa zero round-trip verso il
sistema di autorizzazione centrale.

Il rovescio della medaglia è la staleness: se un utente riceve un nuovo
permesso dopo la risoluzione, la cache non lo sa finché non scade. Per
questo esiste invalidate() (esposto da server.py come
POST /identity/refresh): un refresh self-service immediato senza
aspettare il TTL.

Cache in-memory perché il server è single-process locale (nessun
gunicorn/worker multipli, nessuna istanza multipla). Se in futuro
wikibridge girasse con più processi/istanze, questa cache andrebbe
sostituita con uno store condiviso (es. Redis) — non necessario ora.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from .identity import Identity, IdentityProvider


@dataclass
class _Entry:
    roles: frozenset[str]
    resolved_at: float


class EntitlementCache:
    def __init__(self, provider: IdentityProvider, ttl_s: int = 300):
        self._provider = provider
        self._ttl = ttl_s
        self._lock = threading.Lock()
        self._cache: dict[str, _Entry] = {}

    def roles_for(self, identity: Identity) -> frozenset[str]:
        """Ruoli dalla cache se ancora validi, altrimenti risolti ora dal
        provider (bind LDAP/lookup gruppi) e messi in cache."""
        with self._lock:
            entry = self._cache.get(identity.subject)
            if entry is not None and time.monotonic() - entry.resolved_at < self._ttl:
                return entry.roles
        roles = frozenset(self._provider.resolve_roles(identity))
        with self._lock:
            self._cache[identity.subject] = _Entry(roles, time.monotonic())
        return roles

    def invalidate(self, subject: str) -> None:
        """Bust manuale (refresh self-service) per un utente: la prossima
        roles_for() ri-risolve subito dal provider."""
        with self._lock:
            self._cache.pop(subject, None)
