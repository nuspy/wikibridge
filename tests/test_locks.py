"""Lock tra processi: nessuna scrittura persa quando server, CLI e agenti
lavorano insieme sugli stessi file."""
import os
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest

from wikibridge.config import Config
from wikibridge.filelock import LockTimeout, file_lock
from wikibridge.projects import ProjectStore


def _cfg(data: str) -> Config:
    c = Config()
    c.data_dir, c.secrets_dir = data + "/data", data + "/secrets"
    c.brain_mode = "none"
    return c


def _writer_imports(data: str, tag: str, n: int) -> None:
    from wikibridge.importer import ImportsLog
    log = ImportsLog(ProjectStore(_cfg(data)).get("default"))
    for i in range(n):
        log.upsert({"id": f"{tag}-{i}", "url": "x", "page_index": {}})


def _writer_audit(data: str, n: int) -> None:
    from wikibridge.audit import AuditLog
    a = AuditLog(_cfg(data))
    for i in range(n):
        a.write("test", user=f"p{os.getpid()}", n=i)


def test_lock_is_exclusive_and_released(tmp_path):
    p = tmp_path / "x.lock"
    with file_lock(p):
        with pytest.raises(LockTimeout):
            with file_lock(p, timeout=0.2):
                pass
    assert not p.exists()


def test_abandoned_lock_is_taken_over(tmp_path):
    p = tmp_path / "x.lock"
    p.write_text("processo morto")
    old = time.time() - 600
    os.utime(p, (old, old))
    with file_lock(p, timeout=0.5, stale_after=120):
        assert "pid" in p.read_text()


def test_lock_in_pending_delete_is_retried_not_an_error(tmp_path, monkeypatch):
    """Windows: un lock in cancellazione dà PermissionError per un istante.
    Deve valere "occupato, riprova", non far fallire la scrittura."""
    import wikibridge.filelock as fl
    real_open, real_unlink = os.open, fl.Path.unlink
    calls = {"open": 0, "unlink": 0}

    def flaky_open(path, flags, *a):
        calls["open"] += 1
        if calls["open"] <= 3:
            raise PermissionError(13, "Permission denied")
        return real_open(path, flags, *a)

    def flaky_unlink(self, *a, **k):
        calls["unlink"] += 1
        if calls["unlink"] <= 2:
            raise PermissionError(13, "Permission denied")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(fl.os, "open", flaky_open)
    monkeypatch.setattr(fl.Path, "unlink", flaky_unlink)
    p = tmp_path / "x.lock"
    with fl.file_lock(p, timeout=2, poll=0.01):
        assert p.exists()
    assert not p.exists()                                   # rilasciato davvero
    assert calls["open"] == 4 and calls["unlink"] == 3


def test_concurrent_processes_do_not_lose_records(tmp_path):
    data = str(tmp_path)
    ProjectStore(_cfg(data)).get("default")
    with ProcessPoolExecutor(max_workers=3) as ex:
        list(ex.map(_writer_imports, [data] * 3, ["a", "b", "c"], [15] * 3))
    from wikibridge.importer import ImportsLog
    ids = {r["id"] for r in ImportsLog(ProjectStore(_cfg(data)).get("default")).load()}
    assert len(ids) == 45                                   # nessun record perso


def test_concurrent_audit_writers_keep_the_chain(tmp_path):
    data = str(tmp_path)
    with ProcessPoolExecutor(max_workers=3) as ex:
        list(ex.map(_writer_audit, [data] * 3, [20] * 3))
    from wikibridge.audit import AuditLog
    result = AuditLog(_cfg(data)).verify()
    assert result == {"ok": True, "records": 60}


def test_same_source_update_is_busy(cfg, project, tmp_path):
    from wikibridge.updater import check_updates
    d = tmp_path / "docs"
    d.mkdir()
    (d / "a.md").write_text("uno", encoding="utf-8")
    check_updates(cfg, project, paths=[str(d)])
    with file_lock(project.root / "locks" / "ing-0001.lock"):
        r = check_updates(cfg, project)["sources"][0]
    assert r["state"] == "busy" and "occupata" in r["errors"][0]
    assert check_updates(cfg, project)["sources"][0]["state"] == "ok"
