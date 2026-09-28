from wikibridge.catalog import apply_catalog_result, remove_source_contributions
from wikibridge.wiki.writer import WikiWriter


def _entity(project, name="ACME"):
    return WikiWriter(project).read_page(f"entities/{name.lower()}.md")


def _result(summary, name="ACME"):
    return {"entities": [{"name": name, "type": "organizzazione",
                          "summary": summary}]}


def test_recatalog_replaces_summary_instead_of_appending(project):
    apply_catalog_result(project, "sources/a.md", _result("versione vecchia"))
    apply_catalog_result(project, "sources/a.md", _result("versione nuova"))
    fm, body = _entity(project)
    assert "versione nuova" in body and "versione vecchia" not in body
    assert body.count("versione nuova") == 1
    assert fm["mentions"] == ["sources/a.md"]


def test_removing_one_source_keeps_the_others(project):
    apply_catalog_result(project, "sources/a.md", _result("da a"))
    apply_catalog_result(project, "sources/b.md", _result("da b"))
    remove_source_contributions(project, "sources/a.md")
    fm, body = _entity(project)
    assert "da b" in body and "da a" not in body
    assert fm["mentions"] == ["sources/b.md"]


def test_entity_deleted_when_last_source_goes(project):
    apply_catalog_result(project, "sources/a.md", _result("da a"))
    remove_source_contributions(project, "sources/a.md")
    assert not (project.wiki_dir / "entities" / "acme.md").exists()


def test_legacy_text_dropped_when_single_source_recatalogued(project):
    w = WikiWriter(project)
    w.write_page("entities", "acme", {"title": "ACME", "mentions": ["sources/a.md"]},
                 "# ACME\n\nriassunto legacy\n\n## Menzioni\n\n- [[sources/a.md]]")
    apply_catalog_result(project, "sources/a.md", _result("riassunto nuovo"))
    _, body = _entity(project)
    assert "riassunto legacy" not in body and "riassunto nuovo" in body


def test_relations_of_removed_source_are_dropped(project):
    apply_catalog_result(project, "sources/a.md",
                         {"relations": [{"from": "x", "to": "y", "kind": "usa"}]})
    apply_catalog_result(project, "sources/b.md",
                         {"relations": [{"from": "y", "to": "z", "kind": "usa"}]})
    remove_source_contributions(project, "sources/a.md")
    text = (project.graph_dir / "relations.jsonl").read_text(encoding="utf-8")
    assert "sources/a.md" not in text and "sources/b.md" in text
