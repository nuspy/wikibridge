"""Contratto degli adapter sorgente.

Un adapter sa: riconoscere un URL (detect), produrre il seed set per uno
scope (resolve), scaricare e convertire una pagina (fetch) e dire se è
cambiata rispetto a una revisione nota (changed_since, per il sync).
La BFS sui link, i limiti e la scrittura wiki stanno nell'importer.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import requests

from ..limits import ImportLimits


@dataclass
class PageRef:
    id: str            # id nativo dell'adapter (pageid, content id, URL...)
    url: str
    title: str = ""
    depth: int = 0


@dataclass
class PageContent:
    ref: PageRef
    markdown: str
    links: list[PageRef] = field(default_factory=list)  # outlink stesso-wiki
    revision: str | None = None
    attachments: list[dict] = field(default_factory=list)


class PageGoneError(Exception):
    """La pagina non esiste più alla sorgente (cancellata o rinominata)."""


class AclUnreadable(Exception):
    """I permessi della pagina non si possono leggere (l'account di import
    non ha il diritto, API assente...): la pagina va negata, non aperta."""


class SourceAdapter(ABC):
    name: str = "base"
    # scope per cui resolve() restituisce l'elenco COMPLETO del ramo (serve
    # all'updater per scoprire pagine nuove e sparite senza rifare la BFS)
    LISTING_SCOPES: frozenset[str] = frozenset()

    def __init__(self, session: requests.Session, limits: ImportLimits):
        self.session = session
        self.limits = limits

    @classmethod
    def detect(cls, url: str, session: requests.Session) -> bool:
        """True se l'URL appartiene a questo tipo di wiki (sniffing leggero)."""
        return False

    @abstractmethod
    def resolve(self, url: str, scope: str) -> list[PageRef]:
        """Seed set per lo scope: page | subtree | space."""

    @abstractmethod
    def fetch(self, ref: PageRef) -> PageContent:
        """Scarica e converte una pagina in markdown."""

    def changed_since(self, ref: PageRef, revision: str | None) -> bool:
        """False solo se si può provare che la pagina NON è cambiata."""
        return True

    def changed_ids_since(self, url: str, scope: str, since: str) -> set[str] | None:
        """Change feed: id nativi delle pagine del ramo modificate dopo
        `since` (ISO UTC), con UNA ricerca invece di un controllo per pagina.
        Può includere pagine non cambiate (margini di sicurezza), non deve
        ometterne. None = non disponibile: si controlla pagina per pagina."""
        return None

    def still_in_branch(self, ref: PageRef, url: str, scope: str) -> bool | None:
        """Una pagina che manca dall'elenco del ramo esiste ancora nel ramo?
        (Gli elenchi da indici di ricerca possono essere in ritardo.) None =
        non verificabile: vale l'elenco."""
        return None

    def fetch_acl(self, ref: PageRef) -> list[dict] | None:
        """Restrizioni di lettura della pagina alla fonte, come clausole in
        AND: [{"groups": [...], "users": [{"username"|"email"|"accountId"}]}].
        [] = nessuna restrizione propria o ereditata; None = la fonte non
        espone permessi (si usano solo quelli assegnati in wikibridge).
        Solleva AclUnreadable se i permessi esistono ma non si leggono."""
        return None
