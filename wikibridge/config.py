"""Configurazione di wikibridge.

Stesso pattern di call-bridge: dataclass con default sensati, sovrascritti
da config.json nella radice del repo e poi dalle variabili d'ambiente
WIKIBRIDGE_<CAMPO_MAIUSCOLO>. I percorsi sono assoluti e calcolati dalla
posizione del pacchetto, così CLI/skill/server vedono gli stessi dati sia
da Windows sia da WSL (/mnt/c/...).
"""
from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_DIR / "config.json"


@dataclass
class Config:
    # Server HTTP per la GUI (sezione Conoscenza del Control Center).
    api_host: str = "127.0.0.1"
    api_port: int = 8775
    api_auth_token: str = ""          # vuoto = nessuna auth (solo loopback)

    # Radici dati (override per spostare i dati fuori dal repo).
    data_dir: str = str(REPO_DIR / "data")
    secrets_dir: str = str(REPO_DIR / "secrets")

    # Cervello per la catalogazione: "openai_http" (LM Studio), "agent"
    # (l'agente ospite catalogherà via _pending/), "none".
    brain_mode: str = "openai_http"
    lmstudio_url: str = "http://127.0.0.1:1234/v1"
    lmstudio_model: str = ""          # vuoto = primo modello caricato
    brain_max_tokens: int = 3000
    catalog_chunk_chars: int = 10000

    # Limiti di import (default; ogni import può sovrascriverli).
    max_depth: int = 2
    hard_max_depth: int = 3
    max_pages: int = 200
    max_page_mb: float = 2.0
    max_total_mb: float = 200.0
    max_attachments_mb: float = 0.0   # 0 = non scaricare allegati
    rate_limit_rps: float = 1.0
    request_timeout_s: int = 30
    respect_robots: bool = True
    max_runtime_min: int = 30

    # Autenticazione interattiva: con True un token mancante/scaduto apre
    # il browser per il login OAuth/SSO (sulla macchina locale); con False
    # l'import fallisce con l'istruzione `auth login` (ambienti headless).
    auth_interactive: bool = True
    auth_login_timeout_s: int = 300

    user_agent: str = "wikibridge/0.1 (+https://github.com/nuspy)"

    # Identità di chi interroga l'API (diverso da AuthProfileStore, che
    # sono le credenziali dell'importer verso le sorgenti esterne).
    # "none" = nessuna integrazione enterprise, comportamento invariato.
    identity_provider: str = "none"       # none | ldap
    identity_role_map: dict = field(default_factory=dict)  # gruppo nativo -> ruolo ACL

    # LDAP / Active Directory
    ldap_url: str = ""                    # ldap://... o ldaps://...
    ldap_bind_dn: str = ""
    ldap_bind_password: str = ""          # valore letterale o "env:VAR"
    ldap_base_dn: str = ""
    ldap_user_filter: str = "(sAMAccountName={username})"
    ldap_group_attr: str = "memberOf"
    ldap_start_tls: bool = False          # StartTLS su ldap://
    ldap_ca_file: str = ""                # CA per verificare il certificato del DC
    ldap_allow_insecure: bool = False     # ldap:// in chiaro: SOLO per test

    # Sessioni utente (modalità con identity provider): il login LDAP avviene
    # una volta, poi le richieste usano un token di sessione revocabile.
    session_idle_min: int = 30
    session_max_hours: int = 8
    login_max_failures_user: int = 5      # tenerlo SOTTO la soglia di blocco AD
    login_max_failures_ip: int = 20
    login_window_s: int = 300

    # Esposizione in rete: con un identity provider attivo e api_host non
    # loopback serve TLS (certificato qui o reverse proxy TLS davanti).
    api_tls_cert: str = ""
    api_tls_key: str = ""
    behind_tls_proxy: bool = False
    cors_origins: list = field(default_factory=list)  # vuoto + identity = nessun CORS

    # Cache dei ruoli risolti dal provider (evita un round-trip LDAP per
    # ogni ricerca) e filtro ACL sui blocchi di conoscenza.
    entitlement_cache_ttl_s: int = 300
    default_acl_classification: str = "internal"
    # livello massimo visibile a un utente autenticato senza un ruolo
    # "clearance:<livello>" (public < internal < confidential < restricted)
    default_clearance: str = "internal"
    acl_enforcement: bool = False         # default globale; Project può sovrascrivere

    def data_path(self) -> Path:
        return Path(self.data_dir)

    def secrets_path(self) -> Path:
        return Path(self.secrets_dir)


def load_config(path: Path | None = None) -> Config:
    """Config = default <- config.json <- env WIKIBRIDGE_*."""
    cfg = Config()
    p = path or CONFIG_PATH
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise SystemExit(f"config.json non valido ({p}): {e}")
        for f in dataclasses.fields(cfg):
            if f.name in data:
                setattr(cfg, f.name, data[f.name])
    for f in dataclasses.fields(cfg):
        env = os.environ.get(f"WIKIBRIDGE_{f.name.upper()}")
        if env is not None:
            cur = getattr(cfg, f.name)
            if isinstance(cur, bool):
                setattr(cfg, f.name, env.lower() in ("1", "true", "yes"))
            elif isinstance(cur, int):
                setattr(cfg, f.name, int(env))
            elif isinstance(cur, float):
                setattr(cfg, f.name, float(env))
            elif isinstance(cur, (dict, list)):
                setattr(cfg, f.name, json.loads(env))
            else:
                setattr(cfg, f.name, env)
    return cfg
