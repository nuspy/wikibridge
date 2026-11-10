"""Contesto RAG per l'inferenza: filtrato per chi lo chiede e, nelle
riunioni, per l'intersezione con i partecipanti."""
import pytest

from wikibridge.projects import ProjectStore
from wikibridge.server import create_app
from wikibridge.wiki.search import SearchIndex
from wikibridge.wiki.writer import WikiWriter

from .ldap_fixture import make_provider

SECRET = "zqxbudgetqz"


@pytest.fixture
def client(cfg):
    provider, _ = make_provider(cfg)
    store = ProjectStore(cfg)
    store.get("default")
    store.set_acl_enforcement("default", True)
    project = store.get("default")
    w, idx = WikiWriter(project), SearchIndex(project)
    for title, text, roles, cls in [
        ("Budget legale", f"budget contenzioso {SECRET} fornitore", ["team-legal"], None),
        ("Listino", "budget pubblico listino fornitore", None, "public"),
        ("Paghe", "budget stipendi fornitore", ["team-hr"], None),
    ]:
        p, _ = w.write_source(title=title, markdown=text, import_id="imp-0001",
                              acl_roles=roles, acl_classification=cls)
        rel = f"sources/{p.name}"
        idx.upsert_page(rel, "sources", *w.read_page(rel))
    return create_app(cfg, identity_provider=provider).test_client()


def _login(client, user, pw):
    tok = client.post("/identity/login",
                      json={"username": user, "password": pw}).get_json()["token"]
    return {"Authorization": f"Bearer {tok}"}


def _titles(r):
    return sorted(b["title"] for b in r.get_json()["blocks"])


def test_context_is_filtered_for_the_requester(client):
    h = _login(client, "mario", "mario-pw")               # team-legal
    r = client.post("/rag/context", json={"query": "budget fornitore"}, headers=h)
    assert r.status_code == 200 and r.get_json()["policy"] == "utente"
    assert _titles(r) == ["Budget legale", "Listino"]
    assert SECRET in r.get_data(as_text=True)             # testo completo, non snippet


def test_long_transcript_query_uses_any_terms(client):
    h = _login(client, "anna", "anna-pw")
    transcript = ("allora oggi parliamo del contenzioso con il fornitore, poi "
                  "vediamo se il budget regge e chi segue la pratica") * 3
    r = client.post("/rag/context", json={"query": transcript}, headers=h)
    assert "Budget legale" in _titles(r)


def test_meeting_context_is_the_intersection(client):
    admin = _login(client, "anna", "anna-pw")
    r = client.post("/rag/context", headers=admin,
                    json={"query": "budget", "participants": ["mario.rossi@example.com"]})
    body = r.get_json()
    assert body["participants"] == {"resolved": ["mario.rossi@example.com"],
                                    "unresolved": []}
    assert _titles(r) == ["Budget legale", "Listino"]      # l'admin scende a mario

    r = client.post("/rag/context", headers=admin,
                    json={"query": "budget", "participants": ["mario", "ospite esterno"]})
    assert r.get_json()["participants"]["unresolved"] == ["ospite esterno"]
    assert _titles(r) == ["Listino"]                         # esterno: solo public
    assert SECRET not in r.get_data(as_text=True)

    # risultato che vedranno solo i riconosciuti (pagina della riunione)
    r = client.post("/rag/context", headers=admin,
                    json={"query": "budget", "participants": ["mario", "ospite esterno"],
                          "unresolved": "ignore"})
    assert _titles(r) == ["Budget legale", "Listino"]


def test_meeting_pages_are_visible_to_saver_and_participants(client):
    mario = _login(client, "mario", "mario-pw")
    r = client.post("/meetings", headers=mario,
                    json={"title": "Riunione fornitori", "date": "2026-09-28",
                          "participants": ["luca", "ospite esterno"],
                          "summary": "accordo quadro fornitori"})
    assert r.status_code == 201
    rel = r.get_json()["path"]
    for user, pw, visible in [("mario", "mario-pw", True), ("luca", "luca-pw", True)]:
        h = _login(client, user, pw)
        got = client.get("/wiki/page", query_string={"path": rel}, headers=h)
        assert (got.status_code == 200) is visible, user
    client.post("/meetings", headers=mario,
                json={"title": "Solo io", "date": "2026-09-28", "summary": "privato"})
    luca = _login(client, "luca", "luca-pw")
    tree = client.get("/wiki/tree", headers=luca).get_json()["tree"]
    assert [e["title"] for e in tree["meetings"]] == ["Riunione fornitori"]
