"""Profili di autenticazione uniformi.

secrets/auth_profiles.json: {"profiles": {"<nome>": {"type": ..., campi}}}.
Ogni valore può essere "env:NOME_VAR" (indirection: il segreto sta in una
variabile d'ambiente, non su disco). Gli adapter non vedono mai le
credenziali: apply_profile() configura la requests.Session e basta.

Tipi: none | bearer {token} | basic {username,password} |
header {name,value} | cookie {cookies: "k=v; k2=v2" | path cookies.txt} |
oauth2 {token_url, client_id, client_secret, refresh_token, scopes}
"""
from __future__ import annotations

import getpass
import json
import time
from http.cookiejar import MozillaCookieJar
from pathlib import Path

import requests

from .config import Config

AUTH_TYPES = ("none", "bearer", "basic", "header", "cookie", "oauth2")


def _resolve(value: str) -> str:
    if isinstance(value, str) and value.startswith("env:"):
        import os
        env = os.environ.get(value[4:])
        if env is None:
            raise RuntimeError(f"variabile d'ambiente mancante: {value[4:]}")
        return env
    return value


class AuthProfileStore:
    def __init__(self, cfg: Config):
        self._path = cfg.secrets_path() / "auth_profiles.json"
        self._tokens_cache = cfg.secrets_path() / ".tokens.json"

    def _load(self) -> dict:
        if self._path.is_file():
            return json.loads(self._path.read_text(encoding="utf-8"))
        return {"profiles": {}}

    def _save(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                              encoding="utf-8")

    def names(self) -> list[dict]:
        """Per la GUI: nomi e tipi, MAI i segreti."""
        return [{"name": n, "type": p.get("type", "?")}
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
            raw = _resolve(prof["cookies"])
            if Path(raw).is_file():            # cookies.txt (formato Mozilla)
                jar = MozillaCookieJar(raw)
                jar.load(ignore_discard=True, ignore_expires=True)
                session.cookies.update(jar)
            else:                               # "k=v; k2=v2"
                for part in raw.split(";"):
                    if "=" in part:
                        k, v = part.split("=", 1)
                        session.cookies.set(k.strip(), v.strip())
        elif t == "oauth2":
            session.headers["Authorization"] = \
                f"Bearer {self._oauth2_token(name, prof)}"

    def _oauth2_token(self, name: str, prof: dict) -> str:
        """Access token via refresh_token, con cache su disco."""
        cache = {}
        if self._tokens_cache.is_file():
            cache = json.loads(self._tokens_cache.read_text(encoding="utf-8"))
        entry = cache.get(name)
        if entry and entry.get("expires_at", 0) > time.time() + 60:
            return entry["access_token"]
        resp = requests.post(_resolve(prof["token_url"]), data={
            "grant_type": "refresh_token",
            "refresh_token": _resolve(prof["refresh_token"]),
            "client_id": _resolve(prof["client_id"]),
            "client_secret": _resolve(prof.get("client_secret", "")),
        }, timeout=30)
        resp.raise_for_status()
        tok = resp.json()
        cache[name] = {
            "access_token": tok["access_token"],
            "expires_at": time.time() + int(tok.get("expires_in", 3600)),
        }
        self._tokens_cache.parent.mkdir(parents=True, exist_ok=True)
        self._tokens_cache.write_text(json.dumps(cache), encoding="utf-8")
        return tok["access_token"]


# -- CLI ------------------------------------------------------------------

_FIELDS_BY_TYPE = {
    "none": [],
    "bearer": ["token"],
    "basic": ["username", "password"],
    "header": ["name", "value"],
    "cookie": ["cookies"],
    "oauth2": ["token_url", "client_id", "client_secret", "refresh_token"],
}


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
        profile = {"type": args.type}
        for f in _FIELDS_BY_TYPE[args.type]:
            secret = f in ("token", "password", "client_secret", "refresh_token")
            prompt = f"{f} (oppure env:VAR): "
            profile[f] = (getpass.getpass(prompt) if secret
                          else input(prompt)).strip()
        store.set(args.name, profile)
        print(json.dumps({"saved": args.name, "type": args.type}))
        return 0
    return 1
