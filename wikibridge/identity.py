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

import json
import logging
import re
import ssl
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

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


class AccountInactive(IdentityError):
    """L'account esiste nella sessione ma non più (o non attivo) nel
    directory: la sessione va revocata. Diverso dal directory
    irraggiungibile, che non deve disconnettere nessuno."""


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

    def groups_changed(self) -> bool:
        """True se nel directory qualche gruppo è cambiato dall'ultimo
        controllo (per invalidare i ruoli in cache prima del TTL)."""
        return False

    def check_role_map(self) -> list[str]:
        """Voci di identity_role_map che non corrispondono a un gruppo."""
        return []

    def clear_cache(self) -> None:
        """Svuota eventuali cache interne del provider."""


class SeenUsers:
    """Registro degli utenti visti al login (identities.json), per i
    provider SSO senza un directory interrogabile: permette di indicare una
    persona per nome (permessi personali, partecipanti) dopo il suo primo
    login. Un nome non passa mai da un'identità a un'altra."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def _load(self) -> dict:
        if self.path.is_file():
            return json.loads(self.path.read_text(encoding="utf-8"))
        return {}

    def get(self, name: str) -> dict | None:
        return self._load().get(name.casefold())

    def add(self, ident: Identity, names: set[str]) -> None:
        entry = {"subject": ident.subject, "user_id": ident.user_id,
                 "display_name": ident.display_name}
        with self._lock:
            reg = self._load()
            for n in {n.casefold() for n in names if n}:
                if n in reg and reg[n]["subject"] != ident.subject:
                    log.warning("registro identità: %r è già di %s, non riassegnato "
                                "a %s", n, reg[n]["subject"], ident.subject)
                    continue
                reg[n] = entry
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(reg, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.path)


class NoneProvider(IdentityProvider):
    """Nessuna integrazione enterprise: comportamento invariato."""

    name = "none"

    def login(self, username: str, password: str) -> Identity:
        raise IdentityError("nessun identity provider configurato")

    def resolve_roles(self, identity: Identity) -> set[str]:
        return set()

    def find_user(self, username: str) -> Identity:
        raise IdentityError("nessun identity provider configurato")


_DN_SEP_SPACES = re.compile(r"(?<!\\)\s*([,=])\s*")


def clean_dn(dn: str) -> str:
    """DN utilizzabile come base di ricerca: via gli spazi attorno a
    virgole e uguali ("cn=Legal, ou=Groups", che ldap3 rifiuta), maiuscole
    e spazi interni ai valori invariati."""
    return _DN_SEP_SPACES.sub(r"\1", dn.strip())


def normalize_dn(dn: str) -> str:
    """DN confrontabile: AD restituisce maiuscole e spazi variabili
    ("CN=Legal, OU=Groups" vs "cn=legal,ou=groups")."""
    cleaned = clean_dn(dn)
    try:
        from ldap3.utils.dn import parse_dn
        return ",".join(f"{a.strip().lower()}={v.strip().lower()}"
                        for a, v, _ in parse_dn(cleaned))
    except Exception:  # noqa: BLE001 — DN non conforme: confronto grezzo
        return cleaned.lower()


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
        self.mail_filter = cfg.ldap_mail_filter
        self.group_attr = cfg.ldap_group_attr
        self._bind_dn = cfg.ldap_bind_dn
        self._bind_password = cfg.ldap_bind_password
        self._start_tls = cfg.ldap_start_tls
        self._role_map_raw = dict(cfg.identity_role_map or {})
        self.role_map = {normalize_dn(k): v for k, v in self._role_map_raw.items()}
        self.nested = cfg.ldap_nested_groups
        self.nested_depth = cfg.ldap_nested_depth
        self.change_attr = cfg.ldap_change_attr
        self.watch_filter = cfg.ldap_watch_filter
        self._parents_ttl = cfg.entitlement_cache_ttl_s
        self._parents: dict[str, tuple[list[str], float]] = {}
        self._parents_lock = threading.Lock()
        self._change_mark = None
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
        """Per username o, se contiene @, per email (i partecipanti di una
        riunione arrivano spesso come indirizzi)."""
        safe = self._ldap3.utils.conv.escape_filter_chars(username)
        template = self.mail_filter if "@" in username else self.user_filter
        return self._service_search(self.base_dn, template.format(username=safe),
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

    def _direct_groups(self, dn: str) -> list[str]:
        entries = self._service_search(dn, "(objectClass=*)", [self.group_attr],
                                       scope=self._ldap3.BASE)
        if entries and self.group_attr in entries[0]:
            return [str(g) for g in entries[0][self.group_attr].values]
        return []

    def _group_parents(self, group_dn: str) -> list[str]:
        """Gruppi di cui un gruppo è membro, in cache (condivisa tra utenti:
        i gruppi sono pochi e cambiano raramente)."""
        key = normalize_dn(group_dn)
        now = time.monotonic()
        with self._parents_lock:
            hit = self._parents.get(key)
            if hit and now - hit[1] < self._parents_ttl:
                return hit[0]
        try:
            parents = self._direct_groups(clean_dn(group_dn))
        except IdentityError:
            parents = []
        with self._parents_lock:
            self._parents[key] = (parents, now)
        return parents

    def _check_active(self, identity: Identity) -> None:
        """Account rimosso o disabilitato (userAccountControl bit 2, AD):
        IdentityError, che per il server significa sessione da revocare."""
        entries = self._service_search(identity.subject, "(objectClass=*)",
                                       ["userAccountControl"],
                                       scope=self._ldap3.BASE)
        if not entries:
            raise AccountInactive("account rimosso dal directory")
        uac = entries[0]["userAccountControl"].values if \
            "userAccountControl" in entries[0] else []
        if uac and str(uac[0]).isdigit() and int(uac[0]) & 2:
            raise AccountInactive("account disabilitato")

    def _all_groups(self, identity: Identity) -> list[str]:
        if self.nested == "in_chain":
            safe = self._ldap3.utils.conv.escape_filter_chars(identity.subject)
            flt = f"(member:1.2.840.113556.1.4.1941:={safe})"
            return [e.entry_dn for e in
                    self._service_search(self.base_dn, flt, ["cn"])]
        direct = self._direct_groups(identity.subject)
        if self.nested != "memberof":
            return direct
        seen = {normalize_dn(g): g for g in direct}
        frontier = list(direct)
        for _ in range(self.nested_depth):        # profondità limitata, cicli ok
            nxt = []
            for g in frontier:
                for parent in self._group_parents(g):
                    if normalize_dn(parent) not in seen:
                        seen[normalize_dn(parent)] = parent
                        nxt.append(parent)
            if not nxt:
                break
            frontier = nxt
        return list(seen.values())

    def resolve_roles(self, identity: Identity) -> set[str]:
        self._check_active(identity)
        groups = self._all_groups(identity)
        identity.raw_groups = groups
        roles = {self.role_map[normalize_dn(g)] for g in groups
                 if normalize_dn(g) in self.role_map}
        if groups and not roles:
            log.debug("nessun ruolo mappato per %s", identity.subject)
        # la persona stessa è un principal: permessi a persona singola
        return roles | {identity.principal}

    @staticmethod
    def _mark(value: str):
        return int(value) if value.isdigit() else value

    def groups_changed(self) -> bool:
        """Confronta il valore più alto di ldap_change_attr tra i gruppi con
        quello del controllo precedente. Il primo controllo fissa solo il
        punto di partenza. Un cambio di appartenenza modifica l'oggetto
        gruppo (member), quindi basta osservare i gruppi."""
        attr = self.change_attr
        start = self._change_mark if self._change_mark is not None else 0
        flt = f"(&{self.watch_filter}({attr}>={start}))"
        with self._service_lock:
            try:
                if self._service_conn is None or not self._service_conn.bound:
                    self._service_conn = self._bind(
                        _resolve(self._bind_dn) or None,
                        _resolve(self._bind_password) or None)
                    if self._service_conn is None:
                        raise IdentityError("bind di servizio LDAP non riuscito")
                results = self._service_conn.extend.standard.paged_search(
                    self.base_dn, flt, attributes=[attr], paged_size=500,
                    generator=False)
            except IdentityError:
                raise
            except Exception as e:  # noqa: BLE001
                self._service_conn = None
                raise IdentityError(f"controllo modifiche LDAP fallito: {e}") from e
        values = []
        for r in results or []:
            raw = (r.get("raw_attributes") or {}).get(attr) or []
            if raw:
                v = raw[0]
                values.append(self._mark(v.decode() if isinstance(v, bytes) else str(v)))
        if not values:
            return False
        newest = max(values)
        first = self._change_mark is None
        changed = not first and newest > self._change_mark
        if first or changed:
            self._change_mark = newest
        if changed:
            self.clear_cache()
        return changed

    def check_role_map(self) -> list[str]:
        missing = []
        for dn in self._role_map_raw:
            try:
                if not self._service_search(clean_dn(dn), "(objectClass=*)", ["cn"],
                                            scope=self._ldap3.BASE):
                    missing.append(dn)
            except IdentityError:
                missing.append(dn)
        return missing

    def clear_cache(self) -> None:
        with self._parents_lock:
            self._parents.clear()


def build_identity_provider(cfg: Config) -> IdentityProvider:
    provider = cfg.identity_provider
    if provider == "none":
        return NoneProvider()
    if provider == "ldap":
        return LDAPProvider(cfg)
    if provider == "oidc":
        from .oidc_identity import OIDCProvider
        return OIDCProvider(cfg)
    if provider == "saml":
        from .saml_identity import SAMLProvider
        return SAMLProvider(cfg)
    raise ValueError(f"identity_provider non supportato: {provider}")
