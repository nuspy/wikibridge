import json
import os

from wikibridge.updater import (DEFAULT_FILES_SOURCE, IngestsLog, REMOVE_AFTER,
                                check_updates)
from wikibridge.wiki.search import SearchIndex
from wikibridge.wiki.writer import WikiWriter

from .conftest import source_pages


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _bump_mtime(path):
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))


def _src(report, kind=None):
    return [s for s in report["sources"] if kind is None or s["kind"] == kind]


def test_dir_branch_add_then_idempotent_then_update(cfg, project, tmp_path):
    d = tmp_path / "docs"
    _write(d / "a.md", "alfa uno")
    _write(d / "b.txt", "beta uno")

    r = check_updates(cfg, project, paths=[str(d)])
    assert r["totals"]["added"] == 2
    assert len(source_pages(project)) == 2

    page = project.wiki_dir / "sources" / "a.md"
    before = page.stat().st_mtime_ns
    r = check_updates(cfg, project)
    assert r["totals"]["unchanged"] == 2 and r["totals"]["added"] == 0
    assert page.stat().st_mtime_ns == before          # nessuna riscrittura

    _write(d / "a.md", "alfa due")
    _bump_mtime(d / "a.md")
    r = check_updates(cfg, project)
    assert r["totals"]["updated"] == 1
    assert len(source_pages(project)) == 2            # nessun duplicato
    fm, body = WikiWriter(project).read_page("sources/a.md")
    assert fm["content_version"] == 2 and "alfa due" in body
    hits = SearchIndex(project).query("due")
    assert [h["doc_id"] for h in hits] == [fm["doc_id"]]
    assert hits[0]["version"] == 2


def test_same_file_name_in_different_dirs(cfg, project, tmp_path):
    _write(tmp_path / "d1" / "report.md", "primo report")
    _write(tmp_path / "d2" / "report.md", "secondo report")
    check_updates(cfg, project, paths=[str(tmp_path / "d1"), str(tmp_path / "d2")])
    assert len(source_pages(project)) == 2


def test_rename_keeps_same_page(cfg, project, tmp_path):
    d = tmp_path / "docs"
    _write(d / "a.md", "contenuto stabile")
    check_updates(cfg, project, paths=[str(d)])
    (d / "a.md").rename(d / "c.md")

    r = check_updates(cfg, project)
    assert r["totals"]["renamed"] == 1 and r["totals"]["removed"] == 0
    assert source_pages(project) == ["a.md"]          # stessa pagina
    fm, _ = WikiWriter(project).read_page("sources/a.md")
    assert fm["title"] == "c" and fm["original_path"].endswith("c.md")


def test_deleted_file_removed_only_after_grace(cfg, project, tmp_path):
    d = tmp_path / "docs"
    _write(d / "a.md", "resta")
    _write(d / "b.md", "sparirà")
    check_updates(cfg, project, paths=[str(d)])
    (d / "b.md").unlink()

    r = check_updates(cfg, project)
    assert r["totals"]["missing"] == 1 and r["totals"]["removed"] == 0
    assert "b.md" in source_pages(project)
    for _ in range(REMOVE_AFTER - 1):
        r = check_updates(cfg, project)
    assert r["totals"]["removed"] == 1
    assert source_pages(project) == ["a.md"]
    assert SearchIndex(project).query("sparirà") == []


def test_unreachable_root_never_deletes(cfg, project, tmp_path):
    d = tmp_path / "docs"
    _write(d / "a.md", "uno")
    check_updates(cfg, project, paths=[str(d)])
    d.rename(tmp_path / "smontato")
    for _ in range(REMOVE_AFTER + 1):
        r = check_updates(cfg, project)
    assert _src(r, "dir")[0]["state"] == "unreachable"
    assert source_pages(project) == ["a.md"]


def test_mass_disappearance_is_not_deleted(cfg, project, tmp_path):
    d = tmp_path / "docs"
    for i in range(8):
        _write(d / f"f{i}.md", f"file {i}")
    check_updates(cfg, project, paths=[str(d)])
    for i in range(6):
        (d / f"f{i}.md").unlink()
    for _ in range(REMOVE_AFTER + 1):
        r = check_updates(cfg, project)
    assert r["totals"]["removed"] == 0
    assert any("sospetta" in e for e in _src(r, "dir")[0]["errors"])
    assert len(source_pages(project)) == 8


def test_single_files_list(cfg, project, tmp_path):
    f = tmp_path / "sparsi" / "nota.md"
    _write(f, "prima")
    r = check_updates(cfg, project, paths=[str(f)])
    assert r["totals"]["added"] == 1
    assert IngestsLog(project).get(DEFAULT_FILES_SOURCE)["kind"] == "files"

    _write(f, "dopo")
    _bump_mtime(f)
    r = check_updates(cfg, project)                   # anche senza --path
    assert r["totals"]["updated"] == 1
    assert len(source_pages(project)) == 1


def test_new_file_inside_registered_dir_joins_that_branch(cfg, project, tmp_path):
    d = tmp_path / "docs"
    _write(d / "a.md", "uno")
    check_updates(cfg, project, paths=[str(d)])
    _write(d / "nuovo.md", "due")
    r = check_updates(cfg, project, paths=[str(d / "nuovo.md")])
    assert _src(r, "dir")[0]["added"] == 1
    assert DEFAULT_FILES_SOURCE not in {s["id"] for s in IngestsLog(project).load()}


def test_update_preserves_acl_tags(cfg, project, tmp_path):
    d = tmp_path / "docs"
    _write(d / "a.md", "uno")
    check_updates(cfg, project, paths=[str(d)])
    from wikibridge.acl_admin import set_page_acl
    w = WikiWriter(project)
    set_page_acl(project, "sources/a.md", ["team-legal"])
    assert w.read_page("sources/a.md")[0]["content_version"] == 1   # solo ACL
    _write(d / "a.md", "due")
    _bump_mtime(d / "a.md")
    check_updates(cfg, project)
    fm, _ = w.read_page("sources/a.md")
    assert fm["acl_roles"] == ["team-legal"] and fm["content_version"] == 2


def test_legacy_ingested_documents_are_adopted(cfg, project, tmp_path):
    f = tmp_path / "vecchio.md"
    _write(f, "contenuto")
    WikiWriter(project).write_source(
        title="vecchio", markdown="contenuto", url=f.as_uri(),
        source_kind="document", import_id="ingest",
        extra_fm={"original_path": str(f), "format": ".md"})
    r = check_updates(cfg, project)
    assert r["totals"]["added"] == 0
    assert source_pages(project) == ["vecchio.md"]
    assert IngestsLog(project).get(DEFAULT_FILES_SOURCE)["files"]


def test_dry_run_writes_nothing(cfg, project, tmp_path):
    d = tmp_path / "docs"
    _write(d / "a.md", "uno")
    r = check_updates(cfg, project, paths=[str(d)], dry_run=True)
    assert r["totals"]["added"] == 1
    assert source_pages(project) == []
    src = IngestsLog(project).load()[0]
    assert src["files"] == {}


def test_reingest_same_dir_does_not_duplicate(cfg, project, tmp_path):
    from wikibridge.convert.docs import ingest_path
    d = tmp_path / "docs"
    _write(d / "a.md", "uno")
    ingest_path(cfg, project, d)
    ingest_path(cfg, project, d)
    assert source_pages(project) == ["a.md"]
    assert len(IngestsLog(project).load()) == 1
    assert json.loads(project.ingests_json.read_text(encoding="utf-8"))[0]["kind"] == "dir"
