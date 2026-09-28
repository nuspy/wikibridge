import pytest

from wikibridge import importer
from wikibridge.adapters.base import (PageContent, PageGoneError, PageRef,
                                      SourceAdapter)
from wikibridge.importer import ImportsLog, run_import, sync_import
from wikibridge.updater import REMOVE_AFTER, check_updates
from wikibridge.wiki.writer import WikiWriter

from .conftest import source_pages

BASE = "https://wiki.test/p/"


class FakeSite:
    def __init__(self):
        self.pages: dict[str, dict] = {}

    def put(self, nid, title, body, children=(), rev=None):
        old = self.pages.get(nid)
        self.pages[nid] = {"title": title, "body": body, "children": list(children),
                           "rev": rev or str(int(old["rev"]) + 1 if old else 1)}


class FakeAdapter(SourceAdapter):
    """Come gli adapter reali: id nativo dal ref, altrimenti dall'URL."""
    name = "fake"
    LISTING_SCOPES = frozenset({"space"})

    def __init__(self, site):
        self.site = site

    @staticmethod
    def _nid(ref):
        return ref.id or ref.url.rsplit("/", 1)[1]

    def resolve(self, url, scope):
        if scope == "space":
            return [PageRef(id=n, url=BASE + n, title=p["title"])
                    for n, p in self.site.pages.items()]
        return [PageRef(id=self._nid(PageRef(id="", url=url)), url=url)]

    def fetch(self, ref):
        nid = self._nid(ref)
        if nid not in self.site.pages:
            raise PageGoneError(nid)
        p = self.site.pages[nid]
        ref.id, ref.title = nid, p["title"]
        links = [PageRef(id=c, url=BASE + c, depth=ref.depth + 1) for c in p["children"]]
        return PageContent(ref=ref, markdown=p["body"], links=links, revision=p["rev"])

    def changed_since(self, ref, revision):
        p = self.site.pages.get(self._nid(ref))
        return p is None or p["rev"] != revision


@pytest.fixture
def site(monkeypatch):
    s = FakeSite()
    monkeypatch.setattr(importer, "detect_adapter",
                        lambda url, session, limits, forced="auto": FakeAdapter(s))
    return s


def _import(cfg, project, scope="page", depth=2):
    return run_import(cfg, project, BASE + "root", scope=scope, depth=depth)


def test_branch_update_finds_new_and_changed_pages(cfg, project, site):
    site.put("root", "Radice", "radice", children=["a"])
    site.put("a", "Pagina A", "testo a")
    rec = _import(cfg, project)
    assert rec["pages"] == 2

    site.put("a", "Pagina A", "testo a modificato", children=["b"])
    site.put("b", "Pagina B", "testo b")
    r = check_updates(cfg, project)
    wiki = r["sources"][0]
    assert (wiki["updated"], wiki["added"], wiki["unchanged"]) == (1, 1, 1)
    assert len(source_pages(project)) == 3

    r = check_updates(cfg, project)                   # idempotente
    assert r["sources"][0]["unchanged"] == 3 and r["totals"]["added"] == 0


def test_title_rename_at_source_does_not_duplicate(cfg, project, site):
    site.put("root", "Radice", "radice")
    _import(cfg, project)
    site.put("root", "Radice rinominata", "radice")
    check_updates(cfg, project)
    pages = source_pages(project)
    assert pages == ["radice.md"]
    assert WikiWriter(project).read_page("sources/radice.md")[0]["title"] == "Radice rinominata"


def test_gone_page_removed_after_grace(cfg, project, site):
    site.put("root", "Radice", "radice", children=["a"])
    site.put("a", "Pagina A", "testo a")
    _import(cfg, project)
    del site.pages["a"]
    r = check_updates(cfg, project)
    assert r["sources"][0]["missing"] == 1
    assert len(source_pages(project)) == 2
    for _ in range(REMOVE_AFTER - 1):
        r = check_updates(cfg, project)
    assert r["sources"][0]["removed"] == 1
    assert source_pages(project) == ["radice.md"]


def test_space_listing_adds_and_removes(cfg, project, site):
    for n in ("root", "x", "y"):
        site.put(n, f"Pagina {n}", f"testo {n}")
    _import(cfg, project, scope="space", depth=0)
    assert len(source_pages(project)) == 3

    site.put("z", "Pagina z", "testo z")             # nuova, senza link
    del site.pages["y"]
    r = check_updates(cfg, project)
    assert r["sources"][0]["added"] == 1 and r["sources"][0]["missing"] == 1
    for _ in range(REMOVE_AFTER - 1):
        check_updates(cfg, project)
    assert source_pages(project) == ["pagina-root.md", "pagina-x.md", "pagina-z.md"]


def test_legacy_page_index_without_native_id(cfg, project, site):
    """Regressione: il vecchio sync passava l'URL come id nativo."""
    site.put("root", "Radice", "radice")
    rec = _import(cfg, project)
    log = ImportsLog(project)
    for info in rec["page_index"].values():
        info.pop("native_id", None)
        info.pop("doc_id", None)
    log.upsert(rec)
    site.put("root", "Radice", "radice nuova")
    result = sync_import(cfg, project, rec["id"])
    assert result["refetched"] == 1
    assert result["report"]["missing"] == 0 and not result["report"]["errors"]
