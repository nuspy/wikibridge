"""Identity provider OIDC (Entra ID/Azure AD, Okta, Keycloak, Google, ...).

Flusso:
1. il client apre nel browser GET /identity/oidc/login?return_to=...;
2. authorization_url() genera state, nonce e PKCE e rimanda all'IdP;
3. l'IdP torna su /identity/oidc/callback; complete() scambia il code,
   valida l'id_token (firma via JWKS, iss, aud, exp, nonce) e il server
   crea una sessione wikibridge, consegnata SOLO a un return_to ammesso.
In alternativa un client che ha già un id_token per questo client_id lo
scambia con POST /identity/oidc/token.

Ruoli: dal claim dei gruppi (oidc_groups_claim) via identity_role_map. Il
refresh token resta sul server: a ogni risoluzione dei ruoli (TTL della
cache) i gruppi vengono riletti dall'IdP; se l'IdP rifiuta il refresh
(utente disabilitato, sessione revocata) la sessione wikibridge viene
revocata (AccountInactive), come per un account AD disabilitato.

Limiti: senza un directory interrogabile, un utente si può indicare per
nome (permessi personali, partecipanti alle riunioni) solo dopo il suo
primo login (registro identities.json), oppure sempre se l'id utente è
l'email/username (oidc_user_id_claim). Con troppi gruppi Entra ID mette un
rimando a Graph invece dei gruppi ("overage"): usare gli app roles.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse

import requests

from .auth import _resolve
from .config import Config
from .identity import (AccountInactive, Identity, IdentityError, IdentityProvider,
                       SeenUsers)
from .oauth import discover

log = logging.getLogger("wikibridge.oidc")

# solo algoritmi asimmetrici: con HS* un token firmato usando la chiave
# PUBBLICA dell'IdP come segreto passerebbe la verifica
ALGORITHMS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512",
              "ES256", "ES384", "ES512"]
PENDING_TTL_S = 600
LEEWAY_S = 60
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_NAME_CLAIMS = ("email", "preferred_username", "upn")


class OIDCProvider(IdentityProvider):
    name = "oidc"

    def __init__(self, cfg: Config):
        self.issuer = cfg.oidc_issuer.rstrip("/")
        self.client_id = cfg.oidc_client_id
        self._client_secret = cfg.oidc_client_secret
        if not self.issuer or not self.client_id:
            raise ValueError("identity_provider=oidc richiede oidc_issuer e oidc_client_id")
        if not self.issuer.startswith("https://") and not cfg.oidc_allow_insecure:
            raise ValueError("oidc_issuer deve essere https:// (oidc_allow_insecure "
                             "solo per test)")
        self.scopes = cfg.oidc_scopes
        self.redirect_uri = (cfg.oidc_redirect_uri or
                             f"http://127.0.0.1:{cfg.api_port}/identity/oidc/callback")
        self.groups_claim = cfg.oidc_groups_claim
        self.user_id_claim = cfg.oidc_user_id_claim
        self.username_claim = cfg.oidc_username_claim
        self.return_urls = list(cfg.oidc_return_urls or [])
        self.role_map = {str(k).casefold(): v
                         for k, v in (cfg.identity_role_map or {}).items()}
        self._ttl = cfg.entitlement_cache_ttl_s
        self._meta: dict | None = None
        self._jwks = None
        self._lock = threading.Lock()
        self._pending: dict[str, dict] = {}
        self._refresh: dict[str, tuple[str, float]] = {}   # subject -> (rt, quando)
        self._seen = SeenUsers(Path(cfg.data_dir) / "identities.json")

    # -- metadati e chiavi --------------------------------------------------------

    def _metadata(self) -> dict:
        if self._meta is None:
            try:
                meta = discover(self.issuer)
            except requests.RequestException as e:
                raise IdentityError(f"IdP non raggiungibile: {e}") from e
            if meta.get("issuer", "").rstrip("/") != self.issuer:
                raise IdentityError("discovery OIDC: issuer diverso da oidc_issuer")
            self._meta = meta
        return self._meta

    def _signing_key(self, token: str):
        import jwt
        if self._jwks is None:
            self._jwks = jwt.PyJWKClient(self._metadata()["jwks_uri"], cache_keys=True)
        try:
            return self._jwks.get_signing_key_from_jwt(token).key
        except (jwt.PyJWTError, ValueError) as e:     # anche token malformati
            raise IdentityError(f"chiave di firma non trovata: {e}") from e

    # -- flusso browser -----------------------------------------------------------

    def return_to_allowed(self, url: str) -> bool:
        if not url:
            return True
        p = urlparse(url)
        if p.scheme == "http" and p.hostname in _LOOPBACK:
            return True
        return any(url.startswith(prefix) for prefix in self.return_urls)

    def authorization_url(self, return_to: str = "") -> str:
        if not self.return_to_allowed(return_to):
            raise IdentityError("return_to non ammesso")
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        now = time.monotonic()
        with self._lock:
            self._pending = {k: v for k, v in self._pending.items()
                             if now - v["at"] < PENDING_TTL_S}
            self._pending[state] = {"verifier": verifier, "nonce": nonce,
                                    "return_to": return_to, "at": now}
        params = {"response_type": "code", "client_id": self.client_id,
                  "redirect_uri": self.redirect_uri, "scope": self.scopes,
                  "state": state, "nonce": nonce, "code_challenge": challenge,
                  "code_challenge_method": "S256"}
        auth = self._metadata()["authorization_endpoint"]
        return f"{auth}{'&' if '?' in auth else '?'}{urlencode(params)}"

    def complete(self, code: str, state: str) -> tuple[Identity, str]:
        with self._lock:
            pending = self._pending.pop(state, None)     # uso singolo
        if pending is None or time.monotonic() - pending["at"] > PENDING_TTL_S:
            raise IdentityError("state sconosciuto o scaduto")
        tokens = self._token_request({
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": self.redirect_uri,
            "code_verifier": pending["verifier"]})
        ident = self._identity(tokens, nonce=pending["nonce"])
        return ident, pending["return_to"]

    def from_tokens(self, id_token: str, refresh_token: str | None = None) -> Identity:
        """Scambio di un id_token già ottenuto dal client (stesso client_id)."""
        return self._identity({"id_token": id_token, "refresh_token": refresh_token})

    # -- token --------------------------------------------------------------------

    def _token_request(self, data: dict) -> dict:
        data = {**data, "client_id": self.client_id}
        secret = _resolve(self._client_secret) if self._client_secret else ""
        if secret:
            data["client_secret"] = secret
        try:
            r = requests.post(self._metadata()["token_endpoint"], data=data, timeout=20)
        except requests.RequestException as e:
            raise IdentityError(f"IdP non raggiungibile: {e}") from e
        if r.status_code >= 400:
            try:
                err = r.json().get("error", "")
            except ValueError:
                err = ""
            if err in ("invalid_grant", "unauthorized_client", "access_denied"):
                raise AccountInactive(f"l'IdP rifiuta la sessione ({err})")
            raise IdentityError(f"token endpoint: HTTP {r.status_code} {err}")
        return r.json()

    def _validate(self, id_token: str, nonce: str | None) -> dict:
        import jwt
        try:
            claims = jwt.decode(
                id_token, self._signing_key(id_token), algorithms=ALGORITHMS,
                audience=self.client_id, issuer=self.issuer, leeway=LEEWAY_S,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]})
        except (jwt.PyJWTError, TypeError, ValueError) as e:
            raise IdentityError(f"id_token non valido: {e}") from e
        if nonce is not None and claims.get("nonce") != nonce:
            raise IdentityError("id_token non valido: nonce diverso")
        return claims

    def _identity(self, tokens: dict, nonce: str | None = None) -> Identity:
        id_token = tokens.get("id_token")
        if not id_token:
            raise IdentityError("l'IdP non ha restituito un id_token (scope openid?)")
        claims = self._validate(id_token, nonce)
        if "_claim_names" in claims and self.groups_claim in claims["_claim_names"]:
            log.warning("gruppi omessi dal token (overage) per %s: usare gli app "
                        "roles o ridurre i gruppi emessi", claims.get("sub"))
        user_id = str(claims.get(self.user_id_claim) or claims["sub"])
        if self.user_id_claim in _NAME_CLAIMS:
            user_id = user_id.casefold()
        username = str(claims.get(self.username_claim) or claims.get("email")
                       or claims["sub"])
        groups = claims.get(self.groups_claim) or []
        ident = Identity(subject=f"{self.issuer}|{claims['sub']}",
                         display_name=str(claims.get("name") or username),
                         username=username, user_id=user_id,
                         raw_groups=[str(g) for g in groups])
        if tokens.get("refresh_token"):
            with self._lock:
                self._refresh[ident.subject] = (tokens["refresh_token"], time.monotonic())
        self._register(ident, claims)
        return ident

    # -- contratto IdentityProvider -------------------------------------------------

    def login(self, username: str, password: str) -> Identity:
        raise IdentityError("con OIDC il login avviene via SSO: /identity/oidc/login")

    def resolve_roles(self, identity: Identity) -> set[str]:
        """Gruppi dal token; alla scadenza del TTL li rilegge con il refresh
        token (l'IdP può anche rifiutarlo: AccountInactive)."""
        with self._lock:
            entry = self._refresh.get(identity.subject)
        if entry and time.monotonic() - entry[1] >= self._ttl:
            fresh = self._token_request({"grant_type": "refresh_token",
                                         "refresh_token": entry[0]})
            if fresh.get("id_token"):
                identity.raw_groups = self._identity(
                    {"refresh_token": fresh.get("refresh_token") or entry[0],
                     "id_token": fresh["id_token"]}).raw_groups
            else:
                with self._lock:
                    self._refresh[identity.subject] = (
                        fresh.get("refresh_token") or entry[0], time.monotonic())
        roles = {self.role_map[g.casefold()] for g in identity.raw_groups
                 if g.casefold() in self.role_map}
        return roles | {identity.principal}

    def expire_claims(self, subject: str | None = None) -> None:
        """Forza la rilettura dei gruppi dall'IdP alla prossima risoluzione."""
        with self._lock:
            for s, (rt, _) in list(self._refresh.items()):
                if subject is None or s == subject:
                    self._refresh[s] = (rt, float("-inf"))

    def clear_cache(self) -> None:
        self.expire_claims()

    def find_user(self, username: str) -> Identity:
        key = username.casefold()
        entry = self._seen.get(key)
        if entry:
            return Identity(subject=entry["subject"], username=username,
                            display_name=entry.get("display_name", username),
                            user_id=entry["user_id"])
        if self.user_id_claim in _NAME_CLAIMS:
            return Identity(subject=f"{self.issuer}|{key}", username=username,
                            display_name=username, user_id=key)
        raise IdentityError(f"utente non ancora visto da wikibridge: {username} "
                            "(deve fare login una volta)")

    # -- registro degli utenti visti ----------------------------------------------

    def _register(self, ident: Identity, claims: dict) -> None:
        """Nomi con cui indicare l'utente (permessi personali, partecipanti).
        Un'email non verificata non conta: un ospite potrebbe dichiarare
        quella di un collega."""
        names = {ident.username}
        for c in _NAME_CLAIMS:
            if not claims.get(c):
                continue
            if c == "email" and claims.get("email_verified") is False:
                continue
            names.add(str(claims[c]))
        self._seen.add(ident, names)
