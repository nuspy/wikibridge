"""Traduzione dei permessi letti alla fonte in principal di wikibridge.

Un adapter restituisce le restrizioni di lettura come clausole in AND, con
i nomi della fonte (gruppi Confluence, accountId, username, email). Qui
diventano principal di wikibridge:

- gruppo  -> source_group_map[nome]; con source_group_passthrough il nome
  del gruppo è usato così com'è come ruolo (utile se i gruppi Confluence
  sono i gruppi AD, già mappati con lo stesso nome in identity_role_map);
- utente  -> source_user_map[accountId|username|email] se presente,
  altrimenti email o username cercati nel directory (identity provider)
  per ottenere "user:<id stabile>".

Fail-closed: ciò che non si riesce a tradurre non viene mai "allargato".
Una clausola in cui nessun principal è traducibile diventa [NOBODY]: la
pagina resta visibile solo all'admin finché la mappa non viene completata.
Permessi non leggibili (AclUnreadable) -> [[NOBODY]].
"""
from __future__ import annotations

import logging

from .acl import NOBODY
from .config import Config
from .identity import IdentityError, IdentityProvider

log = logging.getLogger("wikibridge.source_acl")


class SourceAclMapper:
    def __init__(self, cfg: Config, provider: IdentityProvider | None):
        self.group_map = dict(cfg.source_group_map or {})
        self.passthrough = cfg.source_group_passthrough
        self.user_map = dict(cfg.source_user_map or {})
        self.provider = provider
        self.unmapped: set[str] = set()       # per il report
        self._users: dict[str, str | None] = {}

    def _user(self, u: dict) -> str | None:
        for k in ("accountId", "username", "email"):
            if u.get(k) and u[k] in self.user_map:
                return self.user_map[u[k]]
        name = u.get("email") or u.get("username")
        if not name or self.provider is None:
            return None
        if name not in self._users:
            try:
                self._users[name] = self.provider.find_user(name).principal
            except IdentityError:
                self._users[name] = None
        return self._users[name]

    def map(self, native: list[dict] | None) -> list[list[str]] | None:
        """None = la fonte non espone permessi (nessuna clausola dalla fonte)."""
        if native is None:
            return None
        clauses = []
        for clause in native:
            principals = set()
            for g in clause.get("groups", []):
                role = self.group_map.get(g) or (g if self.passthrough else None)
                if role:
                    principals.add(role)
                else:
                    self.unmapped.add(f"gruppo:{g}")
            for u in clause.get("users", []):
                p = self._user(u)
                if p:
                    principals.add(p)
                else:
                    self.unmapped.add("utente:" + (u.get("email") or u.get("username")
                                                   or u.get("accountId") or "?"))
            clauses.append(sorted(principals) or [NOBODY])
        return clauses


UNREADABLE = [[NOBODY]]


def make_mapper(cfg: Config) -> SourceAclMapper:
    provider = None
    if cfg.identity_provider != "none":
        from .identity import build_identity_provider
        provider = build_identity_provider(cfg)
    return SourceAclMapper(cfg, provider)


def read_page_acl(adapter, ref, mapper: SourceAclMapper) -> tuple[list | None, bool]:
    """(clausole per il frontmatter, leggibili?). Clausole None = la fonte
    non espone permessi; non leggibili = [[NOBODY]] (solo admin)."""
    from .adapters.base import AclUnreadable
    try:
        native = adapter.fetch_acl(ref)
    except AclUnreadable as e:
        log.warning("%s", e)
        return UNREADABLE, False
    return mapper.map(native), True
