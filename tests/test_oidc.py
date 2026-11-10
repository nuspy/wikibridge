"""Identity provider OIDC contro un IdP finto su HTTP reale."""
import base64
import hashlib
import hmac
import json
from urllib.parse import parse_qs, urlparse

import pytest
import requests
from cryptography.hazmat.primitives import serialization

from wikibridge.server import create_app

from .oidc_fixture import CLIENT_ID, FakeIdP, configure


@pytest.fixture
def idp():
    server = FakeIdP()
    yield server
    server.stop()


@pytest.fixture
def client(cfg, idp):
    configure(cfg, idp)
    return create_app(cfg).test_client()


def sso(client, user, return_to="http://127.0.0.1:9999/cb"):
    """Percorre il flusso browser: wikibridge -> IdP -> callback."""
    r = client.get("/identity/oidc/login", query_string={"return_to": return_to})
    assert r.status_code == 302
    to_idp = r.headers["Location"] + f"&login_hint={user}"
    back = requests.get(to_idp, allow_redirects=False).headers["Location"]
    q = urlparse(back)
    return client.get(q.path, query_string=q.query)


def token_from(resp):
    return parse_qs(urlparse(resp.headers["Location"]).query)["token"][0]


def test_browser_flow_creates_session_with_mapped_roles(client):
    r = sso(client, "mario")
    assert r.status_code == 302 and r.headers["Cache-Control"] == "no-store"
    h = {"Authorization": f"Bearer {token_from(r)}"}
    me = client.get("/identity/whoami", headers=h).get_json()
    assert me["authenticated"] and not me["admin"]
    assert set(me["roles"]) == {"team-legal", "user:oid-mario"}
    anna = {"Authorization": f"Bearer {token_from(sso(client, 'anna'))}"}
    assert client.get("/identity/whoami", headers=anna).get_json()["admin"]  # case-insensitive


def test_state_is_single_use_and_return_to_is_checked(client, idp):
    r = client.get("/identity/oidc/login", query_string={"return_to": "https://evil.example/x"})
    assert r.status_code == 400
    r = client.get("/identity/oidc/login", query_string={"return_to": ""})
    back = requests.get(r.headers["Location"] + "&login_hint=mario",
                        allow_redirects=False).headers["Location"]
    q = urlparse(back)
    ok = client.get(q.path, query_string=q.query)
    assert ok.status_code == 200 and b"Token di sessione" in ok.data
    assert client.get(q.path, query_string=q.query).status_code == 401     # replay


def test_nonce_mismatch_rejected(client, idp):
    idp.nonce_override = "altro-nonce"
    assert sso(client, "mario").status_code == 401


@pytest.mark.parametrize("bad", ["aud", "exp", "iss", "hs256", "garbage", "empty"])
def test_token_exchange_rejects_invalid_tokens(client, idp, bad):
    if bad in ("garbage", "empty"):
        tok = {"garbage": "abc.def", "empty": ""}[bad]
    elif bad == "hs256":
        # attacco classico: HS256 usando la chiave pubblica come segreto.
        # Forgiato a mano: PyJWT recenti si rifiutano anche di crearlo.
        pub = idp.key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        b64 = lambda d: base64.urlsafe_b64encode(  # noqa: E731
            json.dumps(d).encode()).rstrip(b"=").decode()
        signing = (f'{b64({"alg": "HS256", "typ": "JWT", "kid": "k1"})}.'   # kid valido
                   f'{b64({"iss": idp.issuer, "aud": CLIENT_ID, "sub": "x", "iat": 1, "exp": 9999999999})}')
        sig = base64.urlsafe_b64encode(
            hmac.new(pub, signing.encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
        tok = f"{signing}.{sig}"
    else:
        override = {"aud": {"aud": "altro-client"}, "exp": {"exp": 1000},
                    "iss": {"iss": "https://altro.example"}}[bad]
        tok = idp.id_token("mario", **override)
    assert client.post("/identity/oidc/token", json={"id_token": tok}).status_code == 401


def test_token_exchange_accepts_valid_token(client, idp):
    r = client.post("/identity/oidc/token", json={"id_token": idp.id_token("mario")})
    assert r.status_code == 200 and "team-legal" in r.get_json()["roles"]


def test_groups_reread_on_refresh_and_disabled_user_revoked(client, idp):
    mario = {"Authorization": f"Bearer {token_from(sso(client, 'mario'))}"}
    idp.users["mario"]["groups"] = []                          # tolto dal gruppo
    r = client.post("/identity/refresh", headers=mario).get_json()
    assert "team-legal" not in r["roles"]
    idp.disabled.add("mario")                                  # disabilitato
    r = client.post("/identity/refresh", headers=mario)
    assert r.status_code == 401
    assert client.get("/identity/whoami", headers=mario).status_code == 401


def test_personal_grant_by_name_after_first_login(client, cfg):
    anna = {"Authorization": f"Bearer {token_from(sso(client, 'anna'))}"}
    r = client.post("/identity/invalidate", headers=anna, json={"username": "mario"})
    assert r.status_code == 404                               # mai visto
    sso(client, "mario")
    for name in ("mario", "mario@example.com"):
        r = client.post("/identity/invalidate", headers=anna, json={"username": name})
        assert r.status_code == 200, name


def test_password_login_is_refused_with_oidc(client):
    r = client.post("/identity/login", json={"username": "mario", "password": "x"})
    assert r.status_code == 400
    assert client.get("/health").get_json()["login"] == "sso"
