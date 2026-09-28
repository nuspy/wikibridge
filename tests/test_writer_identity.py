from wikibridge.wiki.writer import WikiWriter

from .conftest import source_pages


def test_same_title_different_documents_do_not_overwrite(project):
    w = WikiWriter(project)
    p1, _ = w.write_source(title="Report", markdown="uno", doc_id="file:a")
    p2, _ = w.write_source(title="Report", markdown="due", doc_id="file:b")
    assert p1 != p2
    assert len(source_pages(project)) == 2
    assert "uno" in p1.read_text(encoding="utf-8")


def test_same_document_reuses_its_page(project):
    w = WikiWriter(project)
    p1, _ = w.write_source(title="Report", markdown="uno", doc_id="file:a")
    p2, changed = w.write_source(title="Report", markdown="uno bis", doc_id="file:a")
    assert p1 == p2 and changed
    assert len(source_pages(project)) == 1


def test_rel_path_keeps_page_when_title_changes(project):
    w = WikiWriter(project)
    p1, _ = w.write_source(title="Vecchio titolo", markdown="x", doc_id="d:1")
    rel = f"sources/{p1.name}"
    p2, changed = w.write_source(title="Nuovo titolo", markdown="x",
                                 doc_id="d:1", rel_path=rel)
    assert p2 == p1 and changed          # titolo cambiato: va reindicizzato
    fm, _ = w.read_page(rel)
    assert fm["title"] == "Nuovo titolo"
    assert len(source_pages(project)) == 1


def test_version_fields_and_increment_only_on_content_change(project):
    w = WikiWriter(project)
    p, _ = w.write_source(title="Doc", markdown="v1", doc_id="d:1",
                          native_id="123", revision="r1")
    rel = f"sources/{p.name}"
    fm, _ = w.read_page(rel)
    assert fm["content_version"] == 1
    assert fm["doc_id"] == "d:1" and fm["native_id"] == "123"
    assert fm["pipeline_version"] and fm["first_seen"]
    first_seen = fm["first_seen"]

    _, changed = w.write_source(title="Doc", markdown="v1", doc_id="d:1",
                                rel_path=rel, revision="r2")
    assert not changed
    assert w.read_page(rel)[0]["content_version"] == 1

    w.write_source(title="Doc", markdown="v2", doc_id="d:1", rel_path=rel,
                   revision="r3")
    fm, _ = w.read_page(rel)
    assert fm["content_version"] == 2
    assert fm["revision"] == "r3"
    assert fm["first_seen"] == first_seen


def test_acl_inherited_on_update(project):
    w = WikiWriter(project)
    p, _ = w.write_source(title="Doc", markdown="v1", doc_id="d:1",
                          acl_roles=["team-legal"], acl_classification="confidential")
    rel = f"sources/{p.name}"
    w.write_source(title="Doc", markdown="v2", doc_id="d:1", rel_path=rel)
    fm, _ = w.read_page(rel)
    assert fm["acl_roles"] == ["team-legal"]
    assert fm["acl_classification"] == "confidential"
