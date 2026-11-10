"""Permessi NTFS dei file locali: tradotti in principal, aggiornati quando
cambiano senza che cambi il contenuto, negati (solo admin) quando non si
leggono o contengono voci "nega". file_read_acl è simulata: i test non
dipendono da pywin32 né dai permessi reali del disco."""
import os

import pytest

from wikibridge import ntfs_acl, source_acl
from wikibridge.acl import NOBODY, Access
from wikibridge.adapters.base import AclUnreadable
from wikibridge.projects import ProjectStore
from wikibridge.updater import check_updates
from wikibridge.wiki.search import SearchIndex
from wikibridge.wiki.writer import WikiWriter

from .ldap_fixture import MARIO, configure, make_provider

LEGAL = "team-legal"


@pytest.fixture
def acls(monkeypatch):
    """nome file -> ACL simulata (o eccezione)."""
    table: dict = {}

    def fake(path):
        v = table[os.path.basename(str(path))]
        if isinstance(v, Exception):
            raise v
        return v
    monkeypatch.setattr(ntfs_acl, "file_read_acl", fake)
    return table


@pytest.fixture
def project(cfg, monkeypatch):
    configure(cfg)
    cfg.source_group_map = {"CORP\\Legal": LEGAL}
    provider, _ = make_provider(cfg)
    monkeypatch.setattr(source_acl, "make_mapper",
                        lambda c: source_acl.SourceAclMapper(c, provider))
    return ProjectStore(cfg).get("default")


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _fm(project, name):
    return WikiWriter(project).read_page(f"sources/{name}.md")[0]


def _sees(project, principals, query):
    acc = Access(project, principals=frozenset(principals), is_admin=False, enforce=True)
    idx = SearchIndex(project)
    return [h["title"] for h in idx.query(query, acl_ids=acc.acl_ids(idx))]


def test_file_permissions_are_translated(cfg, project, acls, tmp_path):
    d = tmp_path / "docs"
    _write(d / "contratto.md", "penale contratto")
    _write(d / "nota.md", "nota personale")
    acls["contratto.md"] = [{"groups": ["CORP\\Legal"], "users": []}]
    acls["nota.md"] = [{"groups": [], "users": [{"username": "mario"}]}]

    r = check_updates(cfg, project, paths=[str(d)], permissions=True)
    assert r["totals"]["added"] == 2
    assert _fm(project, "contratto")["acl_source_clauses"] == [[LEGAL]]
    assert _fm(project, "nota")["acl_source_clauses"] == [[MARIO]]
    assert _sees(project, {LEGAL}, "penale") == ["contratto"]
    assert _sees(project, {MARIO}, "penale") == []
    assert _sees(project, {MARIO}, "nota") == ["nota"]


def test_permission_change_without_content_change(cfg, project, acls, tmp_path):
    d = tmp_path / "docs"
    _write(d / "contratto.md", "penale contratto")
    acls["contratto.md"] = [{"groups": ["CORP\\Legal"], "users": []}]
    check_updates(cfg, project, paths=[str(d)], permissions=True)

    acls["contratto.md"] = [{"groups": [], "users": [{"username": "mario"}]}]
    src = check_updates(cfg, project)["sources"][0]          # flag ricordato
    assert src["acl_updated"] == 1 and src["updated"] == 0
    fm = _fm(project, "contratto")
    assert fm["acl_source_clauses"] == [[MARIO]]
    assert fm["content_version"] == 1                        # testo non riscritto
    assert _sees(project, {LEGAL}, "penale") == []           # revoca effettiva
    assert _sees(project, {MARIO}, "penale") == ["contratto"]

    src = check_updates(cfg, project)["sources"][0]
    assert src["acl_updated"] == 0                           # idempotente


def test_deny_and_unmapped_are_fail_closed(cfg, project, acls, tmp_path):
    d = tmp_path / "docs"
    _write(d / "negato.md", "fusione riservata")
    _write(d / "board.md", "verbale board")
    acls["negato.md"] = AclUnreadable("voce 'nega' per CORP\\stagisti")
    acls["board.md"] = [{"groups": ["CORP\\Board"], "users": []}]

    src = check_updates(cfg, project, paths=[str(d)], permissions=True)["sources"][0]
    assert src["acl_unreadable"] == 1
    assert "gruppo:CORP\\Board" in src["acl_unmapped"]
    assert _fm(project, "negato")["acl_source_clauses"] == [[NOBODY]]
    assert _fm(project, "board")["acl_source_clauses"] == [[NOBODY]]
    assert _sees(project, {LEGAL, MARIO, "CORP\\Board"}, "fusione verbale") == []


def test_without_flag_permissions_are_not_read(cfg, project, acls, tmp_path):
    d = tmp_path / "docs"
    _write(d / "libero.md", "testo libero")                  # nessuna ACL simulata
    r = check_updates(cfg, project, paths=[str(d)])
    assert r["totals"]["added"] == 1
    assert _fm(project, "libero")["acl_source_clauses"] == []


def test_data_dir_warning_for_broad_groups(monkeypatch, tmp_path):
    monkeypatch.setattr(ntfs_acl, "os", type("o", (), {"name": "nt"}))
    monkeypatch.setattr(ntfs_acl, "file_read_acl", lambda p: [
        {"groups": ["Authenticated Users", "CORP\\svc"], "users": []}])
    w = ntfs_acl.data_dir_warnings(tmp_path)
    assert len(w) == 1 and "Authenticated Users" in w[0]
    monkeypatch.setattr(ntfs_acl, "file_read_acl", lambda p: [
        {"groups": ["CORP\\svc"], "users": []}])
    assert ntfs_acl.data_dir_warnings(tmp_path) == []
