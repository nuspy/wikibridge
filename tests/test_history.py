"""Storico delle versioni: le versioni superate restano, con il loro testo;
il documento si ricostruisce a una data; lo storico si vede con le ACL
ATTUALI del documento."""
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

from wikibridge.acl_admin import set_page_acl
from wikibridge.projects import ProjectStore
from wikibridge.server import create_app
from wikibridge.wiki.search import SearchIndex
from wikibridge.wiki.writer import WikiWriter

from .ldap_fixture import make_provider

V1 = "# Penali\nPenale del cinque per cento.\n\n# Recesso\nPreavviso di novanta giorni.\n"
V2 = "# Penali\nPenale del dieci per cento.\n\n# Recesso\nPreavviso di novanta giorni.\n"
V3 = "# Penali\nPenale del dieci per cento.\n"


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _write(project, body, **acl):
    w = WikiWriter(project)
    rel_existing = project.wiki_dir / "sources" / "contratto.md"
    p, _ = w.write_source(title="Contratto", markdown=body, doc_id="d:1",
                          import_id="imp-0001",
                          rel_path="sources/contratto.md" if rel_existing.exists() else None,
                          **acl)
    rel = f"sources/{p.name}"
    idx = SearchIndex(project)
    idx.upsert_page(rel, "sources", *w.read_page(rel))
    return idx, rel


def _tick():
    """Un istante sicuramente dopo l'ultima scrittura e prima della prossima."""
    time.sleep(0.02)
    t = _now()
    time.sleep(0.02)
    return t


def test_versions_are_kept_and_document_rebuilt_at_a_date(project):
    idx, rel = _write(project, V1)
    t1 = _tick()
    _write(project, V2)
    t2 = _tick()
    _write(project, V3)                                   # tolta la sezione Recesso

    versions = idx.history(rel)
    penali = [v for v in versions if v["chunk"] == "penali~0.0"]
    assert [v["version"] for v in penali] == [1, 2] and penali[-1]["current"]
    recesso = [v for v in versions if v["chunk"] == "recesso~0.0"]
    assert len(recesso) == 1 and not recesso[0]["current"]

    at1 = idx.document_at(rel, t1)["text"]
    assert "cinque per cento" in at1 and "novanta giorni" in at1
    at2 = idx.document_at(rel, t2)["text"]
    assert "dieci per cento" in at2 and "novanta giorni" in at2
    now = idx.document_at(rel, _now())["text"]
    assert "novanta" not in now
    # la ricerca vede solo la versione corrente
    assert idx.query("cinque") == [] and idx.query("novanta") == []


def test_dates_are_normalised():
    n = SearchIndex.normalize_at
    assert n("2026-09-01") == "2026-09-01T00:00:00.000+00:00"
    assert n("2026-09-01T10:00:00Z") == n("2026-09-01T12:00:00+02:00")
    with pytest.raises(ValueError):
        n("ieri")


def test_history_survives_rebuild_and_page_removal(project):
    idx, rel = _write(project, V1)
    _tick()
    _write(project, V2)
    idx.rebuild()
    assert [v["version"] for v in idx.history(rel) if v["chunk"] == "penali~0.0"] == [1, 2]
    idx.remove(rel)
    hist = idx.history(rel)
    assert hist and not any(v["current"] for v in hist)   # chiuso, non perso


def test_old_versions_expire(project):
    idx, rel = _write(project, V1)
    _tick()
    _write(project, V2)
    conn = sqlite3.connect(project.index_db)
    old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat(timespec="seconds")
    conn.execute("UPDATE chunk_history SET valid_to = ? WHERE valid_to IS NOT NULL", (old,))
    conn.commit()
    conn.close()
    _write(project, V2 + "\n# Nuova\ntesto\n")          # un'altra scrittura: pulizia
    assert [v["version"] for v in idx.history(rel) if v["chunk"] == "penali~0.0"] == [2]


@pytest.fixture
def api(cfg):
    provider, _ = make_provider(cfg)
    store = ProjectStore(cfg)
    store.get("default")
    store.set_acl_enforcement("default", True)
    project = store.get("default")
    client = create_app(cfg, identity_provider=provider).test_client()

    def login(user, pw):
        r = client.post("/identity/login", json={"username": user, "password": pw})
        return {"Authorization": f"Bearer {r.get_json()['token']}"}

    return client, login, project


def test_history_uses_current_permissions(api):
    client, login, project = api
    # la versione 1 era pubblica, poi il documento è stato ristretto
    idx, rel = _write(project, V1, acl_classification="public")
    t1 = _tick()
    set_page_acl(project, rel, ["team-hr"], "internal")
    _write(project, V2)
    luca, anna = login("luca", "luca-pw"), login("anna", "anna-pw")
    q = {"path": rel, "at": t1}
    assert client.get("/history/document", query_string=q, headers=luca).status_code == 404
    r = client.get("/history/document", query_string=q, headers=anna)
    assert r.status_code == 200 and "cinque" in r.get_json()["text"]

    idx.remove(rel)                                       # cancellato: solo admin
    mario = login("mario", "mario-pw")
    assert client.get("/history", query_string={"path": rel}, headers=mario).status_code == 404
    assert client.get("/history", query_string={"path": rel}, headers=anna).status_code == 200
