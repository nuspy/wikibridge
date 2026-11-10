"""Permessi letti da Confluence: ereditati dagli antenati, tradotti in
principal di wikibridge, aggiornati quando cambiano alla fonte, e negati
(fail-closed) quando non si leggono o non si traducono."""
import pytest

from wikibridge.acl import NOBODY, Access
from wikibridge.acl_admin import set_collection
from wikibridge.importer import run_import
from wikibridge.projects import ProjectStore
from wikibridge.updater import check_updates
from wikibridge.wiki.search import SearchIndex
from wikibridge.wiki.writer import WikiWriter

from .confluence_fixture import FakeConfluence
from .ldap_fixture import MARIO, configure

LEGAL = "team-legal"


@pytest.fixture
def conf():
    c = FakeConfluence()
    c.put("100", "Spazio", "pagina radice")
    c.put("101", "Contratti", "elenco contratti", parent="100", groups=["conf-legal"])
    c.put("102", "Contratto ACME", "penale ACME", parent="101", users=["mario"])
    c.put("103", "Procedure", "procedura acquisti", parent="100")
    c.put("104", "Segreto", "fusione riservata", parent="100")
    c.put("105", "Board", "verbale board", parent="100", groups=["conf-board"])
    c.forbidden.add("104")
    yield c
    c.stop()


@pytest.fixture
def project(cfg, conf):
    configure(cfg)                                  # directory LDAP finto
    cfg.source_group_map = {"conf-legal": LEGAL}
    from wikibridge import source_acl
    from .ldap_fixture import make_provider
    provider, _ = make_provider(cfg)              # directory finto per gli utenti
    orig = source_acl.make_mapper
    source_acl.make_mapper = lambda c: source_acl.SourceAclMapper(c, provider)
    project = ProjectStore(cfg).get("default")
    rec = run_import(cfg, project, conf.url("100"), scope="subtree", depth=0,
                     permissions=True)
    assert rec["state"] == "done", rec["errors"]
    project.rec = rec
    yield project
    source_acl.make_mapper = orig


def _fm(project, title):
    w = WikiWriter(project)
    for rel, fm, _ in w.iter_pages():
        if fm.get("title") == title:
            return rel, fm
    raise KeyError(title)


def _sees(project, principals, query):
    acc = Access(project, principals=frozenset(principals), is_admin=False, enforce=True)
    idx = SearchIndex(project)
    return [h["title"] for h in idx.query(query, acl_ids=acc.acl_ids(idx))]


def test_restrictions_are_inherited_and_translated(project):
    assert _fm(project, "Contratti")[1]["acl_source_clauses"] == [[LEGAL]]
    # restrizione propria + quella ereditata dal padre, in AND
    assert _fm(project, "Contratto ACME")[1]["acl_source_clauses"] == [[LEGAL], [MARIO]]
    assert _sees(project, {LEGAL, MARIO}, "penale") == ["Contratto ACME"]
    assert _sees(project, {LEGAL}, "penale") == []            # manca la persona
    assert _sees(project, {MARIO}, "penale") == []            # manca il gruppo


def test_unreadable_and_unmapped_are_fail_closed(project):
    assert _fm(project, "Segreto")[1]["acl_source_clauses"] == [[NOBODY]]
    assert _fm(project, "Board")[1]["acl_source_clauses"] == [[NOBODY]]
    assert project.rec["acl_unreadable"] == 1
    assert "gruppo:conf-board" in project.rec["acl_unmapped"]
    assert _sees(project, {LEGAL, MARIO, "conf-board"}, "fusione verbale") == []


def test_unrestricted_pages_need_a_collection(project):
    assert _fm(project, "Procedure")[1]["acl_source_clauses"] == []
    assert _sees(project, {LEGAL}, "procedura") == []          # default-deny
    set_collection(project, "spazio-leg", principals=["dipendenti"],
                   classification="internal", sources=[project.rec["id"]])
    assert _sees(project, {"dipendenti"}, "procedura") == ["Procedure"]
    # la collezione si somma alle restrizioni, non le allarga
    assert _sees(project, {"dipendenti", LEGAL}, "penale") == []


def test_permission_change_without_content_change(cfg, project, conf):
    rel, fm = _fm(project, "Contratto ACME")
    conf.restrict("102", users=["mario", "luca"])            # versione invariata
    conf.restrict("101", groups=[])                           # tolta la restrizione del padre
    r = check_updates(cfg, project)["sources"][0]
    assert r["acl_updated"] == 2 and r["updated"] == 0
    rel, fm = _fm(project, "Contratto ACME")
    assert fm["acl_source_clauses"] == [sorted([MARIO, "user:aaaaaaaa-0000-0000-0000-000000000003"])]
    assert fm["content_version"] == 1                          # testo non riscritto
    assert _sees(project, {MARIO}, "penale") == ["Contratto ACME"]


def test_moved_page_gets_new_inherited_restrictions(cfg, project, conf):
    conf.pages["102"]["parent"] = "103"                       # spostata fuori da Contratti
    check_updates(cfg, project)
    assert _fm(project, "Contratto ACME")[1]["acl_source_clauses"] == [[MARIO]]
