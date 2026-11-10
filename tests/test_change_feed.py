"""Change feed: meno chiamate alla sorgente, senza perdere modifiche e senza
cancellare pagine per colpa di un indice di ricerca in ritardo."""
from datetime import datetime, timedelta, timezone

import pytest

from wikibridge.adapters.mediawiki import MediaWikiAdapter
from wikibridge.importer import ImportsLog, run_import
from wikibridge.updater import REMOVE_AFTER, check_updates

from .confluence_fixture import FakeConfluence
from .conftest import source_pages

N = 30


@pytest.fixture
def conf():
    c = FakeConfluence()
    c.put("1", "Radice", "radice")
    for i in range(2, N + 2):
        c.put(str(i), f"Pagina {i}", f"testo {i}", parent="1")
    yield c
    c.stop()


@pytest.fixture
def imported(cfg, project, conf):
    rec = run_import(cfg, project, conf.url("1"), scope="subtree", depth=0)
    assert rec["state"] == "done" and rec["pages"] == N + 1, rec["errors"]
    return rec


def test_subtree_listing_uses_one_search_not_one_call_per_page(imported, conf):
    assert conf.count(r"/child/page") == 0
    assert conf.count(r"/content/search") == 1


def test_update_checks_only_what_changed(cfg, project, imported, conf):
    for p in conf.pages.values():                   # pagine vecchie di 3 giorni
        p["modified"] -= timedelta(days=3)
    conf.put("7", "Pagina 7", "testo 7 modificato", parent="1")
    conf.calls.clear()
    r = check_updates(cfg, project)["sources"][0]
    assert r["feed"] is True
    assert (r["updated"], r["unchanged"]) == (1, N)
    # un solo controllo di revisione (la pagina del feed) invece di 31
    assert conf.count(r"\?version$") == 1
    assert conf.count(r"/content/search") == 2      # elenco del ramo + feed


def test_feed_margin_never_hides_a_change(cfg, project, imported, conf):
    """Modifica avvenuta 'prima' dell'ultimo controllo secondo l'orologio di
    Confluence (fusi orari diversi): il margine di 24 ore la include."""
    log = ImportsLog(project)
    rec = log.get(imported["id"])
    rec["last_sync"] = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
    log.upsert(rec)
    conf.put("9", "Pagina 9", "testo 9 cambiato", parent="1")
    assert check_updates(cfg, project)["sources"][0]["updated"] == 1


def test_search_index_lag_does_not_delete_pages(cfg, project, imported, conf):
    conf.unindexed.add("5")                         # non compare nella ricerca
    for _ in range(REMOVE_AFTER + 1):
        r = check_updates(cfg, project)["sources"][0]
    assert r["missing"] == 0 and r["removed"] == 0
    assert len(source_pages(project)) == N + 1


def test_page_moved_out_of_branch_is_removed(cfg, project, imported, conf):
    conf.put("99", "Altrove", "fuori dal ramo")
    conf.pages["5"]["parent"] = "99"                # spostata fuori dal ramo
    for _ in range(REMOVE_AFTER):
        r = check_updates(cfg, project)["sources"][0]
    assert r["removed"] == 1
    assert len(source_pages(project)) == N


class _FakeCall:
    """_call di MediaWiki simulata: RecentChanges in due pagine."""

    def __init__(self):
        self.args = []

    def __call__(self, **params):
        self.args.append(params)
        if "rccontinue" not in params:
            return {"query": {"recentchanges": [{"title": "A"}, {"title": "B"}]},
                    "continue": {"rccontinue": "x", "continue": "-||"}}
        return {"query": {"recentchanges": [{"title": "C"}]}}


def test_mediawiki_recentchanges_feed(monkeypatch):
    ad = MediaWikiAdapter(session=None, limits=None)
    ad._api = "https://wiki.example/w/api.php"
    fake = _FakeCall()
    monkeypatch.setattr(ad, "_call", fake)
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    assert ad.changed_ids_since("https://wiki.example/wiki/X", "space", since) == {"A", "B", "C"}
    assert fake.args[0]["rcend"].endswith("Z")
    old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    assert ad.changed_ids_since("https://wiki.example/wiki/X", "space", old) is None

    def boom(**p):
        raise RuntimeError("api giù")

    monkeypatch.setattr(ad, "_call", boom)
    assert ad.changed_ids_since("https://wiki.example/wiki/X", "space", since) is None
