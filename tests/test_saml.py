"""Identity provider SAML: flusso SP-initiated contro un IdP finto che
firma davvero le asserzioni (xmlsec), e rifiuto delle risposte non valide."""
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("onelogin.saml2")

from wikibridge.saml_identity import SAMLProvider  # noqa: E402
from wikibridge.server import create_app  # noqa: E402

from .ldap_fixture import MARIO, bump_usn, make_provider, set_attr, LEGAL, MARIO_DN  # noqa: E402
from .saml_fixture import GROUPS, OID, FakeSamlIdP, IDP_SSO, configure  # noqa: E402

ACS = "http://127.0.0.1:8775/identity/saml/acs"
SP = "http://127.0.0.1:8775/identity/saml/metadata"
BACK = "http://127.0.0.1:9999/cb"


@pytest.fixture
def idp():
    return FakeSamlIdP()


@pytest.fixture
def client(cfg, idp):
    configure(cfg, idp)
    return create_app(cfg).test_client()


def start(client, return_to=BACK):
    r = client.get("/identity/saml/login", query_string={"return_to": return_to})
    assert r.status_code == 302 and r.headers["Location"].startswith(IDP_SSO)
    return FakeSamlIdP.request_of(r.headers["Location"])


def post(client, relay, saml_response):
    return client.post("/identity/saml/acs",
                       data={"SAMLResponse": saml_response, "RelayState": relay})


def sso(client, idp, nameid="mario.rossi@example.com", groups=("grp-legal",), **kw):
    req_id, relay = start(client)
    kw.setdefault("attrs", {OID: "oid-mario", GROUPS: list(groups)})
    resp = idp.response(in_response_to=req_id, acs=ACS, audience=SP, nameid=nameid, **kw)
    return post(client, relay, resp)


def token_from(resp):
    return parse_qs(urlparse(resp.headers["Location"]).query)["token"][0]


def whoami(client, resp):
    return client.get("/identity/whoami",
                      headers={"Authorization": f"Bearer {token_from(resp)}"}).get_json()


def test_login_with_signed_assertion(client, idp):
    r = sso(client, idp)
    assert r.status_code == 303 and r.headers["Location"].startswith(BACK + "?token=")
    me = whoami(client, r)
    assert me["authenticated"] and not me["admin"]
    assert set(me["roles"]) == {"team-legal", "user:oid-mario"}
    admin = sso(client, idp, groups=("GRP-Admins",))       # mappa case-insensitive
    assert whoami(client, admin)["admin"]


def test_health_metadata_and_password_login(client):
    h = client.get("/health").get_json()
    assert h["login"] == "sso" and h["sso_login"] == "/identity/saml/login"
    meta = client.get("/identity/saml/metadata")
    assert meta.status_code == 200 and SP.encode() in meta.data and ACS.encode() in meta.data
    assert client.post("/identity/login", json={"username": "a", "password": "b"}
                       ).status_code == 400
    assert client.get("/identity/oidc/login").status_code == 404


@pytest.mark.parametrize("variant", [
    {"sign": "none"},                       # asserzione non firmata
    {"sign": "other"},                      # firmata da un'altra chiave
    {"tamper": True},                       # modificata dopo la firma
    {"expired": True},                      # scaduta
])
def test_invalid_responses_rejected(client, idp, variant):
    groups = ("grp-utenti",) if variant.get("tamper") else ("grp-legal",)
    assert sso(client, idp, groups=groups, **variant).status_code == 401


def test_wrong_audience_and_destination_rejected(client, idp):
    req_id, relay = start(client)
    bad = idp.response(in_response_to=req_id, acs=ACS, audience="https://altro-sp",
                       nameid="mario.rossi@example.com")
    assert post(client, relay, bad).status_code == 401
    req_id, relay = start(client)
    bad = idp.response(in_response_to=req_id, acs="https://evil.example/acs",
                       audience=SP, nameid="mario.rossi@example.com")
    assert post(client, relay, bad).status_code == 401


def test_replay_unsolicited_and_mismatched_request(client, idp):
    req_id, relay = start(client)
    good = idp.response(in_response_to=req_id, acs=ACS, audience=SP,
                        nameid="mario.rossi@example.com")
    assert post(client, relay, good).status_code == 303
    assert post(client, relay, good).status_code == 401            # replay
    assert post(client, "inventato", good).status_code == 401      # non richiesta
    _, relay2 = start(client)
    # risposta valida ma per un'altra richiesta (InResponseTo diverso)
    assert post(client, relay2, good).status_code == 401
    assert post(client, relay2, "non-base64!!").status_code == 401  # già consumato
    _, relay3 = start(client)
    assert post(client, relay3, "non-base64!!").status_code == 401  # malformato


def test_return_to_checked(client):
    r = client.get("/identity/saml/login", query_string={"return_to": "https://evil.example/x"})
    assert r.status_code == 400


def test_transient_nameid_needs_stable_attribute(cfg, idp):
    configure(cfg, idp)
    cfg.saml_user_id_attr = ""
    client = create_app(cfg).test_client()
    r = sso(client, idp, nameid="_t123",
            nameid_format="urn:oasis:names:tc:SAML:2.0:nameid-format:transient")
    assert r.status_code == 401
    r = sso(client, idp, nameid="Mario.Rossi@example.com")          # persistente: ok
    assert "user:mario.rossi@example.com" in whoami(client, r)["roles"]


def test_seen_user_can_be_named_after_login(cfg, idp):
    configure(cfg, idp)
    provider = SAMLProvider(cfg)
    client = create_app(cfg, identity_provider=provider).test_client()
    from wikibridge.identity import IdentityError
    with pytest.raises(IdentityError):
        provider.find_user("mario.rossi@example.com")
    sso(client, idp)
    assert provider.find_user("MARIO.ROSSI@example.com").principal == "user:oid-mario"


def test_roles_from_ldap_follow_directory(cfg, idp):
    """saml_roles_source=ldap: SAML per il login, gruppi e account dal
    directory, con revoca senza nuovo login."""
    directory, seed = make_provider(cfg)
    configure(cfg, idp, roles_source="ldap")
    provider = SAMLProvider(cfg, directory=directory)
    client = create_app(cfg, identity_provider=provider).test_client()
    r = sso(client, idp, nameid="mario", attrs={})
    h = {"Authorization": f"Bearer {token_from(r)}"}
    admin = {"Authorization": f"Bearer {token_from(sso(client, idp, nameid='anna', attrs={}))}"}
    me = client.get("/identity/whoami", headers=h).get_json()
    assert set(me["roles"]) == {"team-legal", MARIO}               # stesso principal di LDAP

    set_attr(seed, MARIO_DN, "memberOf", [])                        # revoca nel directory
    bump_usn(seed, LEGAL, 300)
    assert client.post("/identity/sync-groups", headers=admin).get_json()["changed"]
    assert client.get("/identity/whoami", headers=h).get_json()["roles"] == [MARIO]

    set_attr(seed, MARIO_DN, "userAccountControl", ["514"])         # disabilitato
    bump_usn(seed, MARIO_DN, 301)
    assert client.post("/identity/sync-groups", headers=admin).get_json()["changed"]
    assert client.get("/identity/whoami", headers=h).status_code == 401

    assert sso(client, idp, nameid="sconosciuto", attrs={}).status_code == 401
