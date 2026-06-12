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

    user_agent: str = "wikibridge/0.1 (+https://github.com/nuspy)"

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
            else:
                setattr(cfg, f.name, env)
    return cfg
