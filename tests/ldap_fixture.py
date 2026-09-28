"""Directory LDAP finto (ldap3 MOCK_SYNC): bind e ricerche reali di ldap3,
senza server. Il DIT sta sull'oggetto Server ed è condiviso tra le
connessioni, quindi login e lettura dei gruppi passano davvero dal codice
di LDAPProvider."""
import ldap3

from wikibridge.identity import LDAPProvider

BASE = "dc=example,dc=com"
SERVICE_DN = f"cn=svc,ou=service,{BASE}"
LEGAL = f"CN=Legal,OU=Groups,{BASE.upper()}"
ADMINS = f"cn=wb-admins,ou=groups,{BASE}"


def make_directory():
    server = ldap3.Server("fake-dc")
    seed = ldap3.Connection(server, user=SERVICE_DN, password="svc-pw",
                            client_strategy=ldap3.MOCK_SYNC)
    add = seed.strategy.add_entry
    add(SERVICE_DN, {"userPassword": "svc-pw", "cn": "svc"})
    add(f"cn=Mario Rossi,ou=users,{BASE}",
        {"userPassword": "mario-pw", "sAMAccountName": "mario",
         "displayName": "Mario Rossi", "objectClass": "person",
         "objectGUID": "{AAAAAAAA-0000-0000-0000-000000000001}",
         "memberOf": [LEGAL]})
    add(f"cn=Anna Admin,ou=users,{BASE}",
        {"userPassword": "anna-pw", "sAMAccountName": "anna",
         "displayName": "Anna Admin", "objectClass": "person",
         "objectGUID": "{AAAAAAAA-0000-0000-0000-000000000002}",
         "memberOf": [ADMINS]})
    add(f"cn=Luca Bianchi,ou=users,{BASE}",
        {"userPassword": "luca-pw", "sAMAccountName": "luca",
         "displayName": "Luca Bianchi", "objectClass": "person",
         "objectGUID": "{AAAAAAAA-0000-0000-0000-000000000003}"})
    return server, seed


MARIO = "user:aaaaaaaa-0000-0000-0000-000000000001"
LUCA = "user:aaaaaaaa-0000-0000-0000-000000000003"


def configure(cfg):
    cfg.identity_provider = "ldap"
    cfg.ldap_url = "ldap://fake-dc"
    cfg.ldap_allow_insecure = True
    cfg.ldap_base_dn = BASE
    cfg.ldap_bind_dn = SERVICE_DN
    cfg.ldap_bind_password = "svc-pw"
    # spazi e maiuscole diversi da quelli restituiti dal directory
    cfg.identity_role_map = {"cn=legal, ou=groups, dc=example, dc=com": "team-legal",
                             ADMINS: "admin"}
    return cfg


def make_provider(cfg):
    server, seed = make_directory()
    provider = LDAPProvider(configure(cfg), server=server,
                            client_strategy=ldap3.MOCK_SYNC)
    return provider, seed
