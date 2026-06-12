"""Adapter Confluence (Cloud e Server/Data Center).

REST API:
- detect: URL con /wiki/spaces/... o /display/..., o probe /rest/api/space
- fetch: GET /rest/api/content/<id>?expand=body.storage,version,children.page
  → body.storage (XHTML) convertito in markdown; revisione = version.number
- subtree: children.page ricorsivo dal seed
- space: /rest/api/content?spaceKey=...&type=page (paginato)
Auth tipica: profilo basic (email + API token su Cloud) o bearer (PAT).
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

from .base import PageContent, PageRef, SourceAdapter
from ..convert.html_md import html_to_markdown

log = logging.getLogger("wikibridge.confluence")

# /wiki/spaces/KEY/pages/12345/Titolo  |  /pages/viewpage.action?pageId=12345
_PAGEID_RE = re.compile(r"/pages/(\d+)")
_VIEWPAGE_RE = re.compile(r"pageId=(\d+)")
_SPACE_RE = re.compile(r"/spaces/([^/]+)")


class ConfluenceAdapter(SourceAdapter):
    name = "confluence"

    def __init__(self, session, limits):
        super().__init__(session, limits)
        self._rest = ""      # https://host(/wiki)/rest/api
        self._base = ""

    @classmethod
    def detect(cls, url: str, session) -> bool:
        path = urlparse(url).path
        return ("/wiki/spaces/" in path or "/display/" in path
                or "viewpage.action" in path
                or bool(_PAGEID_RE.search(path)))

    def _ensure_rest(self, url: str) -> None:
        if self._rest:
            return
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        candidates = [f"{base}/wiki/rest/api", f"{base}/rest/api"]
        for rest in candidates:
            try:
                r = self.session.get(f"{rest}/space", params={"limit": 1})
                if r.status_code in (200, 403):   # 403 = REST esiste, auth da rivedere
                    self._rest = rest
                    self._base = base
                    if r.status_code == 403:
                        log.warning("Confluence REST risponde 403: profilo auth mancante o senza permessi?")
                    return
            except Exception:  # noqa: BLE001
                continue
        raise RuntimeError(f"REST API Confluence non trovata per {url}")

    def _get(self, path: str, **params) -> dict:
        r = self.session.get(f"{self._rest}{path}", params=params)
        r.raise_for_status()
        return r.json()

    def _page_ref(self, content: dict, depth: int = 0) -> PageRef:
        link = content.get("_links", {}).get("webui", "")
        return PageRef(id=str(content["id"]),
                       url=f"{self._base}/wiki{link}" if link.startswith("/spaces")
                           else f"{self._base}{link}",
                       title=content.get("title", ""), depth=depth)

    def _content_id_from_url(self, url: str) -> str:
        m = _PAGEID_RE.search(url) or _VIEWPAGE_RE.search(url)
        if m:
            return m.group(1)
        # /display/SPACE/Titolo: risolvi per titolo
        parts = urlparse(url).path.split("/display/", 1)
        if len(parts) == 2:
            space, _, title = parts[1].partition("/")
            data = self._get("/content", spaceKey=space,
                             title=title.replace("+", " "), limit=1)
            if data.get("results"):
                return str(data["results"][0]["id"])
        raise ValueError(f"id pagina non ricavabile dall'URL: {url}")

    # -- contratto --------------------------------------------------------------

    def resolve(self, url: str, scope: str) -> list[PageRef]:
        self._ensure_rest(url)
        if scope == "space":
            m = _SPACE_RE.search(url)
            if not m:
                raise ValueError("scope=space richiede un URL /spaces/<KEY>/...")
            space = m.group(1)
            refs: list[PageRef] = []
            start = 0
            while len(refs) < self.limits.max_pages:
                data = self._get("/content", spaceKey=space, type="page",
                                 limit=min(100, self.limits.max_pages - len(refs)),
                                 start=start)
                results = data.get("results", [])
                refs += [self._page_ref(c) for c in results]
                if len(results) < data.get("limit", 100):
                    break
                start += len(results)
            return refs

        cid = self._content_id_from_url(url)
        seed = self._get(f"/content/{cid}")
        refs = [self._page_ref(seed)]
        if scope == "subtree":
            stack = [cid]
            while stack and len(refs) < self.limits.max_pages:
                parent = stack.pop()
                start = 0
                while len(refs) < self.limits.max_pages:
                    data = self._get(f"/content/{parent}/child/page",
                                     limit=100, start=start)
                    results = data.get("results", [])
                    for c in results:
                        refs.append(self._page_ref(c))
                        stack.append(str(c["id"]))
                    if len(results) < 100:
                        break
                    start += len(results)
        return refs

    def fetch(self, ref: PageRef) -> PageContent:
        if not self._rest:
            self._ensure_rest(ref.url)
        cid = ref.id or self._content_id_from_url(ref.url)
        data = self._get(f"/content/{cid}",
                         expand="body.storage,version,children.page")
        html = data.get("body", {}).get("storage", {}).get("value", "")
        if len(html.encode()) > self.limits.max_page_mb * 1024 * 1024:
            raise ValueError(f"pagina oltre {self.limits.max_page_mb} MB")
        md, _ = html_to_markdown(html, ref.url)
        ref.title = data.get("title") or ref.title
        children = data.get("children", {}).get("page", {}).get("results", [])
        links = [self._page_ref(c, depth=ref.depth + 1) for c in children]
        return PageContent(ref=ref, markdown=md, links=links,
                           revision=str(data.get("version", {}).get("number", "")))

    def changed_since(self, ref: PageRef, revision: str | None) -> bool:
        if not revision:
            return True
        try:
            cid = ref.id or self._content_id_from_url(ref.url)
            data = self._get(f"/content/{cid}", expand="version")
            return str(data.get("version", {}).get("number", "")) != revision
        except Exception:  # noqa: BLE001
            return True
