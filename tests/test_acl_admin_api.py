"""Gestione ACL via API (quella che usa la GUI): anteprima, conferma sulle
modifiche che allargano, nomi leggibili delle persone, ruoli proposti."""
import pytest

from wikibridge.projects import ProjectStore
from wikibridge.server import create_app
from wikibridge.wiki.search import SearchIndex
from wikibridge.wiki.writer import WikiWriter

from .ldap_fixture import MARIO, make_provider


@pytest.fixture
def env(cfg):
    provider, _ = make_provider(cfg)
    cfg.source_group_map = {"conf-board": "board"}
    project = ProjectStore(cfg).get("default")
    w, idx = WikiWriter(project), SearchIndex(project)
    p, _ = w.write_source(title="Contratto", markdown="penale", import_id="imp-0001")
    idx.upsert_page(f"sources/{p.name}", "sources", *w.read_page(f"sources/{p.name}"))
    client = create_app(cfg, identity_provider=provider).test_client()
    tok = client.post("/identity/login", json={"username": "anna", "password": "anna-pw"}
                      ).get_json()["token"]
    return client, {"Authorization": f"Bearer {tok}"}, f"sources/{p.name}"


def test_show_offers_known_roles_and_levels(env):
    client, h, _ = env
    r = client.get("/acl", headers=h).get_json()
    # dalle mappe dei gruppi (identity_role_map e source_group_map), senza admin
    assert r["known_roles"] == ["board", "team-legal"]
    assert r["levels"] == ["public", "internal", "confidential", "restricted"]
    assert r["labels"] == {}


def test_page_acl_preview_confirm_and_labels(env):
    client, h, path = env
    body = {"path": path, "principals": ["team-legal"], "users": ["mario"]}
    r = client.put("/acl/page", json={**body, "dry_run": True}, headers=h)
    assert r.status_code == 200
    assert r.get_json()["preview"]["added_principals"] == sorted(["team-legal", MARIO])
    r = client.put("/acl/page", json=body, headers=h)
    assert r.status_code == 409                               # allarga: serve conferma
    # l'anteprima nomina già la persona, prima che sia salvata
    assert r.get_json()["preview"]["labels"] == {MARIO: "Mario Rossi (mario)"}
    r = client.put("/acl/page", json={**body, "confirm": True}, headers=h)
    assert r.status_code == 200
    shown = client.get(f"/acl?path={path}", headers=h).get_json()
    assert shown["page"]["acl_roles"] == sorted(["team-legal", MARIO])
    assert shown["labels"] == {MARIO: "Mario Rossi (mario)"}   # persona leggibile
    # restringere (togliere una persona) non chiede conferma
    r = client.put("/acl/page", json={"path": path, "principals": ["team-legal"]}, headers=h)
    assert r.status_code == 200
    # alzare la classificazione restringe; abbassarla allarga
    assert client.put("/acl/page", json={"path": path, "principals": ["team-legal"],
                                         "classification": "restricted"},
                      headers=h).status_code == 200
    assert client.put("/acl/page", json={"path": path, "principals": ["team-legal"],
                                         "classification": "public"},
                      headers=h).status_code == 409
    # togliere tutti i principal propri lascia decidere alla collezione: allarga
    assert client.put("/acl/page", json={"path": path, "principals": []},
                      headers=h).status_code == 409


def test_disabling_enforcement_needs_confirmation_and_is_audited(env, cfg):
    client, h, _ = env
    assert client.post("/projects/default/acl", json={"enabled": True},
                       headers=h).status_code == 200           # restringere: subito
    r = client.post("/projects/default/acl", json={"enabled": False}, headers=h)
    assert r.status_code == 409 and r.get_json()["preview"]["widening"]
    assert r.get_json()["preview"]["documents_affected"] == 1
    r = client.post("/projects/default/acl", json={"enabled": False, "confirm": True},
                    headers=h)
    assert r.status_code == 200 and r.get_json()["acl_enforcement"] is False
    events = client.get("/audit?action=acl_enforcement", headers=h).get_json()
    assert len(events["events"]) == 2


def test_gate_and_collection_dry_run(env):
    client, h, _ = env
    r = client.put("/acl/gate", json={"principals": ["dipendenti"], "dry_run": True},
                   headers=h).get_json()
    assert r["preview"]["added_principals"] == ["dipendenti"]
    assert client.get("/acl", headers=h).get_json()["gate"] == []     # nulla salvato
    r = client.put("/acl/collections/legale", headers=h, json={
        "principals": ["team-legal"], "users": ["luca"], "sources": ["imp-0001"],
        "classification": "confidential", "confirm": True})
    assert r.status_code == 200
    shown = client.get("/acl", headers=h).get_json()
    assert shown["collections"]["legale"]["sources"] == ["imp-0001"]
    assert "Luca Bianchi (luca)" in shown["labels"].values()
    assert "team-legal" in shown["known_roles"]


def test_unknown_page_is_a_clean_error(env):
    client, h, _ = env
    assert client.get("/acl?path=sources/nessuno.md", headers=h).status_code == 404
    r = client.put("/acl/page", json={"path": "sources/nessuno.md", "principals": ["x"]},
                   headers=h)
    assert r.status_code == 400
