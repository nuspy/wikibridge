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

import requests

from .base import AclUnreadable, PageContent, PageRef, SourceAdapter
from ..auth import AuthRequiredError
from ..convert.html_md import html_to_markdown

log = logging.getLogger("wikibridge.confluence")

# /wiki/spaces/KEY/pages/12345/Titolo  |  /pages/viewpage.action?pageId=12345
_PAGEID_RE = re.compile(r"/pages/(\d+)")
_VIEWPAGE_RE = re.compile(r"pageId=(\d+)")
_SPACE_RE = re.compile(r"/spaces/([^/]+)")
FEED_MARGIN_H = 24


class ConfluenceAdapter(SourceAdapter):
    name = "confluence"
    LISTING_SCOPES = frozenset({"subtree", "space"})

    def __init__(self, session, limits):
        super().__init__(session, limits)
        self._rest = ""      # https://host(/wiki)/rest/api
        self._base = ""
        self._restrictions: dict[str, dict | None] = {}

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
            # CQL: tutto il ramo in ~1 chiamata ogni 100 pagine invece di una
            # chiamata "figli" per ogni pagina; ripiego sulla visita se la
            # ricerca non è disponibile
            listed = self._cql_refs(f"type=page and ancestor={cid}")
            if listed is not None:
                return (refs + listed)[: self.limits.max_pages]
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
        ref.id = cid
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

    # -- ricerca CQL (elenchi e change feed) ---------------------------------------

    def _cql_refs(self, cql: str) -> list[PageRef] | None:
        """Risultati di una ricerca CQL (paginata); None se la ricerca non è
        disponibile o fallisce."""
        refs: list[PageRef] = []
        start = 0
        try:
            while len(refs) < self.limits.max_pages:
                data = self._get("/content/search", cql=cql, limit=100, start=start)
                results = data.get("results", [])
                refs += [self._page_ref(c) for c in results]
                if len(results) < 100:
                    return refs
                start += len(results)
        except (requests.RequestException, AuthRequiredError, ValueError) as e:
            log.info("ricerca CQL non disponibile (%s): ripiego", e)
            return None
        return refs

    def changed_ids_since(self, url: str, scope: str, since: str) -> set[str] | None:
        """Pagine del ramo modificate dopo `since`. In CQL lastmodified è
        interpretato nel fuso dell'utente Confluence: margine di 24 ore (le
        pagine in più passano comunque dal controllo di revisione)."""
        from datetime import datetime, timedelta
        try:
            stamp = (datetime.fromisoformat(since) - timedelta(hours=FEED_MARGIN_H)
                     ).strftime("%Y-%m-%d %H:%M")
            self._ensure_rest(url)
            if scope == "space":
                m = _SPACE_RE.search(url)
                if not m:
                    return None
                where = f'space="{m.group(1)}"'
            elif scope == "subtree":
                cid = self._content_id_from_url(url)
                where = f"(ancestor={cid} or id={cid})"
            else:
                return None
        except (ValueError, RuntimeError, requests.RequestException):
            return None
        refs = self._cql_refs(f'type=page and {where} and lastmodified >= "{stamp}"')
        return None if refs is None else {r.id for r in refs}

    def still_in_branch(self, ref: PageRef, url: str, scope: str) -> bool | None:
        cid = ref.id or self._content_id_from_url(ref.url)
        try:
            data = self._get(f"/content/{cid}", expand="ancestors,space")
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return False                              # cancellata davvero
            return None
        except (requests.RequestException, AuthRequiredError):
            return None
        if scope == "space":
            m = _SPACE_RE.search(url)
            return bool(m) and data.get("space", {}).get("key") == m.group(1)
        root = self._content_id_from_url(url)
        return cid == root or root in {str(a["id"]) for a in data.get("ancestors", [])}

    def _node_restriction(self, cid: str) -> dict | None:
        """Restrizione di lettura impostata SU questa pagina (None se non ne
        ha). In cache: le pagine sorelle condividono gli stessi antenati."""
        if cid in self._restrictions:
            return self._restrictions[cid]
        try:
            data = self._get(f"/content/{cid}/restriction/byOperation/read",
                             expand="restrictions.user,restrictions.group")
        except (requests.RequestException, AuthRequiredError) as e:
            raise AclUnreadable(f"restrizioni di {cid} non leggibili: {e}") from e
        r = data.get("restrictions", {})
        users = [{k: u[k] for k in ("accountId", "username", "email") if u.get(k)}
                 for u in r.get("user", {}).get("results", [])]
        groups = [g.get("name") for g in r.get("group", {}).get("results", [])
                  if g.get("name")]
        clause = {"groups": groups, "users": users} if (users or groups) else None
        self._restrictions[cid] = clause
        return clause

    def fetch_acl(self, ref: PageRef) -> list[dict] | None:
        """In Confluence una restrizione di lettura vale per la pagina e per
        tutti i discendenti, e più restrizioni si sommano (intersezione):
        una clausola per ogni pagina ristretta tra la pagina e i suoi
        antenati. I permessi di spazio non vengono letti (su Cloud servono
        diritti di amministratore): si modellano con una collezione."""
        if not self._rest:
            self._ensure_rest(ref.url)
        cid = ref.id or self._content_id_from_url(ref.url)
        try:
            data = self._get(f"/content/{cid}", expand="ancestors")
        except (requests.RequestException, AuthRequiredError) as e:
            raise AclUnreadable(f"antenati di {cid} non leggibili: {e}") from e
        chain = [str(a["id"]) for a in data.get("ancestors", [])] + [str(cid)]
        return [c for c in (self._node_restriction(x) for x in chain) if c]

    def changed_since(self, ref: PageRef, revision: str | None) -> bool:
        if not revision:
            return True
        try:
            cid = ref.id or self._content_id_from_url(ref.url)
            data = self._get(f"/content/{cid}", expand="version")
            return str(data.get("version", {}).get("number", "")) != revision
        except Exception:  # noqa: BLE001
            return True
