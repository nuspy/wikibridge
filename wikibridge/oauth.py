"""OAuth2 interattivo (Authorization Code + PKCE) e rilevamento protezioni.

interactive_login() apre il browser dell'utente sull'authorization endpoint
del provider, riceve il redirect su un mini server di loopback
(http://127.0.0.1:<porta>/callback), scambia il code per i token e li
restituisce. Con PKCE non serve client_secret (client pubblico, RFC 8252).

detect_protection() fa una richiesta anonima a un URL e classifica la
protezione (IdP noto, OAuth generico, form di login SSO, basic auth),
producendo lo scheletro del profilo auth corrispondente.

looks_like_auth_wall() è il rilevatore usato dalla sessione HTTP durante
gli import: riconosce 401/403, redirect verso IdP e pagine di login
servite al posto del contenuto.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import re
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import requests

log = logging.getLogger("wikibridge.oauth")

DEFAULT_REDIRECT_PORT = 8776
DEFAULT_SCOPES = ["openid", "profile", "email", "offline_access"]

_IDP_EXACT = {
    "login.microsoftonline.com", "login.windows.net", "sts.windows.net",
    "accounts.google.com", "id.atlassian.com", "auth.atlassian.com",
}
_IDP_SUFFIXES = (".okta.com", ".oktapreview.com", ".auth0.com",
                 ".onelogin.com", ".pingidentity.com",
                 ".microsoftonline.com", ".duosecurity.com")
_IDP_PREFIXES = ("login.", "sso.", "auth.", "idp.", "adfs.", "keycloak.",
                 "id.", "signin.", "accounts.")

_LOGIN_HINTS = re.compile(
    r"(login|log-in|signin|sign-in|sso|saml|oauth|authorize|userlogin"
    r"|returnto=|redirect_uri=|samlrequest=)", re.IGNORECASE)
_PASSWORD_INPUT = re.compile(r"<input[^>]+type=[\"']?password", re.IGNORECASE)

_PAGE_OK = """<html><head><meta charset="utf-8"><title>wikibridge</title></head>
<body style="font-family:sans-serif;text-align:center;margin-top:4em">
<h2>&#10003; Login completato</h2>
<p>Puoi chiudere questa scheda e tornare a wikibridge.</p></body></html>"""
_PAGE_ERR = """<html><head><meta charset="utf-8"><title>wikibridge</title></head>
<body style="font-family:sans-serif;text-align:center;margin-top:4em">
<h2>&#10007; Login non riuscito</h2><p>{error}</p></body></html>"""


def _is_idp_host(host: str) -> bool:
    host = host.lower()
    return (host in _IDP_EXACT
            or any(host.endswith(s) for s in _IDP_SUFFIXES)
            or any(host.startswith(p) for p in _IDP_PREFIXES))


def looks_like_auth_wall(resp: requests.Response) -> str | None:
    """Motivo se la risposta è un muro di autenticazione, altrimenti None."""
    if resp.status_code == 401:
        return "HTTP 401"
    if resp.status_code == 403 and "WWW-Authenticate" in resp.headers:
        return "HTTP 403 con WWW-Authenticate"
    final = urlparse(resp.url)
    if resp.history:
        orig_host = urlparse(resp.history[0].url).netloc
        if final.netloc != orig_host and _is_idp_host(final.netloc):
            return f"redirect all'IdP {final.netloc}"
    # pagina di login servita al posto del contenuto (SSO/wiki private)
    if ("html" in resp.headers.get("Content-Type", "")
            and _LOGIN_HINTS.search(f"{final.path}?{final.query}")
            and _PASSWORD_INPUT.search(resp.text[:200_000])):
        return f"pagina di login ({final.netloc}{final.path})"
    return None


# -- endpoint -----------------------------------------------------------------

def discover(issuer: str) -> dict:
    """Metadati OIDC da <issuer>/.well-known/openid-configuration."""
    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    resp = requests.get(url, timeout=20)
    resp.raise_for_status()
    return resp.json()


def endpoints_for(prof: dict, resolve=lambda v: v) -> tuple[str, str]:
    """(authorization_endpoint, token_endpoint) del profilo.

    auth_url/token_url espliciti vincono; altrimenti discovery OIDC
    sull'issuer. Il token_url da solo basta per il flusso refresh.
    """
    auth_url, token_url = prof.get("auth_url"), prof.get("token_url")
    if token_url and (auth_url or not prof.get("issuer")):
        return resolve(auth_url or ""), resolve(token_url)
    issuer = prof.get("issuer")
    if not issuer:
        raise ValueError("profilo oauth2 senza issuer né auth_url/token_url")
    meta = discover(resolve(issuer))
    return meta["authorization_endpoint"], meta["token_endpoint"]


# -- flusso interattivo ---------------------------------------------------------

def interactive_login(prof: dict, *, resolve=lambda v: v,
                      open_browser: bool = True,
                      timeout_s: int = 300) -> dict:
    """Authorization Code + PKCE via browser. Ritorna il JSON dei token."""
    auth_url, token_url = endpoints_for(prof, resolve)
    if not auth_url:
        raise ValueError("profilo oauth2 senza auth_url/issuer: "
                         "impossibile il login interattivo")
    client_id = resolve(prof["client_id"])
    port = int(prof.get("redirect_port", DEFAULT_REDIRECT_PORT))
    redirect_uri = f"http://127.0.0.1:{port}/callback"
    scopes = prof.get("scopes") or DEFAULT_SCOPES
    if isinstance(scopes, str):
        scopes = scopes.split()

    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    params = {
        "response_type": "code", "client_id": client_id,
        "redirect_uri": redirect_uri, "scope": " ".join(scopes),
        "state": state, "code_challenge": challenge,
        "code_challenge_method": "S256",
        **prof.get("auth_params", {}),  # es. audience/prompt per Atlassian
    }
    url = f"{auth_url}{'&' if '?' in auth_url else '?'}{urlencode(params)}"

    result: dict = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            result.update({k: v[0] for k, v in
                           parse_qs(parsed.query).items()})
            page = (_PAGE_OK if "error" not in result else
                    _PAGE_ERR.format(error=result.get("error_description")
                                     or result.get("error", "")))
            body = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            done.set()

        def log_message(self, *args):  # silenzia l'access log
            return

    try:
        server = HTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        raise RuntimeError(
            f"porta {port} occupata per il callback OAuth "
            f"(redirect_port nel profilo): {e}") from e
    server.timeout = 1.0

    log.info("login OAuth: %s", url)
    print(f"{'Apro il browser per il login' if open_browser else 'Apri nel browser'}:"
          f"\n  {url}", flush=True)
    if open_browser:
        webbrowser.open(url)
    deadline = time.monotonic() + timeout_s
    try:
        while not done.is_set() and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()

    if not result:
        raise TimeoutError(
            f"login OAuth non completato entro {timeout_s}s")
    if "error" in result:
        raise RuntimeError("login OAuth rifiutato: "
                           + (result.get("error_description")
                              or result["error"]))
    if result.get("state") != state:
        raise RuntimeError("state OAuth non corrispondente (possibile CSRF)")

    data = {
        "grant_type": "authorization_code", "code": result["code"],
        "redirect_uri": redirect_uri, "client_id": client_id,
        "code_verifier": verifier,
    }
    if prof.get("client_secret"):
        data["client_secret"] = resolve(prof["client_secret"])
    resp = requests.post(token_url, data=data, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"scambio code->token fallito "
                           f"({resp.status_code}): {resp.text[:300]}")
    return resp.json()


def refresh_grant(prof: dict, refresh_token: str, *,
                  resolve=lambda v: v) -> dict:
    """Access token (ed eventuale refresh ruotato) dal refresh_token."""
    _, token_url = endpoints_for(prof, resolve)
    data = {
        "grant_type": "refresh_token", "refresh_token": refresh_token,
        "client_id": resolve(prof["client_id"]),
    }
    if prof.get("client_secret"):
        data["client_secret"] = resolve(prof["client_secret"])
    resp = requests.post(token_url, data=data, timeout=30)
    resp.raise_for_status()
    return resp.json()


# -- rilevamento della protezione di un URL -------------------------------------

def _preset_for_idp(host: str, location_url: str) -> dict | None:
    """Scheletro di profilo oauth2 per gli IdP riconosciuti."""
    if host.endswith(".microsoftonline.com") or host == "login.windows.net":
        m = re.search(r"microsoftonline\.com/([^/?]+)", location_url)
        tenant = m.group(1) if m else "organizations"
        return {"provider": "azure",
                "issuer": f"https://login.microsoftonline.com/{tenant}/v2.0",
                "scopes": DEFAULT_SCOPES,
                "note": "registra su Entra ID una app 'Mobile and desktop "
                        "applications' con redirect http://127.0.0.1:8776/callback "
                        "e usa il suo client_id"}
    if host == "accounts.google.com":
        return {"provider": "google",
                "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
                "token_url": "https://oauth2.googleapis.com/token",
                "scopes": ["openid", "email", "profile"],
                "auth_params": {"access_type": "offline", "prompt": "consent"},
                "note": "crea credenziali OAuth di tipo 'Desktop app' su "
                        "Google Cloud Console"}
    if host in ("id.atlassian.com", "auth.atlassian.com"):
        return {"provider": "atlassian",
                "auth_url": "https://auth.atlassian.com/authorize",
                "token_url": "https://auth.atlassian.com/oauth/token",
                "scopes": ["read:confluence-content.all",
                           "read:confluence-space.summary", "offline_access"],
                "auth_params": {"audience": "api.atlassian.com",
                                "prompt": "consent"},
                "note": "app OAuth 2.0 (3LO) su developer.atlassian.com; in "
                        "alternativa per Confluence Cloud basta un profilo "
                        "basic con email + API token"}
    if host.endswith((".okta.com", ".oktapreview.com")):
        return {"provider": "okta", "issuer": f"https://{host}",
                "scopes": DEFAULT_SCOPES,
                "note": "client di tipo 'Native App' con PKCE su Okta"}
    if host.endswith(".auth0.com"):
        return {"provider": "auth0", "issuer": f"https://{host}",
                "scopes": DEFAULT_SCOPES,
                "note": "application di tipo 'Native' su Auth0"}
    return None


def detect_protection(url: str, user_agent: str = "wikibridge") -> dict:
    """Sonda l'URL anonimamente e classifica la protezione.

    Ritorna {url, final_url, status, kind, provider?, profile, note} dove
    profile è lo scheletro pronto per `auth add`/`auth login` (con hosts
    già compilato per la selezione automatica del profilo).
    """
    resp = requests.get(url, timeout=20, allow_redirects=True,
                        headers={"User-Agent": user_agent})
    host = urlparse(url).netloc
    final = urlparse(resp.url)
    out = {"url": url, "final_url": resp.url, "status": resp.status_code,
           "kind": "open", "profile": None, "note": ""}

    wall = looks_like_auth_wall(resp)
    if wall is None:
        out["note"] = "nessuna protezione rilevata: l'URL è leggibile anonimamente"
        return out
    out["wall"] = wall

    www = resp.headers.get("WWW-Authenticate", "").lower()
    if resp.status_code == 401 and www.startswith("basic"):
        out.update(kind="basic",
                   profile={"type": "basic", "hosts": [host]},
                   note="basic auth: completa il profilo con username e password")
        return out

    if final.netloc != host:
        preset = _preset_for_idp(final.netloc, resp.url)
        if preset:
            note = preset.pop("note", "")
            provider = preset.pop("provider")
            out.update(kind="oauth", provider=provider,
                       profile={"type": "oauth2", "hosts": [host],
                                "redirect_port": DEFAULT_REDIRECT_PORT,
                                "client_id": "", **preset},
                       note=note)
            return out

    if "bearer" in www:
        out.update(kind="oauth",
                   profile={"type": "oauth2", "hosts": [host],
                            "redirect_port": DEFAULT_REDIRECT_PORT,
                            "client_id": ""},
                   note="OAuth bearer: chiedi all'amministratore issuer (o "
                        "auth_url/token_url) e client_id, poi `auth login`")
        return out

    out.update(kind="sso_form",
               profile={"type": "cookie", "hosts": [host], "domain": host,
                        "login_url": url},
               note="login SSO via form: `wikibridge auth login <nome>` apre "
                    "il browser e cattura i cookie di sessione dopo il login")
    return out
