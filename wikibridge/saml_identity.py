"""Identity provider SAML 2.0 (AD FS, Entra ID, Okta, Shibboleth, ...).

Flusso (SP-initiated, binding Redirect → POST):
1. il client apre nel browser GET /identity/saml/login?return_to=...;
2. authorization_url() crea l'AuthnRequest e un RelayState monouso;
3. l'IdP rimanda il browser con un POST su /identity/saml/acs; complete()
   verifica la firma dell'asserzione con il certificato dell'IdP,
   destinatario, audience, scadenze e InResponseTo (deve rispondere a una
   richiesta partita da qui: niente login avviati dall'IdP, niente replay);
4. il server crea una sessione wikibridge, consegnata SOLO a un return_to
   ammesso, come per OIDC.

Ruoli (saml_roles_source):
- "ldap": SAML fa solo il login; l'utente viene cercato nel directory
  (ldap_*) e ruoli, account disabilitati e modifiche dei gruppi seguono il
  provider LDAP: revoche in tempo reale. È la scelta consigliata con AD FS.
- "assertion": gruppi dall'attributo saml_groups_attr via
  identity_role_map. SAML non ha un refresh token: i gruppi restano quelli
  del login fino alla scadenza della sessione (session_max_hours), e un
  account disabilitato resta dentro fino ad allora.

Non supportati: asserzioni cifrate, AuthnRequest firmate, Single Logout,
login avviato dall'IdP. Firma SHA-1 rifiutata.
"""
from __future__ import annotations

import logging
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from .auth import _resolve
from .config import Config
from .identity import (Identity, IdentityError, IdentityProvider, LDAPProvider,
                       SeenUsers)

log = logging.getLogger("wikibridge.saml")

PENDING_TTL_S = 600
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_POST = "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"
_REDIRECT = "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"
_TRANSIENT = "urn:oasis:names:tc:SAML:2.0:nameid-format:transient"


def _pem(value: str) -> str:
    """Certificato come testo PEM, percorso di un file, o env:VAR."""
    value = _resolve(value) if value else ""
    if value and "BEGIN CERTIFICATE" not in value and Path(value).is_file():
        value = Path(value).read_text(encoding="utf-8")
    return value


class SAMLProvider(IdentityProvider):
    name = "saml"

    def __init__(self, cfg: Config, *, directory: IdentityProvider | None = None):
        try:
            from onelogin.saml2.settings import OneLogin_Saml2_Settings
        except ImportError as e:
            raise ValueError("identity_provider=saml richiede python3-saml "
                             "(pip install 'wikibridge[saml]')") from e
        self.acs_url = (cfg.saml_acs_url or
                        f"http://127.0.0.1:{cfg.api_port}/identity/saml/acs")
        base = self.acs_url.rsplit("/", 1)[0]
        self.sp_entity_id = cfg.saml_sp_entity_id or f"{base}/metadata"
        idp = self._idp_settings(cfg)
        self.idp_entity_id = idp["entityId"]
        self._settings = OneLogin_Saml2_Settings({
            "strict": True, "debug": False,
            "sp": {"entityId": self.sp_entity_id,
                   "assertionConsumerService": {"url": self.acs_url, "binding": _POST},
                   "NameIDFormat": "urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified"},
            "idp": idp,
            "security": {"wantAssertionsSigned": True, "wantMessagesSigned": False,
                         "wantNameId": True, "wantAttributeStatement": False,
                         "requestedAuthnContext": False,
                         "rejectDeprecatedAlgorithm": True,
                         "allowRepeatAttributeName": True}},
            sp_validation_only=True)
        self.roles_source = cfg.saml_roles_source
        if self.roles_source not in ("assertion", "ldap"):
            raise ValueError("saml_roles_source deve essere 'assertion' o 'ldap'")
        self.directory = directory
        if self.roles_source == "ldap" and self.directory is None:
            self.directory = LDAPProvider(cfg)
        self.groups_attr = cfg.saml_groups_attr
        self.username_attr = cfg.saml_username_attr
        self.user_id_attr = cfg.saml_user_id_attr
        self.display_name_attr = cfg.saml_display_name_attr
        self.return_urls = list(cfg.saml_return_urls or [])
        self.role_map = {str(k).casefold(): v
                         for k, v in (cfg.identity_role_map or {}).items()}
        self._lock = threading.Lock()
        self._pending: dict[str, dict] = {}
        self._seen = SeenUsers(Path(cfg.data_dir) / "identities.json")

    # -- configurazione dell'IdP --------------------------------------------------

    @staticmethod
    def _idp_settings(cfg: Config) -> dict:
        if cfg.saml_idp_metadata:
            from onelogin.saml2.idp_metadata_parser import \
                OneLogin_Saml2_IdPMetadataParser as Parser
            src = cfg.saml_idp_metadata
            if src.startswith(("http://", "https://")):
                if src.startswith("http://") and not cfg.saml_allow_insecure:
                    # metadati in chiaro: chi li altera sostituisce il certificato
                    raise ValueError("saml_idp_metadata deve essere https:// o un file "
                                     "(saml_allow_insecure solo per test)")
                try:
                    parsed = Parser.parse_remote(src, timeout=20)
                except Exception as e:  # noqa: BLE001 — urllib, XML
                    raise ValueError(f"metadati SAML non leggibili da {src}: {e}") from e
            else:
                parsed = Parser.parse(Path(src).read_text(encoding="utf-8"))
            idp = parsed.get("idp") or {}
        else:
            idp = {"entityId": cfg.saml_idp_entity_id,
                   "singleSignOnService": {"url": cfg.saml_idp_sso_url,
                                           "binding": _REDIRECT},
                   "x509cert": _pem(cfg.saml_idp_cert)}
        if (not idp.get("entityId") or not idp.get("singleSignOnService", {}).get("url")
                or not (idp.get("x509cert") or idp.get("x509certMulti"))):
            raise ValueError("identity_provider=saml richiede saml_idp_metadata oppure "
                             "saml_idp_entity_id, saml_idp_sso_url e saml_idp_cert")
        return idp

    def sp_metadata(self) -> str:
        """Metadati di wikibridge da registrare nell'IdP (entity id, ACS)."""
        meta = self._settings.get_sp_metadata()
        return meta.decode() if isinstance(meta, bytes) else meta

    def _request_data(self, post: dict | None = None) -> dict:
        """La richiesta vista da python3-saml: sempre l'ACS configurato, così
        il controllo sul destinatario confronta con l'URL dichiarato all'IdP
        anche dietro un reverse proxy."""
        u = urlparse(self.acs_url)
        return {"https": "on" if u.scheme == "https" else "off",
                "http_host": u.netloc, "script_name": u.path,
                "get_data": {}, "post_data": dict(post or {})}

    # -- flusso browser -----------------------------------------------------------

    def return_to_allowed(self, url: str) -> bool:
        if not url:
            return True
        p = urlparse(url)
        if p.scheme == "http" and p.hostname in _LOOPBACK:
            return True
        return any(url.startswith(prefix) for prefix in self.return_urls)

    def authorization_url(self, return_to: str = "") -> str:
        from onelogin.saml2.auth import OneLogin_Saml2_Auth
        if not self.return_to_allowed(return_to):
            raise IdentityError("return_to non ammesso")
        relay = secrets.token_urlsafe(24)
        auth = OneLogin_Saml2_Auth(self._request_data(), old_settings=self._settings)
        url = auth.login(return_to=relay)
        now = time.monotonic()
        with self._lock:
            self._pending = {k: v for k, v in self._pending.items()
                             if now - v["at"] < PENDING_TTL_S}
            self._pending[relay] = {"request_id": auth.get_last_request_id(),
                                    "return_to": return_to, "at": now}
        return url

    def complete(self, form: dict) -> tuple[Identity, str]:
        """Valida il POST dell'IdP sull'ACS (SAMLResponse + RelayState)."""
        from onelogin.saml2.auth import OneLogin_Saml2_Auth
        with self._lock:
            pending = self._pending.pop(str(form.get("RelayState", "")), None)  # monouso
        if pending is None or time.monotonic() - pending["at"] > PENDING_TTL_S:
            raise IdentityError("RelayState sconosciuto o scaduto (login non partito "
                                "da wikibridge, già usato, o troppo vecchio)")
        auth = OneLogin_Saml2_Auth(self._request_data(form), old_settings=self._settings)
        try:
            auth.process_response(request_id=pending["request_id"])
        except Exception as e:  # noqa: BLE001 — XML malformato, base64 errato, ...
            raise IdentityError(f"risposta SAML non valida: {e}") from e
        if auth.get_errors() or not auth.is_authenticated():
            raise IdentityError("risposta SAML non valida: "
                                f"{auth.get_last_error_reason() or auth.get_errors()}")
        return self._identity(auth), pending["return_to"]

    # -- identità -----------------------------------------------------------------

    @staticmethod
    def _first(attrs: dict, name: str) -> str:
        values = (attrs.get(name) or []) if name else []
        return str(values[0]) if values else ""

    def _identity(self, auth) -> Identity:
        attrs = auth.get_attributes() or {}
        nameid = auth.get_nameid() or ""
        username = self._first(attrs, self.username_attr) or nameid
        if self.roles_source == "ldap":
            # stessa identità del login LDAP: principal user:<objectGUID>
            ident = self.directory.find_user(username)
            log.info("login SAML di %s risolto nel directory", username)
            return ident
        user_id = self._first(attrs, self.user_id_attr)
        if not user_id:
            if auth.get_nameid_format() == _TRANSIENT:
                # un NameID transient cambia a ogni login: i permessi personali
                # e le riunioni non ritroverebbero mai la persona
                raise IdentityError("NameID transient: configurare saml_user_id_attr "
                                    "con un attributo stabile")
            user_id = nameid.casefold() if "@" in nameid else nameid
        ident = Identity(subject=f"{self.idp_entity_id}|{user_id}",
                         display_name=self._first(attrs, self.display_name_attr) or username,
                         username=username, user_id=user_id,
                         raw_groups=[str(g) for g in attrs.get(self.groups_attr) or []])
        self._seen.add(ident, {username, nameid})
        return ident

    # -- contratto IdentityProvider -------------------------------------------------

    def login(self, username: str, password: str) -> Identity:
        raise IdentityError("con SAML il login avviene via SSO: /identity/saml/login")

    def resolve_roles(self, identity: Identity) -> set[str]:
        if self.roles_source == "ldap":
            return self.directory.resolve_roles(identity)
        roles = {self.role_map[g.casefold()] for g in identity.raw_groups
                 if g.casefold() in self.role_map}
        return roles | {identity.principal}

    def groups_changed(self) -> bool:
        return self.directory.groups_changed() if self.roles_source == "ldap" else False

    def check_role_map(self) -> list[str]:
        return self.directory.check_role_map() if self.roles_source == "ldap" else []

    def clear_cache(self) -> None:
        if self.roles_source == "ldap":
            self.directory.clear_cache()

    def find_user(self, username: str) -> Identity:
        if self.roles_source == "ldap":
            return self.directory.find_user(username)
        entry = self._seen.get(username)
        if entry:
            return Identity(subject=entry["subject"], username=username,
                            display_name=entry.get("display_name", username),
                            user_id=entry["user_id"])
        raise IdentityError(f"utente non ancora visto da wikibridge: {username} "
                            "(deve fare login una volta)")
