"""Adapter MediaWiki (Wikipedia e wiki aziendali).

Usa l'action API (api.php):
- detect: probe di api.php (action=query&meta=siteinfo)
- fetch: action=parse (HTML del contenuto, niente chrome) → markdown,
  link interni da prop=links del parse, revisione = revid
- subtree: categorymembers ricorsivo (la pagina seed dev'essere una
  Category:) o, per pagine normali, prop=links della seed
- space: generator=allpages del namespace 0 (cap a max_pages)
- etiquette: maxlag=5 su ogni chiamata (i 5xx/429 li gestisce il retry
  della sessione); rate limit per host già nel ThrottledSession
"""
from __future__ import annotations

import logging
import re
from urllib.parse import quote, urlparse

from .base import PageContent, PageGoneError, PageRef, SourceAdapter
from ..convert.html_md import html_to_markdown

log = logging.getLogger("wikibridge.mediawiki")

_WIKI_PATH_RE = re.compile(r"/(?:wiki|index\.php)(?:/|\?title=)([^?&#]+)")


class MediaWikiAdapter(SourceAdapter):
    name = "mediawiki"
    LISTING_SCOPES = frozenset({"subtree", "space"})

    def __init__(self, session, limits):
        super().__init__(session, limits)
        self._api = ""        # https://host/w/api.php
        self._base = ""       # https://host

    # -- riconoscimento -------------------------------------------------------

    @classmethod
    def _candidate_apis(cls, url: str) -> list[str]:
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        return [f"{base}/w/api.php", f"{base}/api.php",
                f"{base}/mediawiki/api.php"]

    @classmethod
    def detect(cls, url: str, session) -> bool:
        if not _WIKI_PATH_RE.search(urlparse(url).path + "?" + (urlparse(url).query or "")):
            return False
        for api in cls._candidate_apis(url):
            try:
                r = session.get(api, params={
                    "action": "query", "meta": "siteinfo", "format": "json"})
                if r.status_code == 200 and "query" in r.json():
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def _ensure_api(self, url: str) -> None:
        if self._api:
            return
        for api in self._candidate_apis(url):
            try:
                r = self.session.get(api, params={
                    "action": "query", "meta": "siteinfo", "format": "json"})
                if r.status_code == 200 and "query" in r.json():
                    self._api = api
                    p = urlparse(url)
                    self._base = f"{p.scheme}://{p.netloc}"
                    return
            except Exception:  # noqa: BLE001
                continue
        raise RuntimeError(f"api.php non trovata per {url}")

    def _call(self, **params) -> dict:
        params.setdefault("format", "json")
        params.setdefault("maxlag", 5)
        r = self.session.get(self._api, params=params)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            if data["error"].get("code") == "missingtitle":
                raise PageGoneError(data["error"].get("info", "missingtitle"))
            raise RuntimeError(f"MediaWiki API: {data['error'].get('info')}")
        return data

    @staticmethod
    def _title_from_url(url: str) -> str:
        m = _WIKI_PATH_RE.search(urlparse(url).path + "?" + (urlparse(url).query or ""))
        if not m:
            raise ValueError(f"titolo non ricavabile dall'URL: {url}")
        from urllib.parse import unquote
        return unquote(m.group(1)).replace("_", " ")

    def _page_url(self, title: str) -> str:
        return f"{self._base}/wiki/{quote(title.replace(' ', '_'))}"

    # -- contratto --------------------------------------------------------------

    def resolve(self, url: str, scope: str) -> list[PageRef]:
        self._ensure_api(url)
        title = self._title_from_url(url)
        if scope == "page":
            return [PageRef(id=title, url=url, title=title, depth=0)]

        if scope == "subtree":
            if title.lower().startswith(("category:", "categoria:")):
                return self._category_members(title)
            # pagina normale: seed = pagina + suoi link diretti
            refs = [PageRef(id=title, url=url, title=title, depth=0)]
            refs += self._page_links(title, depth=0)
            return refs[: self.limits.max_pages]

        # space: tutte le pagine del namespace principale (cap)
        refs: list[PageRef] = []
        cont: dict = {}
        while len(refs) < self.limits.max_pages:
            data = self._call(action="query", list="allpages", apnamespace=0,
                              aplimit=min(500, self.limits.max_pages - len(refs)),
                              **cont)
            for p in data["query"]["allpages"]:
                refs.append(PageRef(id=p["title"],
                                    url=self._page_url(p["title"]),
                                    title=p["title"], depth=0))
            cont = data.get("continue") or {}
            if not cont:
                break
        return refs

    def _category_members(self, category: str) -> list[PageRef]:
        """Membri della categoria, ricorsivo sulle sottocategorie (con cap)."""
        refs: list[PageRef] = []
        seen_cats: set[str] = set()
        stack = [category]
        while stack and len(refs) < self.limits.max_pages:
            cat = stack.pop()
            if cat in seen_cats:
                continue
            seen_cats.add(cat)
            cont: dict = {}
            while len(refs) < self.limits.max_pages:
                data = self._call(action="query", list="categorymembers",
                                  cmtitle=cat, cmlimit=500,
                                  cmtype="page|subcat", **cont)
                for m in data["query"]["categorymembers"]:
                    t = m["title"]
                    if t.lower().startswith(("category:", "categoria:")):
                        stack.append(t)
                    else:
                        refs.append(PageRef(id=t, url=self._page_url(t),
                                            title=t, depth=0))
                cont = data.get("continue") or {}
                if not cont:
                    break
        return refs

    def _page_links(self, title: str, depth: int) -> list[PageRef]:
        refs: list[PageRef] = []
        cont: dict = {}
        while True:
            data = self._call(action="query", titles=title, prop="links",
                              plnamespace=0, pllimit=500, **cont)
            pages = data["query"]["pages"]
            for p in pages.values():
                for l in p.get("links", []):
                    refs.append(PageRef(id=l["title"],
                                        url=self._page_url(l["title"]),
                                        title=l["title"], depth=depth + 1))
            cont = data.get("continue") or {}
            if not cont:
                break
        return refs

    def fetch(self, ref: PageRef) -> PageContent:
        title = ref.id or self._title_from_url(ref.url)
        ref.id = title
        data = self._call(action="parse", page=title,
                          prop="text|revid|links|displaytitle",
                          disableeditsection=1, disabletoc=1, redirects=1)
        parse = data["parse"]
        html = parse["text"]["*"]
        if len(html.encode()) > self.limits.max_page_mb * 1024 * 1024:
            raise ValueError(f"pagina oltre {self.limits.max_page_mb} MB")
        md, _ = html_to_markdown(html, ref.url)
        ref.title = parse.get("title") or title
        links = [PageRef(id=l["*"], url=self._page_url(l["*"]),
                         title=l["*"], depth=ref.depth + 1)
                 for l in parse.get("links", [])
                 if l.get("ns") == 0 and "exists" in l]
        return PageContent(ref=ref, markdown=md, links=links,
                           revision=str(parse.get("revid", "")))

    def changed_since(self, ref: PageRef, revision: str | None) -> bool:
        if not revision:
            return True
        title = ref.id or self._title_from_url(ref.url)
        try:
            data = self._call(action="query", titles=title, prop="revisions",
                              rvprop="ids", rvlimit=1)
            for p in data["query"]["pages"].values():
                revs = p.get("revisions") or []
                if revs:
                    return str(revs[0]["revid"]) != revision
        except Exception:  # noqa: BLE001
            pass
        return True
