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


class SourceAdapter(ABC):
    name: str = "base"

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
