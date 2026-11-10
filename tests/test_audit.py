"""Registro di audit: chi ha visto cosa, privacy delle query, integrità,
conferma sulle modifiche che allargano l'accesso."""
import json
from pathlib import Path

import pytest

from wikibridge.projects import ProjectStore
from wikibridge.server import create_app
from wikibridge.wiki.search import SearchIndex
from wikibridge.wiki.writer import WikiWriter

from .ldap_fixture import make_provider

SECRET_Q = "fusione segreta aurora"


@pytest.fixture
def env(cfg):
    provider, _ = make_provider(cfg)
    store = ProjectStore(cfg)
    store.get("default")
    store.set_acl_enforcement("default", True)
    project = store.get("default")
    w, idx = WikiWriter(project), SearchIndex(project)
    for title, text, roles in [("Aurora", "fusione aurora", ["team-hr"]),
                               ("Listino", "listino aurora", ["team-legal"])]:
        p, _ = w.write_source(title=title, markdown=text, import_id="imp-0001",
                              acl_roles=roles)
        idx.upsert_page(f"sources/{p.name}", "sources", *w.read_page(f"sources/{p.name}"))
    client = create_app(cfg, identity_provider=provider).test_client()

    def login(user, pw):
        r = client.post("/identity/login", json={"username": user, "password": pw})
        return {"Authorization": f"Bearer {r.get_json().get('token', '')}"}

    return client, login, Path(cfg.data_dir) / "audit"


def _events(audit_dir):
    return [json.loads(line) for f in sorted(audit_dir.glob("*.jsonl"))
            for line in f.read_text(encoding="utf-8").splitlines()]


def test_logins_recorded_without_passwords(env):
    client, login, audit_dir = env
    login("mario", "password-sbagliata")
    login("mario", "mario-pw")
    ev = _events(audit_dir)
    assert [e["action"] for e in ev] == ["login_failed", "login"]
    assert ev[1]["user"].startswith("cn=Mario Rossi")
    text = "".join(f.read_text(encoding="utf-8") for f in audit_dir.glob("*.jsonl"))
    assert "mario-pw" not in text and "password-sbagliata" not in text


def test_who_saw_what_and_query_privacy(env):
    client, login, audit_dir = env
    mario = login("mario", "mario-pw")
    client.get("/search", query_string={"q": SECRET_Q}, headers=mario)
    client.get("/search", query_string={"q": SECRET_Q}, headers=mario)
    client.get("/wiki/page", query_string={"path": "sources/aurora.md"}, headers=mario)
    client.get("/wiki/page", query_string={"path": "sources/listino.md"}, headers=mario)
    ev = [e for e in _events(audit_dir) if e["action"] in ("search", "page")]
    s1, s2 = ev[0], ev[1]
    assert "query" not in s1 and s1["query_hmac"] == s2["query_hmac"]
    assert SECRET_Q not in json.dumps(ev)
    assert [(e["path"], e["outcome"]) for e in ev[2:]] == [
        ("sources/aurora.md", "denied"), ("sources/listino.md", "allowed")]

    admin = login("anna", "anna-pw")
    seen = client.get("/audit", query_string={"path": "sources/listino.md"},
                      headers=admin).get_json()["events"]
    assert {e["user"].split(",")[0] for e in seen} == {"cn=Mario Rossi"}
    assert client.get("/audit", headers=mario).status_code == 403


def test_tampering_is_detected(env):
    client, login, audit_dir = env
    admin = login("anna", "anna-pw")
    for _ in range(3):
        client.get("/search", query_string={"q": "aurora"}, headers=admin)
    assert client.get("/audit/verify", headers=admin).get_json()["ok"]
    f = next(audit_dir.glob("*.jsonl"))
    lines = f.read_text(encoding="utf-8").splitlines()
    lines[1] = lines[1].replace('"search"', '"logout"')        # riga alterata
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    r = client.get("/audit/verify", headers=admin).get_json()
    assert not r["ok"] and r["line"] == 3        # la catena si rompe dopo la riga 2

    lines[1] = lines[1].replace('"logout"', '"search"')        # ripristina
    lines[-1] = lines[-1].replace('"search"', '"logout"')      # altera l'ultima
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    r = client.get("/audit/verify", headers=admin).get_json()
    assert not r["ok"] and "ultima riga" in r["error"]


def test_widening_requires_confirmation(env):
    client, login, audit_dir = env
    admin = login("anna", "anna-pw")
    body = {"principals": ["dipendenti"], "sources": ["imp-0001"]}
    r = client.put("/acl/collections/tutto", json={**body, "dry_run": True}, headers=admin)
    prev = r.get_json()["preview"]
    assert prev["widening"] and prev["documents_affected"] == 2
    r = client.put("/acl/collections/tutto", json=body, headers=admin)
    assert r.status_code == 409 and r.get_json()["preview"]["added_principals"] == ["dipendenti"]
    r = client.put("/acl/collections/tutto", json={**body, "confirm": True}, headers=admin)
    assert r.status_code == 200
    # restringere non chiede conferma; eliminare (toglie una restrizione) sì
    r = client.put("/acl/collections/tutto", json={"principals": [], "sources": ["imp-0001"]},
                   headers=admin)
    assert r.status_code == 200
    assert client.delete("/acl/collections/tutto", headers=admin).status_code == 409
    assert client.delete("/acl/collections/tutto", json={"confirm": True},
                         headers=admin).status_code == 200
    actions = [e["action"] for e in _events(audit_dir)]
    assert actions.count("acl_collection") == 2 and "acl_collection_delete" in actions


def test_other_writes_get_a_generic_event(env):
    client, login, audit_dir = env
    admin = login("anna", "anna-pw")
    client.post("/projects", json={"name": "Nuovo"}, headers=admin)
    ev = [e for e in _events(audit_dir) if e["action"] == "api"]
    assert ev[-1]["path"] == "/projects" and ev[-1]["status"] == 201


def test_two_writers_keep_a_valid_chain(cfg, env):
    """Server e CLI scrivono lo stesso registro: niente falsi allarmi."""
    from wikibridge.audit import AuditLog
    client, login, _ = env
    admin = login("anna", "anna-pw")
    cli = AuditLog(cfg)
    for i in range(3):
        client.get("/search", query_string={"q": "aurora"}, headers=admin)
        cli.write("acl_page", user="local-cli", n=i)
    assert client.get("/audit/verify", headers=admin).get_json()["ok"]
    assert cli.verify()["ok"]


def test_old_files_are_purged(cfg, env):
    client, login, audit_dir = env
    audit_dir.mkdir(parents=True, exist_ok=True)
    old = audit_dir / "audit-2020-01.jsonl"
    old.write_text('{"action": "vecchio"}\n', encoding="utf-8")
    login("mario", "mario-pw")
    assert not old.exists()
