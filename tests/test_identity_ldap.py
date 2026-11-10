import pytest

from wikibridge.config import Config
from wikibridge.entitlements import EntitlementCache
from wikibridge.identity import LOGIN_FAILED, IdentityError, LDAPProvider, normalize_dn
from wikibridge.sessions import LoginThrottle, SessionStore

from .ldap_fixture import LEGAL, MARIO, configure, make_provider


def test_login_and_roles_with_dn_normalisation(cfg):
    provider, _ = make_provider(cfg)
    ident = provider.login("mario", "mario-pw")
    assert ident.display_name == "Mario Rossi"
    assert provider.resolve_roles(ident) == {"team-legal", MARIO}


def test_find_user_gives_stable_personal_principal(cfg):
    provider, _ = make_provider(cfg)
    assert provider.find_user("mario").principal == MARIO
    with pytest.raises(IdentityError):
        provider.find_user("fantasma")


@pytest.mark.parametrize("user,pw", [("mario", "sbagliata"), ("nessuno", "x"),
                                     ("mario", ""), ("", "x"), ("m*", "mario-pw")])
def test_every_login_failure_has_the_same_message(cfg, user, pw):
    provider, _ = make_provider(cfg)
    with pytest.raises(IdentityError) as e:
        provider.login(user, pw)
    assert str(e.value) == LOGIN_FAILED


def test_plaintext_ldap_refused_without_starttls():
    cfg = configure(Config())
    cfg.ldap_allow_insecure = False
    with pytest.raises(ValueError, match="StartTLS"):
        LDAPProvider(cfg)


def test_group_change_visible_after_cache_invalidation(cfg):
    provider, seed = make_provider(cfg)
    cache = EntitlementCache(provider, ttl_s=300)
    ident = provider.login("anna", "anna-pw")
    me = ident.principal
    assert cache.roles_for(ident) == {"admin", me}
    dn = ident.subject
    seed.strategy.entries[dn]["memberOf"].append(LEGAL.encode())
    assert cache.roles_for(ident) == {"admin", me}      # TTL: ancora in cache
    cache.invalidate(dn)
    assert cache.roles_for(ident) == {"admin", "team-legal", me}


def test_normalize_dn():
    assert normalize_dn("CN=Legal, OU=Groups,DC=Example") == "cn=legal,ou=groups,dc=example"
    # spazi attorno ai separatori via, spazi dentro i valori conservati
    assert normalize_dn("cn=Mario Rossi , ou = Users") == \
        normalize_dn("CN=Mario Rossi,OU=Users") == "cn=mario rossi,ou=users"


def test_session_idle_and_absolute_expiry():
    now = [0.0]
    store = SessionStore(idle_s=60, max_s=300, clock=lambda: now[0])
    from wikibridge.identity import Identity
    tok = store.create(Identity(subject="u"))
    now[0] = 50
    assert store.get(tok) is not None
    now[0] = 115                                     # 65 s di inattività
    assert store.get(tok) is None
    tok = store.create(Identity(subject="u"))
    for t in range(0, 360, 50):                      # sempre attiva...
        now[0] = 115 + t
        alive = store.get(tok)
    assert alive is None                             # ...ma oltre max_s


def test_login_throttle_window():
    now = [0.0]
    th = LoginThrottle(max_user=3, max_ip=10, window_s=100, clock=lambda: now[0])
    for _ in range(3):
        th.failure("Mario", "1.1.1.1")
    assert th.retry_after("mario", "2.2.2.2") > 0    # per utente, case-insensitive
    now[0] = 101
    assert th.retry_after("mario", "2.2.2.2") == 0
