"""Profili di autenticazione uniformi.

secrets/auth_profiles.json: {"profiles": {"<nome>": {"type": ..., campi}}}.
Ogni valore può essere "env:NOME_VAR" (indirection: il segreto sta in una
variabile d'ambiente, non su disco). Gli adapter non vedono mai le
credenziali: apply_profile() configura la requests.Session e basta.

Tipi: none | bearer {token} | basic {username,password} |
header {name,value} | cookie {cookies, domain, login_url} |
oauth2 {issuer | auth_url+token_url, client_id, client_secret?, scopes?,
        redirect_port?, refresh_token?}

Ogni profilo può avere "hosts": [..] — l'importer seleziona da solo il
profilo giusto in base all'host dell'URL, senza --auth-profile.

Flusso oauth2 (il più automatico possibile):
 1. `wikibridge auth login <nome>` apre il browser (Authorization Code +
    PKCE, vedi oauth.py) e salva access+refresh token in .tokens.json;
 2. gli import successivi usano l'access token in cache e lo rinnovano
    in silenzio col refresh token (rotazione inclusa);
 3. se il refresh fallisce e l'ambiente lo consente (auth_interactive),
    riparte da solo il login nel browser.

Per SSO non-OAuth (form di login aziendali) il tipo cookie supporta la
cattura assistita: login nel browser, poi i cookie di sessione vengono
letti dal browser stesso (browser-cookie3) e salvati in secrets/cookies/.
"""
from __future__ import annotations

import getpass
import json
import logging
import os
import sys
import time
import webbrowser
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from urllib.parse import urlparse

import requests

from . import oauth
from .config import Config

log = logging.getLogger("wikibridge.auth")

AUTH_TYPES = ("none", "bearer", "basic", "header", "cookie", "oauth2")


class AuthRequiredError(RuntimeError):
    """La sorgente richiede autenticazione (o il token è scaduto)."""

    def __init__(self, url: str, reason: str):
        super().__init__(f"autenticazione richiesta per {url}: {reason}")
        self.url = url
        self.reason = reason


def _resolve(value: str) -> str:
    if isinstance(value, str) and value.startswith("env:"):
        env = os.environ.get(value[4:])
        if env is None:
            raise RuntimeError(f"variabile d'ambiente mancante: {value[4:]}")
        return env
    return value


class AuthProfileStore:
    def __init__(self, cfg: Config):
        self._cfg = cfg
        self._path = cfg.secrets_path() / "auth_profiles.json"
        self._tokens_cache = cfg.secrets_path() / ".tokens.json"
        self._cookies_dir = cfg.secrets_path() / "cookies"

    def _load(self) -> dict:
        if self._path.is_file():
            return json.loads(self._path.read_text(encoding="utf-8"))
        return {"profiles": {}}

    def _save(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                              encoding="utf-8")

    def names(self) -> list[dict]:
        """Per la GUI: nomi, tipi e host associati, MAI i segreti."""
        return [{"name": n, "type": p.get("type", "?"),
                 "hosts": p.get("hosts", [])}
                for n, p in sorted(self._load()["profiles"].items())]

    def get(self, name: str) -> dict:
        prof = self._load()["profiles"].get(name)
        if prof is None:
            raise KeyError(f"profilo auth inesistente: {name}")
        return prof

    def set(self, name: str, profile: dict) -> None:
        if profile.get("type") not in AUTH_TYPES:
            raise ValueError(f"tipo auth non valido: {profile.get('type')}")
        data = self._load()
        data["profiles"][name] = profile
        self._save(data)

    def remove(self, name: str) -> None:
        data = self._load()
        data["profiles"].pop(name, None)
        self._save(data)

    # -- selezione automatica per host ----------------------------------------

    def find_for(self, url: str) -> str | None:
        """Nome del profilo associato all'host dell'URL, se esiste."""
        host = urlparse(url).netloc.lower()
        if not host:
            return None
        for name, prof in sorted(self._load()["profiles"].items()):
            for h in prof.get("hosts", []):
                h = h.lower()
                if host == h or host.endswith("." + h):
                    return name
        return None

    def bind_host(self, name: str, url: str) -> None:
        """Associa l'host dell'URL al profilo: i prossimi import lo
        selezioneranno da soli."""
        host = urlparse(url).netloc.lower()
        if not host:
            return
        prof = self.get(name)
        hosts = prof.setdefault("hosts", [])
        if host not in (h.lower() for h in hosts):
            hosts.append(host)
            self.set(name, prof)

    # -- applicazione ---------------------------------------------------------

    def apply_profile(self, session: requests.Session,
                      name: str | None) -> None:
        if not name:
            return
        prof = self.get(name)
        t = prof["type"]
        if t == "none":
            return
        if t == "bearer":
            session.headers["Authorization"] = f"Bearer {_resolve(prof['token'])}"
        elif t == "basic":
            session.auth = (_resolve(prof["username"]),
                            _resolve(prof["password"]))
        elif t == "header":
            session.headers[prof["name"]] = _resolve(prof["value"])
        elif t == "cookie":
            self._apply_cookies(session, prof)
        elif t == "oauth2":
            session.headers["Authorization"] = \
                f"Bearer {self._oauth2_token(name, prof)}"

    def _apply_cookies(self, session: requests.Session, prof: dict) -> None:
        raw = _resolve(prof.get("cookies", ""))
        if not raw:
            raise AuthRequiredError(
                prof.get("domain", "?"),
                "profilo cookie senza cookie salvati: esegui "
                "`wikibridge auth login <nome>`")
        if Path(raw).is_file():                # cookies.txt (formato Mozilla)
            jar = MozillaCookieJar(raw)
            jar.load(ignore_discard=True, ignore_expires=True)
            session.cookies.update(jar)
        else:                                   # "k=v; k2=v2"
            for part in raw.split(";"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    session.cookies.set(k.strip(), v.strip())

    # -- oauth2: cache token + refresh + login interattivo ----------------------

    def _load_tokens(self) -> dict:
        if self._tokens_cache.is_file():
            return json.loads(self._tokens_cache.read_text(encoding="utf-8"))
        return {}

    def _store_tokens(self, name: str, tok: dict,
                      fallback_refresh: str = "") -> str:
        cache = self._load_tokens()
        cache[name] = {
            "access_token": tok["access_token"],
            "expires_at": time.time() + int(tok.get("expires_in", 3600)),
            # molti IdP ruotano il refresh token a ogni uso: si salva sempre
            # l'ultimo ricevuto, altrimenti si tiene il precedente
            "refresh_token": tok.get("refresh_token") or fallback_refresh,
        }
        self._tokens_cache.parent.mkdir(parents=True, exist_ok=True)
        self._tokens_cache.write_text(json.dumps(cache), encoding="utf-8")
        return tok["access_token"]

    def _interactive_ok(self) -> bool:
        env = os.environ.get("WIKIBRIDGE_AUTH_INTERACTIVE")
        if env is not None:
            return env.lower() in ("1", "true", "yes")
        return bool(getattr(self._cfg, "auth_interactive", True))

    def _oauth2_token(self, name: str, prof: dict) -> str:
        """Access token: cache -> refresh silenzioso -> login nel browser."""
        entry = self._load_tokens().get(name, {})
        if entry.get("expires_at", 0) > time.time() + 60:
            return entry["access_token"]
        refresh = entry.get("refresh_token") or prof.get("refresh_token", "")
        if refresh:
            try:
                tok = oauth.refresh_grant(prof, _resolve(refresh),
                                          resolve=_resolve)
                return self._store_tokens(name, tok,
                                          fallback_refresh=_resolve(refresh))
            except Exception as e:  # noqa: BLE001 — refresh scaduto/revocato
                log.warning("refresh token del profilo %s non valido: %s",
                            name, e)
        if self._interactive_ok():
            return self._store_tokens(name, self._browser_login(prof))
        raise AuthRequiredError(
            name, f"nessun token valido: esegui `wikibridge auth login {name}`")

    def _browser_login(self, prof: dict) -> dict:
        return oauth.interactive_login(
            prof, resolve=_resolve,
            timeout_s=getattr(self._cfg, "auth_login_timeout_s", 300))

    # -- login interattivo (CLI/GUI) -------------------------------------------

    def login(self, name: str, *, open_browser: bool = True,
              wait_for_user: bool = True,
              capture_only: bool = False) -> dict:
        """Login interattivo del profilo: oauth2 via PKCE, cookie via
        cattura dal browser. Salva token/cookie per gli usi successivi."""
        prof = self.get(name)
        t = prof["type"]
        if t == "oauth2":
            tok = oauth.interactive_login(
                prof, resolve=_resolve, open_browser=open_browser,
                timeout_s=getattr(self._cfg, "auth_login_timeout_s", 300))
            self._store_tokens(name, tok)
            return {"profile": name, "type": t, "logged_in": True,
                    "refresh_token": bool(tok.get("refresh_token")),
                    "expires_in": tok.get("expires_in"),
                    "note": ("" if tok.get("refresh_token") else
                             "nessun refresh token: aggiungi lo scope "
                             "offline_access (o access_type=offline) per il "
                             "rinnovo automatico")}
        if t == "cookie":
            return self._login_cookie(name, prof,
                                      open_browser=open_browser and not capture_only,
                                      wait_for_user=wait_for_user and not capture_only)
        raise ValueError(
            f"login interattivo non applicabile al tipo {t}: usare oauth2 o cookie")

    def _login_cookie(self, name: str, prof: dict, *,
                      open_browser: bool, wait_for_user: bool) -> dict:
        domain = prof.get("domain") or next(iter(prof.get("hosts", [])), "")
        if not domain:
            raise ValueError("profilo cookie senza domain né hosts")
        login_url = prof.get("login_url") or f"https://{domain}/"
        if open_browser:
            print(f"Apro il browser su {login_url} — completa il login SSO.",
                  flush=True)
            webbrowser.open(login_url)
            if wait_for_user and sys.stdin.isatty():
                input("Quando hai finito il login, premi INVIO per catturare "
                      "i cookie... ")
        jar = self._cookies_from_browser(domain)
        if jar is None:
            if not (wait_for_user and sys.stdin.isatty()):
                raise AuthRequiredError(
                    domain, "cattura cookie dal browser non riuscita "
                            "(browser-cookie3 mancante o cookie cifrati): "
                            "esegui `wikibridge auth login` da terminale e "
                            "incolla l'header Cookie")
            raw = input("Incolla l'header Cookie (k=v; k2=v2): ").strip()
            if not raw:
                raise AuthRequiredError(domain, "nessun cookie fornito")
            prof["cookies"] = raw
            self.set(name, prof)
            return {"profile": name, "type": "cookie", "logged_in": True,
                    "source": "manuale", "cookies": len(raw.split(";"))}
        path = self._cookies_dir / f"{name}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        mjar = MozillaCookieJar(str(path))
        for c in jar:
            mjar.set_cookie(c)
        mjar.save(ignore_discard=True, ignore_expires=True)
        prof["cookies"] = str(path)
        self.set(name, prof)
        return {"profile": name, "type": "cookie", "logged_in": True,
                "source": "browser", "cookies": len(mjar),
                "saved": str(path)}

    @staticmethod
    def _cookies_from_browser(domain: str):
        """Cookie jar del dominio letto dai browser installati, o None."""
        try:
            import browser_cookie3
        except ImportError:
            log.warning("browser-cookie3 non installato: "
                        "pip install browser-cookie3")
            return None
        loaders = [browser_cookie3.firefox, browser_cookie3.edge,
                   browser_cookie3.chrome, browser_cookie3.chromium]
        for loader in loaders:
            try:
                jar = loader(domain_name=domain)
                if len(jar):
                    log.info("cookie di %s catturati da %s (%d)",
                             domain, loader.__name__, len(jar))
                    return jar
            except Exception as e:  # noqa: BLE001 — browser assente o store cifrato
                log.debug("cattura cookie via %s fallita: %s",
                          loader.__name__, e)
        return None

    # -- re-login durante un import ---------------------------------------------

    def reauth(self, session: requests.Session, name: str) -> bool:
        """Nuovo login (interattivo se serve) e ri-applicazione del profilo.
        True se la sessione è stata riconfigurata."""
        prof = self.get(name)
        t = prof["type"]
        if t == "oauth2":
            if not self._interactive_ok():
                return False
            # invalida l'access token in cache: _oauth2_token farà
            # refresh o login da capo
            cache = self._load_tokens()
            if name in cache:
                cache[name]["expires_at"] = 0
                self._tokens_cache.write_text(json.dumps(cache),
                                              encoding="utf-8")
            session.headers["Authorization"] = \
                f"Bearer {self._oauth2_token(name, prof)}"
            return True
        if t == "cookie":
            if not self._interactive_ok():
                return False
            self._login_cookie(name, prof, open_browser=True,
                               wait_for_user=True)
            session.cookies.clear()
            self._apply_cookies(session, self.get(name))
            return True
        return False


# -- CLI ------------------------------------------------------------------

_FIELDS_BY_TYPE = {
    "none": [],
    "bearer": ["token"],
    "basic": ["username", "password"],
    "header": ["name", "value"],
    "cookie": ["cookies", "domain", "login_url"],
    "oauth2": ["issuer", "auth_url", "token_url", "client_id",
               "client_secret", "scopes", "redirect_port", "refresh_token"],
}
_SECRET_FIELDS = {"token", "password", "client_secret", "refresh_token"}
# campi che possono restare vuoti (omessi dal profilo)
_OPTIONAL_FIELDS = {"issuer", "auth_url", "token_url", "client_secret",
                    "scopes", "redirect_port", "refresh_token",
                    "cookies", "domain", "login_url"}


def _prompt_profile(auth_type: str) -> dict:
    profile: dict = {"type": auth_type}
    for f in _FIELDS_BY_TYPE[auth_type]:
        opt = " [opzionale]" if f in _OPTIONAL_FIELDS else ""
        prompt = f"{f}{opt} (oppure env:VAR): "
        value = (getpass.getpass(prompt) if f in _SECRET_FIELDS
                 else input(prompt)).strip()
        if not value:
            if f in _OPTIONAL_FIELDS:
                continue
            raise SystemExit(f"campo obbligatorio mancante: {f}")
        if f == "scopes":
            profile[f] = value.split()
        elif f == "redirect_port":
            profile[f] = int(value)
        else:
            profile[f] = value
    hosts = input("hosts della wiki (separati da spazio, per la selezione "
                  "automatica) [opzionale]: ").strip()
    if hosts:
        profile["hosts"] = hosts.split()
    return profile


def auth_cli(cfg: Config, args) -> int:
    store = AuthProfileStore(cfg)
    if args.auth_cmd == "list":
        print(json.dumps(store.names(), ensure_ascii=False, indent=1))
        return 0
    if args.auth_cmd == "remove":
        store.remove(args.name)
        print(json.dumps({"removed": args.name}))
        return 0
    if args.auth_cmd == "add":
        profile = _prompt_profile(args.type)
        store.set(args.name, profile)
        out = {"saved": args.name, "type": args.type}
        if args.type in ("oauth2", "cookie"):
            out["next"] = f"wikibridge auth login {args.name}"
        print(json.dumps(out, ensure_ascii=False))
        return 0
    if args.auth_cmd == "login":
        result = store.login(args.name, open_browser=not args.no_browser)
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0
    if args.auth_cmd == "detect":
        result = oauth.detect_protection(args.url, cfg.user_agent)
        if args.save and result.get("profile"):
            prof = result["profile"]
            if prof["type"] == "oauth2" and not prof.get("client_id"):
                prof["client_id"] = input("client_id (oppure env:VAR): ").strip()
            if prof["type"] == "basic":
                prof["username"] = input("username (oppure env:VAR): ").strip()
                prof["password"] = getpass.getpass("password (oppure env:VAR): ").strip()
            store.set(args.save, prof)
            result["saved"] = args.save
            if prof["type"] in ("oauth2", "cookie"):
                result["next"] = f"wikibridge auth login {args.save}"
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0
    return 1
