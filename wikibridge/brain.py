"""Cervello LLM per la catalogazione — tre backend.

- OpenAIHttpBrain: endpoint OpenAI-compatibile (LM Studio). Usato da
  GUI/CLI quando brain_mode=openai_http.
- AgentQueueBrain: NON chiama nessuna API. complete_json ritorna None e
  il cataloguer accoda task in _pending/: sarà l'agente ospite
  (Hermes/Claude — che È l'LLM) a drenarli leggendo i prompt della skill
  e applicando i risultati con `wikibridgectl catalog-apply`.
- NullBrain: nessuna catalogazione (le sources si importano comunque).
"""
from __future__ import annotations

import json
import logging
import re

import requests

from .config import Config

log = logging.getLogger("wikibridge.brain")

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def parse_json_reply(text: str) -> dict | None:
    """JSON da una risposta LLM: tollera fence e testo attorno."""
    text = text.strip()
    m = _FENCE_RE.search(text)
    if m:
        text = m.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


class NullBrain:
    mode = "none"

    def complete_json(self, system: str, user: str, *,
                      max_tokens: int = 3000) -> dict | None:
        return None


class AgentQueueBrain:
    mode = "agent"

    def complete_json(self, system: str, user: str, *,
                      max_tokens: int = 3000) -> dict | None:
        return None      # il cataloguer accoda il task in _pending/


class OpenAIHttpBrain:
    mode = "openai_http"

    def __init__(self, cfg: Config):
        self._url = cfg.lmstudio_url.rstrip("/")
        self._model = cfg.lmstudio_model
        self._max_tokens = cfg.brain_max_tokens

    def _model_id(self) -> str:
        if self._model:
            return self._model
        r = requests.get(f"{self._url}/models", timeout=10)
        r.raise_for_status()
        models = r.json().get("data", [])
        if not models:
            raise RuntimeError("LM Studio: nessun modello caricato")
        self._model = models[0]["id"]
        return self._model

    def complete_json(self, system: str, user: str, *,
                      max_tokens: int = 0) -> dict | None:
        body = {
            "model": self._model_id(),
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": 0.2,
            "max_tokens": max_tokens or self._max_tokens,
        }
        for attempt in (1, 2):
            try:
                r = requests.post(f"{self._url}/chat/completions",
                                  json=body, timeout=300)
                r.raise_for_status()
                text = r.json()["choices"][0]["message"]["content"]
            except (requests.RequestException, KeyError, IndexError) as e:
                log.warning("brain tentativo %d fallito: %s", attempt, e)
                continue
            parsed = parse_json_reply(text)
            if parsed is not None:
                return parsed
            body["messages"].append({"role": "assistant", "content": text})
            body["messages"].append({
                "role": "user",
                "content": "Rispondi SOLO con il JSON richiesto, senza testo attorno."})
        return None


def make_brain(cfg: Config, mode: str | None = None):
    m = mode or cfg.brain_mode
    if m == "openai_http":
        return OpenAIHttpBrain(cfg)
    if m == "agent":
        return AgentQueueBrain()
    return NullBrain()
