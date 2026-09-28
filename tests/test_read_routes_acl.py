"""Test di leak: un marcatore presente solo in una source riservata (e in
ciò che ne deriva) non deve comparire in NESSUNA risposta GET per un
utente non autorizzato. Le route vengono prese dall'app, così una route
aggiunta in futuro è coperta automaticamente."""
import json

import pytest

from wikibridge.acl_admin import set_page_acl
from wikibridge.catalog import apply_catalog_result, rebuild_catalog
from wikibridge.projects import ProjectStore
from wikibridge.server import create_app
from wikibridge.wiki.search import SearchIndex
from wikibridge.wiki.writer import WikiWriter

from .ldap_fixture import MARIO, make_provider

MARKER = "zqxsegretoqz"


@pytest.fixture
def app_and_project(cfg):
    provider, _ = make_provider(cfg)
    store = ProjectStore(cfg)
    project = store.get("default")
    store.set_acl_enforcement("default", True)
    project = store.get("default")
    w = WikiWriter(project)
    idx = SearchIndex(project)
    for title, text, acl in [("Contratto riservato", f"clausola {MARKER}", ["team-hr"]),
                             ("Comunicato", "notizia pubblica", None)]:
        p, _ = w.write_source(title=title, markdown=text, import_id="imp-0001",
                              acl_roles=acl,
                              acl_classification=None if acl else "public")
        fm, body = w.read_page(f"sources/{p.name}")
        idx.upsert_page(f"sources/{p.name}", "sources", fm, body)
    apply_catalog_result(project, "sources/contratto-riservato.md",
                         {"entities": [{"name": "ACME", "summary": f"penale {MARKER}"}],
                          "relations": [{"from": "ACME", "to": "Beta", "kind": MARKER}]})
    apply_catalog_result(project, "sources/comunicato.md",
                         {"entities": [{"name": "ACME", "summary": "partner"},
                                       {"name": "Beta", "summary": "fornitore"}]})
    # testo legacy (non attribuibile) in un'entità
    w.write_page("entities", "legacy", {"title": "Legacy", "mentions": ["sources/comunicato.md"]},
                 f"# Legacy\n\nvecchio {MARKER}\n\n## Menzioni\n\n- [[sources/comunicato.md]]")
    idx.upsert_page("entities/legacy.md", "entities",
                    *w.read_page("entities/legacy.md"))
    w.write_overview(f"panoramica {MARKER}")
    w.append_log(f"import {MARKER}")
    w.rebuild_index()
    return create_app(cfg, identity_provider=provider), project


def _login(client, user, pw):
    tok = client.post("/identity/login",
                      json={"username": user, "password": pw}).get_json()["token"]
    return {"Authorization": f"Bearer {tok}"}


def _all_pages(project):
    pages = ["index.md", "overview.md", "log.md"]
    for section in ("sources", "entities", "concepts", "meetings", "syntheses"):
        pages += [f"{section}/{p.name}" for p in (project.wiki_dir / section).glob("*.md")]
    return pages


def test_no_get_route_leaks_restricted_content(app_and_project):
    app, project = app_and_project
    client = app.test_client()
    h = _login(client, "mario", "mario-pw")            # team-legal, non team-hr
    responses = []
    for rule in app.url_map.iter_rules():
        if "GET" not in rule.methods or rule.arguments or rule.rule.startswith("/static"):
            continue
        responses.append(client.get(rule.rule, query_string={"q": MARKER}, headers=h))
    for page in _all_pages(project):
        responses.append(client.get("/wiki/page", query_string={"path": page}, headers=h))
    for r in responses:
        assert r.status_code < 500, r.request.path
        assert MARKER not in r.get_data(as_text=True), r.request.path


def test_restricted_user_sees_public_part(app_and_project):
    app, _ = app_and_project
    client = app.test_client()
    h = _login(client, "mario", "mario-pw")
    tree = client.get("/wiki/tree", headers=h).get_json()["tree"]
    assert [e["file"] for e in tree["sources"]] == ["sources/comunicato.md"]
    page = client.get("/wiki/page", query_string={"path": "entities/acme.md"},
                      headers=h).get_json()
    assert "partner" in page["body"] and page["frontmatter"]["mentions"] == \
        ["sources/comunicato.md"]
    assert client.get("/wiki/page", query_string={"path": "overview.md"},
                      headers=h).status_code == 404
    hits = client.get("/search", query_string={"q": "partner"}, headers=h).get_json()["hits"]
    assert [x["path"] for x in hits] == ["entities/acme.md"]


def test_personal_grant_opens_the_page(app_and_project):
    app, project = app_and_project
    set_page_acl(project, "sources/contratto-riservato.md", ["team-hr", MARIO])
    client = app.test_client()
    h = _login(client, "mario", "mario-pw")
    r = client.get("/wiki/page", query_string={"path": "sources/contratto-riservato.md"},
                   headers=h)
    assert r.status_code == 200 and MARKER in r.get_json()["body"]


def test_admin_sees_everything(app_and_project):
    app, _ = app_and_project
    client = app.test_client()
    h = _login(client, "anna", "anna-pw")
    body = json.dumps(client.get("/search", query_string={"q": MARKER},
                                 headers=h).get_json())
    assert MARKER in body
    assert client.get("/wiki/page", query_string={"path": "overview.md"},
                      headers=h).status_code == 200


def test_gate_hides_the_project(app_and_project):
    app, _ = app_and_project
    client = app.test_client()
    admin = _login(client, "anna", "anna-pw")
    client.put("/acl/gate", json={"principals": ["team-hr"]}, headers=admin)
    h = _login(client, "mario", "mario-pw")
    assert client.get("/projects", headers=h).get_json() == []
    assert client.get("/wiki/tree", headers=h).status_code == 404


def test_catalog_rebuild_clears_legacy_text(app_and_project):
    _, project = app_and_project

    def fake_hook(proj, rel):
        apply_catalog_result(proj, rel, {"entities": [{"name": "ACME",
                                                       "summary": f"da {rel}"}]})

    out = rebuild_catalog(None, project, hook=fake_hook)
    assert out["sources"] == 2
    assert not (project.wiki_dir / "entities" / "legacy.md").exists()
    text = (project.wiki_dir / "entities" / "acme.md").read_text(encoding="utf-8")
    assert MARKER not in text and "da sources/comunicato.md" in text
