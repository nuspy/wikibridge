"""Fase C: gruppi annidati, revoche e concessioni senza aspettare il TTL,
account disabilitati, directory irraggiungibile, mappa ruoli verificata."""
import pytest

from wikibridge.identity import IdentityError
from wikibridge.server import create_app

from .ldap_fixture import (CONTRACTS, LEGAL, LUCA_DN, MARIO_DN, bump_usn,
                           make_provider, set_attr)


def test_nested_groups_are_resolved(cfg):
    provider, seed = make_provider(cfg)
    set_attr(seed, LUCA_DN, "memberOf", [CONTRACTS])      # contracts ⊂ Legal
    assert "team-legal" in provider.resolve_roles(provider.find_user("luca"))


def test_nested_groups_off_and_cycles(cfg):
    cfg.ldap_nested_groups = "off"
    provider, seed = make_provider(cfg)
    set_attr(seed, LUCA_DN, "memberOf", [CONTRACTS])
    assert "team-legal" not in provider.resolve_roles(provider.find_user("luca"))

    cfg.ldap_nested_groups = "memberof"
    provider, seed = make_provider(cfg)
    set_attr(seed, LEGAL, "memberOf", [CONTRACTS])        # ciclo Legal <-> contracts
    set_attr(seed, LUCA_DN, "memberOf", [CONTRACTS])
    assert "team-legal" in provider.resolve_roles(provider.find_user("luca"))


def test_change_detection(cfg):
    provider, seed = make_provider(cfg)
    assert provider.groups_changed() is False                # punto di partenza
    assert provider.groups_changed() is False
    bump_usn(seed, LEGAL, 900)
    assert provider.groups_changed() is True
    assert provider.groups_changed() is False


@pytest.fixture
def env(cfg):
    provider, seed = make_provider(cfg)
    client = create_app(cfg, identity_provider=provider).test_client()

    def login(user, pw):
        r = client.post("/identity/login", json={"username": user, "password": pw})
        return {"Authorization": f"Bearer {r.get_json().get('token', '')}"}

    return client, seed, provider, login


def _roles(client, h):
    return client.get("/identity/whoami", headers=h).get_json().get("roles", [])


def test_revocation_applies_at_sync_not_after_ttl(env):
    client, seed, _, login = env
    mario, admin = login("mario", "mario-pw"), login("anna", "anna-pw")
    assert "team-legal" in _roles(client, mario)
    set_attr(seed, MARIO_DN, "memberOf", [])                 # tolto dal gruppo
    bump_usn(seed, LEGAL, 900)
    assert "team-legal" in _roles(client, mario)             # ancora in cache
    r = client.post("/identity/sync-groups", headers=admin).get_json()
    assert r["changed"] is True
    assert "team-legal" not in _roles(client, mario)         # subito


def test_disabled_account_loses_session(env):
    client, seed, _, login = env
    mario, admin = login("mario", "mario-pw"), login("anna", "anna-pw")
    set_attr(seed, MARIO_DN, "userAccountControl", ["514"])  # disabilitato
    bump_usn(seed, MARIO_DN, 950)
    client.post("/identity/sync-groups", headers=admin)
    r = client.get("/identity/whoami", headers=mario)
    assert r.status_code == 401 and "revocata" in r.get_json()["error"]
    assert login("mario", "mario-pw")["Authorization"] == "Bearer "   # niente login


def test_directory_outage_is_503_without_logout(env, monkeypatch):
    client, _, provider, login = env
    mario, admin = login("mario", "mario-pw"), login("anna", "anna-pw")
    client.post("/identity/invalidate", json={}, headers=admin)  # svuota la cache

    def down(*a, **k):
        raise IdentityError("directory giù")

    monkeypatch.setattr(provider, "_service_search", down)
    assert client.get("/identity/whoami", headers=mario).status_code == 503
    monkeypatch.undo()
    assert client.get("/identity/whoami", headers=mario).status_code == 200


def test_admin_invalidate_user_and_revoke_sessions(env):
    client, _, _, login = env
    luca, admin = login("luca", "luca-pw"), login("anna", "anna-pw")
    assert client.get("/identity/whoami", headers=luca).status_code == 200
    r = client.post("/identity/invalidate", headers=admin,
                    json={"username": "luca", "revoke_sessions": True}).get_json()
    assert r["sessions_revoked"] == 1
    assert client.get("/identity/whoami", headers=luca).status_code == 401
    assert client.post("/identity/invalidate", headers=luca,
                       json={}).status_code == 401


def test_optional_background_poll(cfg):
    import time
    cfg.ldap_change_poll_s = 1
    provider, seed = make_provider(cfg)
    app = create_app(cfg, identity_provider=provider)
    client = app.test_client()
    tok = client.post("/identity/login",
                      json={"username": "mario", "password": "mario-pw"}).get_json()["token"]
    h = {"Authorization": f"Bearer {tok}"}
    set_attr(seed, MARIO_DN, "memberOf", [])
    bump_usn(seed, LEGAL, 900)
    try:
        for _ in range(40):
            if "team-legal" not in _roles(client, h):
                break
            time.sleep(0.1)
        assert "team-legal" not in _roles(client, h)
    finally:
        app.config["wb_poll_stop"].set()


def test_status_reports_unknown_role_map_groups(cfg):
    cfg_map = {"cn=inesistente,ou=groups,dc=example,dc=com": "fantasma"}
    provider, _ = make_provider(cfg)
    cfg.identity_role_map = {**cfg.identity_role_map, **cfg_map}
    provider._role_map_raw.update(cfg_map)
    client = create_app(cfg, identity_provider=provider).test_client()
    tok = client.post("/identity/login",
                      json={"username": "anna", "password": "anna-pw"}).get_json()["token"]
    st = client.get("/identity/status", headers={"Authorization": f"Bearer {tok}"}).get_json()
    assert st["role_map_missing"] == list(cfg_map)
    assert st["sync"]["last_check"] and st["sessions"] == 1
    assert isinstance(st["deployment_warnings"], list)
