"""Crawler generico HTML (GitHub wiki, GitBook, DokuWiki, siti doc...).

Nessuna API: fetch della pagina, estrazione contenuto via convert.html_md,
link <a> per la BFS. Vincoli: stesso host SEMPRE; con scope subtree/space
anche il prefisso di path. robots.txt rispettato (configurabile).
Revisione = ETag o Last-Modified per il sync incrementale.

Limiti noti (documentati): pagine renderizzate in JavaScript non sono
supportate (niente browser headless in v1).
"""
from __future__ import annotations

import logging
import urllib.robotparser
from urllib.parse import urlparse

from .base import PageContent, PageRef, SourceAdapter
from ..convert.html_md import extract_links, html_to_markdown

log = logging.getLogger("wikibridge.generic")

_SKIP_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".pdf", ".zip",
             ".tar", ".gz", ".mp4", ".mp3", ".css", ".js", ".ico",
             ".woff", ".woff2")


class GenericAdapter(SourceAdapter):
    name = "generic"

    def __init__(self, session, limits):
        super().__init__(session, limits)
        self._robots: dict[str, urllib.robotparser.RobotFileParser] = {}
        self._seed_host = ""
        self._path_prefix = ""

    @classmethod
    def detect(cls, url: str, session) -> bool:
        return True  # fallback universale

    # -- robots ---------------------------------------------------------------

    def _allowed_by_robots(self, url: str) -> bool:
        if not self.limits.respect_robots:
            return True
        host = urlparse(url).netloc
        rp = self._robots.get(host)
        if rp is None:
            rp = urllib.robotparser.RobotFileParser()
            robots_url = f"{urlparse(url).scheme}://{host}/robots.txt"
            try:
                resp = self.session.get(robots_url)
                if resp.status_code == 200:
                    rp.parse(resp.text.splitlines())
                else:
                    rp.allow_all = True
            except Exception:  # noqa: BLE001 — robots irraggiungibile = permesso
                rp.allow_all = True
            self._robots[host] = rp
        return rp.can_fetch(self.session.headers.get("User-Agent", "*"), url)

    # -- contratto --------------------------------------------------------------

    def resolve(self, url: str, scope: str) -> list[PageRef]:
        parsed = urlparse(url)
        self._seed_host = parsed.netloc
        if scope == "page":
            self._path_prefix = ""           # i link restano stesso-host
        elif scope == "subtree":
            # prefisso = directory della pagina seed
            path = parsed.path.rsplit("/", 1)[0] + "/"
            self._path_prefix = path
        else:                                 # space = tutto l'host
            self._path_prefix = "/"
        return [PageRef(id=url, url=url, depth=0)]

    def _in_scope(self, url: str) -> bool:
        p = urlparse(url)
        if p.netloc != self._seed_host:
            return False
        if any(p.path.lower().endswith(ext) for ext in _SKIP_EXT):
            return False
        if self._path_prefix and not p.path.startswith(self._path_prefix):
            return False
        return True

    def fetch(self, ref: PageRef) -> PageContent:
        if not self._allowed_by_robots(ref.url):
            raise PermissionError(f"robots.txt vieta: {ref.url}")
        resp = self.session.get(ref.url)
        resp.raise_for_status()
        ctype = resp.headers.get("Content-Type", "")
        if "html" not in ctype and "xml" not in ctype:
            raise ValueError(f"non è HTML ({ctype}): {ref.url}")
        if len(resp.content) > self.limits.max_page_mb * 1024 * 1024:
            raise ValueError(f"pagina oltre {self.limits.max_page_mb} MB")

        md, title = html_to_markdown(resp.text, ref.url)
        ref.title = ref.title or title
        links = [PageRef(id=u, url=u, depth=ref.depth + 1)
                 for u in extract_links(resp.text, ref.url) if self._in_scope(u)]
        revision = resp.headers.get("ETag") or resp.headers.get("Last-Modified")
        return PageContent(ref=ref, markdown=md, links=links, revision=revision)

    def changed_since(self, ref: PageRef, revision: str | None) -> bool:
        if not revision:
            return True
        try:
            resp = self.session.head(ref.url, allow_redirects=True)
            current = resp.headers.get("ETag") or resp.headers.get("Last-Modified")
            return current != revision if current else True
        except Exception:  # noqa: BLE001
            return True
