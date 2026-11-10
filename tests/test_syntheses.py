"""Sintesi: visibili solo a chi vede TUTTE le fonti da cui derivano,
valutate per riferimento (seguono i cambi di permesso delle fonti)."""
import pytest

from wikibridge.acl_admin import set_page_acl
from wikibridge.projects import ProjectStore
from wikibridge.server import create_app
from wikibridge.wiki.search import SearchIndex
from wikibridge.wiki.writer import WikiWriter

from .ldap_fixture import make_provider

MARK = "zqxsintesiqz"


@pytest.fixture
def env(cfg):
    provider, _ = make_provider(cfg)
    store = ProjectStore(cfg)
    store.get("default")
    store.set_acl_enforcement("default", True)
    project = store.get("default")
    w, idx = WikiWriter(project), SearchIndex(project)
    for title, text, roles, cls in [("Legale", "parere legale", ["team-legal"], None),
                                    ("Pubblico", "comunicato", None, "public"),
                                    ("Paghe", "stipendi", ["team-hr"], None)]:
        p, _ = w.write_source(title=title, markdown=text, import_id="imp-0001",
                              acl_roles=roles, acl_classification=cls)
        idx.upsert_page(f"sources/{p.name}", "sources", *w.read_page(f"sources/{p.name}"))
    client = create_app(cfg, identity_provider=provider).test_client()

    def login(user, pw):
        r = client.post("/identity/login", json={"username": user, "password": pw})
        return {"Authorization": f"Bearer {r.get_json()['token']}"}

    return client, login, project


def _save(client, h, sources, title="Sintesi"):
    return client.post("/syntheses", headers=h, json={
        "title": title, "body": f"riassunto {MARK}", "sources": sources})


def _sees(client, h, rel):
    return client.get("/wiki/page", query_string={"path": rel}, headers=h).status_code == 200


def test_visible_only_to_who_sees_all_sources(env):
    client, login, project = env
    mario, anna = login("mario", "mario-pw"), login("anna", "anna-pw")   # legal, admin
    r = _save(client, mario, ["sources/legale.md", "sources/pubblico.md"])
    assert r.status_code == 201
    rel = r.get_json()["path"]
    luca = login("luca", "luca-pw")                                      # nessun ruolo
    assert _sees(client, mario, rel) and _sees(client, anna, rel)
    assert not _sees(client, luca, rel)
    hits = client.get("/search", query_string={"q": MARK}, headers=luca).get_json()["hits"]
    assert hits == []


def test_follows_permission_changes_of_its_sources(env):
    client, login, project = env
    mario, luca = login("mario", "mario-pw"), login("luca", "luca-pw")
    rel = _save(client, mario, ["sources/legale.md"]).get_json()["path"]
    assert not _sees(client, luca, rel)
    from .ldap_fixture import LUCA
    set_page_acl(project, "sources/legale.md", ["team-legal", LUCA])     # fonte allargata
    assert _sees(client, luca, rel)                                       # subito
    SearchIndex(project).remove("sources/legale.md")                     # fonte sparita
    assert not _sees(client, mario, rel)


def test_author_must_see_every_declared_source(env):
    client, login, _ = env
    mario = login("mario", "mario-pw")
    r = _save(client, mario, ["sources/legale.md", "sources/paghe.md"])  # paghe: team-hr
    assert r.status_code == 400 and "paghe" not in r.get_data(as_text=True)


def test_sources_must_be_documents(env):
    client, login, project = env
    anna = login("anna", "anna-pw")
    rel = _save(client, anna, ["sources/pubblico.md"], "Prima").get_json()["path"]
    assert _save(client, anna, [rel], "Seconda").status_code == 400      # da una sintesi
    assert _save(client, anna, ["sources/inesistente.md"]).status_code == 400
    assert _save(client, anna, []).status_code == 400
