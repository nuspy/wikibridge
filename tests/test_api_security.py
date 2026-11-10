import pytest

from wikibridge.server import check_deployment, create_app

from .ldap_fixture import make_provider


@pytest.fixture
def client(cfg):
    provider, _ = make_provider(cfg)
    return create_app(cfg, identity_provider=provider).test_client()


def login(client, user, pw, **headers):
    r = client.post("/identity/login", json={"username": user, "password": pw},
                    headers=headers)
    return r, {"Authorization": f"Bearer {r.get_json().get('token', '')}", **headers}


def test_requests_without_session_are_refused(client):
    assert client.get("/search?q=x").status_code == 401
    assert client.get("/health").get_json() == {"ok": True, "multi_user": True,
                                                "login": "password"}


def test_login_logout_and_whoami(client):
    r, h = login(client, "mario", "mario-pw")
    assert r.status_code == 200 and "team-legal" in r.get_json()["roles"]
    me = client.get("/identity/whoami", headers=h).get_json()
    assert me["authenticated"] and not me["admin"]
    assert client.get("/search?q=x", headers=h).status_code == 200
    client.post("/identity/logout", headers=h)
    assert client.get("/search?q=x", headers=h).status_code == 401


def test_non_admin_cannot_manage(client):
    _, h = login(client, "mario", "mario-pw")
    for method, path in [("post", "/projects/default/acl"), ("get", "/imports"),
                         ("post", "/update"), ("get", "/sources"),
                         ("get", "/auth/profiles"), ("post", "/projects")]:
        r = getattr(client, method)(path, json={"enabled": False}, headers=h)
        assert r.status_code == 403, path


def test_admin_can_manage(client):
    _, h = login(client, "anna", "anna-pw")
    r = client.post("/projects/default/acl", json={"enabled": True}, headers=h)
    assert r.status_code == 200 and r.get_json()["acl_enforcement"] is True
    assert "active_project" in client.get("/health", headers=h).get_json()


def test_login_failures_are_uniform_and_throttled(client):
    msgs = {login(client, u, p)[0].get_json()["error"]
            for u, p in [("mario", "no"), ("fantasma", "no")]}
    assert msgs == {"credenziali non valide"}
    for _ in range(4):
        login(client, "mario", "no")
    r, _ = login(client, "mario", "mario-pw")        # anche con la password giusta
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0


def test_application_key_is_separate_from_user_session(cfg):
    cfg.api_auth_token = "chiave-app"
    provider, _ = make_provider(cfg)
    client = create_app(cfg, identity_provider=provider).test_client()
    r, _ = login(client, "mario", "mario-pw")
    assert r.status_code == 401                      # manca la chiave app
    r, h = login(client, "mario", "mario-pw", **{"X-Wikibridge-Key": "chiave-app"})
    assert r.status_code == 200
    assert client.get("/search?q=x", headers=h).status_code == 200
    h.pop("X-Wikibridge-Key")
    assert client.get("/search?q=x", headers=h).status_code == 401


def test_single_user_mode_unchanged(cfg):
    client = create_app(cfg).test_client()
    assert client.post("/projects/default/acl", json={"enabled": True}).status_code == 200
    cfg.api_auth_token = "tok"
    client = create_app(cfg).test_client()
    assert client.get("/projects").status_code == 401
    assert client.get("/projects", headers={"Authorization": "Bearer tok"}).status_code == 200


def test_network_exposure_requires_tls(cfg):
    cfg.identity_provider = "ldap"
    cfg.api_host = "0.0.0.0"
    with pytest.raises(ValueError, match="TLS"):
        check_deployment(cfg)
    cfg.behind_tls_proxy = True
    check_deployment(cfg)
