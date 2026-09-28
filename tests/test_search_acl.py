import sqlite3

from wikibridge.acl import Access
from wikibridge.acl_admin import set_collection, set_gate, set_page_acl
from wikibridge.catalog import apply_catalog_result
from wikibridge.wiki.search import SearchIndex
from wikibridge.wiki.writer import WikiWriter


def _add(project, title, markdown, source="imp-0001", **acl):
    w = WikiWriter(project)
    p, _ = w.write_source(title=title, markdown=markdown, doc_id=f"t:{title}",
                          import_id=source, **acl)
    rel = f"sources/{p.name}"
    fm, body = w.read_page(rel)
    SearchIndex(project).upsert_page(rel, "sources", fm, body)
    return rel


def _search(project, q, *principals, admin=False, enforce=True):
    acc = Access(project, principals=frozenset(principals), is_admin=admin,
                 enforce=enforce)
    idx = SearchIndex(project)
    return sorted(h["title"] for h in idx.query(q, acl_ids=acc.acl_ids(idx)))


def _setup(project):
    _add(project, "Riservato", "contratto segreto", acl_roles=["team-legal"])
    _add(project, "Pubblico", "contratto pubblico", acl_classification="public")
    _add(project, "Senza tag", "contratto non taggato")


def test_no_enforcement_is_unchanged(project):
    _setup(project)
    assert _search(project, "contratto", "x", enforce=False) == \
        ["Pubblico", "Riservato", "Senza tag"]


def test_role_filter_default_deny_and_admin(project):
    _setup(project)
    assert _search(project, "contratto", "team-hr") == ["Pubblico"]
    assert _search(project, "contratto", "team-legal") == ["Pubblico", "Riservato"]
    assert _search(project, "contratto", admin=True) == \
        ["Pubblico", "Riservato", "Senza tag"]


def test_personal_grant_after_ingestion_is_immediate(project):
    rel = _add(project, "Nota", "bilancio riservato", acl_roles=["team-finance"])
    luca = "user:luca-guid"
    assert _search(project, "bilancio", luca) == []
    set_page_acl(project, rel, ["team-finance", luca])
    assert _search(project, "bilancio", luca) == ["Nota"]


def test_collection_change_is_by_reference(project):
    _add(project, "A", "piano industriale", source="imp-0001")
    _add(project, "B", "piano commerciale", source="imp-0001")
    _add(project, "C", "piano altro", source="imp-0002")
    set_collection(project, "strategia", principals=["board"],
                   classification="internal", sources=["imp-0001"])
    conn = sqlite3.connect(project.index_db)
    before = conn.execute("SELECT path, acl_id FROM docs ORDER BY path").fetchall()
    assert _search(project, "piano", "board") == ["A", "B"]
    assert _search(project, "piano", "sales") == []
    set_collection(project, "strategia", principals=["board", "sales"],
                   classification="internal", sources=["imp-0001"])
    assert _search(project, "piano", "sales") == ["A", "B"]      # subito
    after = conn.execute("SELECT path, acl_id FROM docs ORDER BY path").fetchall()
    assert before == after                                       # indice intatto


def test_document_can_only_restrict_its_collection(project):
    rel = _add(project, "Dossier", "fusione", source="imp-0001")
    set_collection(project, "legal", principals=["team-legal"],
                   classification="internal", sources=["imp-0001"])
    set_page_acl(project, rel, ["board"])
    assert _search(project, "fusione", "board") == []            # manca team-legal
    assert _search(project, "fusione", "team-legal") == []       # manca board
    assert _search(project, "fusione", "board", "team-legal") == ["Dossier"]


def test_clearance_levels(project):
    _add(project, "Segreto", "progetto aurora", acl_roles=["rd"],
         acl_classification="restricted")
    assert _search(project, "aurora", "rd") == []
    assert _search(project, "aurora", "rd", "clearance:restricted") == ["Segreto"]


def test_project_gate(project):
    _add(project, "Pubblico", "voce pubblica", acl_classification="public")
    set_gate(project, ["dipendenti"])
    assert _search(project, "voce", "esterno") == []
    acc = Access(project, principals=frozenset({"esterno"}), is_admin=False, enforce=True)
    assert not acc.project_visible()
    assert _search(project, "voce", "dipendenti") == ["Pubblico"]


def test_entity_segments_follow_their_source(project):
    _add(project, "Contratto X", "testo", acl_roles=["team-legal"])
    _add(project, "Comunicato", "testo", acl_classification="public")
    apply_catalog_result(project, "sources/contratto-x.md",
                         {"entities": [{"name": "ACME", "summary": "penale riservata"}]})
    apply_catalog_result(project, "sources/comunicato.md",
                         {"entities": [{"name": "ACME", "summary": "partner annunciato"}]})
    assert _search(project, "penale", "team-hr") == []
    assert _search(project, "annunciato", "team-hr") == ["ACME"]
    assert _search(project, "penale", "team-legal") == ["ACME"]


def test_hits_carry_document_and_version(project):
    _add(project, "Doc", "parola unica")
    hit = SearchIndex(project).query("unica")[0]
    assert hit["doc_id"] == "t:Doc" and hit["version"] == 1


def test_old_schema_is_migrated(project, tmp_path):
    from wikibridge.wiki import search
    _add(project, "Doc", "parola unica")
    conn = sqlite3.connect(project.index_db)
    conn.execute("UPDATE meta SET v = '1' WHERE k = 'schema'")
    conn.commit()
    conn.close()
    search._checked.clear()
    assert [h["title"] for h in SearchIndex(project).query("unica")] == ["Doc"]
