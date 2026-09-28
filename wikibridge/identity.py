"""Identità di chi interroga l'API di wikibridge (ricerca RAG), non le
credenziali verso le sorgenti esterne da importare — quelle restano in
auth.py (AuthProfileStore).

Flusso: login() verifica le credenziali UNA volta (bind LDAP) e il server
rilascia un token di sessione (sessions.py); le richieste successive non
toccano più LDAP. resolve_roles() legge i gruppi dal directory ed è
chiamato solo al cache-miss di EntitlementCache (entitlements.py): è lì
che sta il costo LDAP, ed è lì che la cache lo copre.
"""
from __future__ import annotations

import logging
import ssl
import threading
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from .auth import _resolve
from .config import Config

log = logging.getLogger("wikibridge.identity")

# Unico messaggio per ogni fallimento di login: messaggi diversi per
# "utente inesistente" e "password errata" permetterebbero di enumerare
# gli account. Il motivo reale va solo nei log del server.
LOGIN_FAILED = "credenziali non valide"


@dataclass
class Identity:
    subject: str                      # DN LDAP, NameID SAML, sub OIDC
    display_name: str = ""
    username: str = ""
    user_id: str = ""                 # stabile anche a rinomine (objectGUID)
    raw_groups: list[str] = field(default_factory=list)

    @property
    def principal(self) -> str:
        """Principal per le autorizzazioni a persona singola."""
        return f"user:{self.user_id or normalize_dn(self.subject)}"


class IdentityError(RuntimeError):
    """Credenziale assente, errata o non verificabile."""


class IdentityProvider(ABC):
    name: str = "base"

    @abstractmethod
    def login(self, username: str, password: str) -> Identity:
        """Verifica le credenziali in modo forte (bind LDAP, ...) e ritorna
        l'Identity. Solleva IdentityError(LOGIN_FAILED) su ogni errore."""

    @abstractmethod
    def resolve_roles(self, identity: Identity) -> set[str]:
        """Ruoli applicativi (gli stessi usati in acl_roles sui blocchi),
        mappati dai gruppi nativi del provider."""


class NoneProvider(IdentityProvider):
    """Nessuna integrazione enterprise: comportamento invariato."""

    name = "none"

    def login(self, username: str, password: str) -> Identity:
        raise IdentityError("nessun identity provider configurato")

    def resolve_roles(self, identity: Identity) -> set[str]:
        return set()

    def find_user(self, username: str) -> Identity:
        raise IdentityError("nessun identity provider configurato")


def normalize_dn(dn: str) -> str:
    """DN confrontabile: AD restituisce maiuscole e spazi variabili
    ("CN=Legal, OU=Groups" vs "cn=legal,ou=groups")."""
    try:
        from ldap3.utils.dn import parse_dn
        return ",".join(f"{a.strip().lower()}={v.strip().lower()}"
                        for a, v, _ in parse_dn(dn))
    except Exception:  # noqa: BLE001 — DN non conforme: confronto grezzo
        return "".join(dn.lower().split())


class LDAPProvider(IdentityProvider):
    """LDAP/Active Directory. Le credenziali arrivano solo al login; i
    ruoli si ricavano dai gruppi (memberOf) via identity_role_map, perché i
    DN dei gruppi AD raramente coincidono con gli acl_roles dei blocchi."""

    name = "ldap"

    def __init__(self, cfg: Config, *, server=None, client_strategy=None):
        import ldap3
        self._ldap3 = ldap3
        self.url = cfg.ldap_url
        self.base_dn = cfg.ldap_base_dn
        self.user_filter = cfg.ldap_user_filter
        self.group_attr = cfg.ldap_group_attr
        self._bind_dn = cfg.ldap_bind_dn
        self._bind_password = cfg.ldap_bind_password
        self._start_tls = cfg.ldap_start_tls
        self.role_map = {normalize_dn(k): v
                         for k, v in (cfg.identity_role_map or {}).items()}
        if not self.url or not self.base_dn:
            raise ValueError("identity_provider=ldap richiede ldap_url e ldap_base_dn")
        if (self.url.lower().startswith("ldap://") and not self._start_tls
                and not cfg.ldap_allow_insecure):
            raise ValueError(
                "ldap:// senza StartTLS: le password viaggerebbero in chiaro. "
                "Usa ldaps://, ldap_start_tls=true, oppure (solo test) "
                "ldap_allow_insecure=true")
        if server is None:
            tls = ldap3.Tls(validate=ssl.CERT_REQUIRED,
                            ca_certs_file=cfg.ldap_ca_file or None)
            server = ldap3.Server(self.url, get_info=ldap3.NONE, tls=tls,
                                  use_ssl=self.url.lower().startswith("ldaps://"),
                                  connect_timeout=5)
        self._server = server
        self._strategy = client_strategy or ldap3.SYNC
        self._service_conn = None
        self._service_lock = threading.Lock()

    def _bind(self, user: str | None, password: str | None):
        """Connessione autenticata, o None se il bind non riesce (password
        errata o directory irraggiungibile: il chiamante non li distingue
        verso il client)."""
        ldap3 = self._ldap3
        conn = ldap3.Connection(self._server, user=user, password=password,
                                client_strategy=self._strategy,
                                raise_exceptions=False)
        try:
            if self._start_tls:
                conn.open()
                conn.start_tls()
            return conn if conn.bind() else None
        except ldap3.core.exceptions.LDAPException as e:
            log.warning("bind LDAP non riuscito per %s: %s", user, e)
            return None

    def _service_search(self, base: str, flt: str, attributes: list[str],
                        scope=None) -> list:
        """Ricerca con la connessione di servizio, riusata tra le chiamate
        e riaperta una volta se è caduta."""
        scope = scope or self._ldap3.SUBTREE
        with self._service_lock:
            for attempt in (1, 2):
                try:
                    if self._service_conn is None or not self._service_conn.bound:
                        self._service_conn = self._bind(
                            _resolve(self._bind_dn) or None,
                            _resolve(self._bind_password) or None)
                        if self._service_conn is None:
                            raise IdentityError("bind di servizio LDAP non riuscito")
                    self._service_conn.search(base, flt, search_scope=scope,
                                              attributes=attributes)
                    return list(self._service_conn.entries)
                except Exception as e:  # noqa: BLE001
                    self._service_conn = None
                    if attempt == 2:
                        log.error("directory LDAP non utilizzabile: %s", e)
                        raise IdentityError(LOGIN_FAILED) from e
        return []

    @staticmethod
    def _guid(entry) -> str:
        if "objectGUID" not in entry or not entry["objectGUID"].raw_values:
            return ""
        raw = entry["objectGUID"].raw_values[0]
        if isinstance(raw, bytes) and len(raw) == 16:      # formato binario AD
            return str(uuid.UUID(bytes_le=raw))
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        return text.strip("{}").lower()

    def _find(self, username: str) -> list:
        safe = self._ldap3.utils.conv.escape_filter_chars(username)
        return self._service_search(self.base_dn,
                                    self.user_filter.format(username=safe),
                                    ["displayName", "cn", "objectGUID"])

    def find_user(self, username: str) -> Identity:
        """Risolve uno username senza password (per assegnare permessi a
        persona singola). Solleva IdentityError se non esiste o è ambiguo."""
        entries = self._find(username)
        if len(entries) != 1:
            raise IdentityError(f"utente non trovato o ambiguo: {username}")
        e = entries[0]
        name = e["displayName"].value if "displayName" in e else None
        return Identity(subject=e.entry_dn, username=username,
                        display_name=str(name or username), user_id=self._guid(e))

    def login(self, username: str, password: str) -> Identity:
        if not username or not password:
            # password vuota = "unauthenticated bind" che molti DC accettano
            raise IdentityError(LOGIN_FAILED)
        entries = self._find(username)
        if len(entries) != 1:
            # bind fittizio: stessi tempi di risposta di una password errata
            self._bind(f"cn={uuid.uuid4().hex},{self.base_dn}", password)
            log.info("login fallito per %r: utente non trovato o ambiguo", username)
            raise IdentityError(LOGIN_FAILED)
        entry = entries[0]
        conn = self._bind(entry.entry_dn, password)
        if conn is None:
            log.info("login fallito per %r: password errata", username)
            raise IdentityError(LOGIN_FAILED)
        conn.unbind()
        name = entry["displayName"].value if "displayName" in entry else None
        return Identity(subject=entry.entry_dn, username=username,
                        display_name=str(name or username),
                        user_id=self._guid(entry))

    def resolve_roles(self, identity: Identity) -> set[str]:
        entries = self._service_search(identity.subject, "(objectClass=*)",
                                       [self.group_attr],
                                       scope=self._ldap3.BASE)
        groups: list[str] = []
        if entries and self.group_attr in entries[0]:
            groups = [str(g) for g in entries[0][self.group_attr].values]
        identity.raw_groups = groups
        roles = {self.role_map[normalize_dn(g)] for g in groups
                 if normalize_dn(g) in self.role_map}
        if groups and not roles:
            log.debug("nessun ruolo mappato per %s", identity.subject)
        # la persona stessa è un principal: permessi a persona singola
        return roles | {identity.principal}


def build_identity_provider(cfg: Config) -> IdentityProvider:
    provider = cfg.identity_provider
    if provider == "none":
        return NoneProvider()
    if provider == "ldap":
        return LDAPProvider(cfg)
    raise ValueError(f"identity_provider non supportato: {provider}")
