"""Registro di audit: chi ha fatto cosa, e soprattutto chi ha visto cosa.

Una riga JSON per evento in data/audit/audit-AAAA-MM.jsonl (append-only,
un file al mese, conservazione di audit_retention_months). Serve a
rispondere a domande come "chi ha visto questo documento riservato?":
per ricerche, contesto RAG e pagine vengono registrati i PATH restituiti.

Privacy: la query non viene salvata in chiaro ma come impronta HMAC con
una chiave segreta (secrets/audit.key): due ricerche uguali hanno la
stessa impronta, ma dall'impronta non si risale al testo. Con
audit_query_mode="full" si salva il testo, con "none" niente. I corpi
delle richieste non vengono mai registrati (possono contenere password e
segreti dei profili).

Integrità: ogni riga porta l'hash della precedente ("prev"); `verify()`
trova la prima riga modificata o rimossa. Rileva le manomissioni, non le
impedisce, e non vede la cancellazione di un file intero: per quello
servono permessi del filesystem o l'invio a un sistema esterno (SIEM).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .filelock import LockTimeout, file_lock

log = logging.getLogger("wikibridge.audit")
GENESIS = "0" * 64


class AuditLog:
    def __init__(self, cfg: Config):
        self.enabled = cfg.audit_enabled
        self.dir = Path(cfg.audit_dir or Path(cfg.data_dir) / "audit")
        self.query_mode = cfg.audit_query_mode
        self.retention = cfg.audit_retention_months
        self._key_path = Path(cfg.secrets_dir) / "audit.key"
        self._lock = threading.Lock()
        self._key: bytes | None = None
        self._last: str | None = None       # hash dell'ultima riga scritta da noi
        self._month: str | None = None
        # server e CLI sono processi diversi che scrivono lo stesso file: ogni
        # riga dice chi l'ha scritta, e la catena riparte sempre dalla fine
        # reale del file, non da quella che ricorda il singolo processo
        self._writer = secrets.token_hex(4)

    # -- scrittura ------------------------------------------------------------

    def _file(self, month: str) -> Path:
        return self.dir / f"audit-{month}.jsonl"

    def _hmac_key(self) -> bytes:
        if self._key is None:
            if self._key_path.is_file():
                self._key = bytes.fromhex(self._key_path.read_text().strip())
            else:
                self._key = secrets.token_bytes(32)
                self._key_path.parent.mkdir(parents=True, exist_ok=True)
                self._key_path.write_text(self._key.hex())
        return self._key

    def query_field(self, text: str) -> dict:
        if self.query_mode == "full":
            return {"query": text}
        if self.query_mode == "none" or not text:
            return {}
        digest = hmac.new(self._hmac_key(), text.strip().casefold().encode("utf-8"),
                          hashlib.sha256).hexdigest()[:32]
        return {"query_hmac": digest, "query_len": len(text)}

    @staticmethod
    def _tail(path: Path) -> bytes:
        """Ultima riga non vuota del file, letta dalla fine (file grandi ok)."""
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            buf = b""
            while end > 0:
                step = min(4096, end)
                end -= step
                f.seek(end)
                buf = f.read(step) + buf
                lines = buf.rstrip(b"\r\n").split(b"\n")
                if len(lines) > 1 or end == 0:
                    return lines[-1].rstrip(b"\r")
        return b""

    def _last_hash(self, month: str) -> str:
        """Hash dell'ultima riga del mese (o dei mesi precedenti): la catena
        continua dopo un riavvio e tra processi diversi."""
        for f in sorted(self.dir.glob("audit-*.jsonl"), reverse=True):
            if f.stem.split("-", 1)[1] > month:
                continue
            tail = self._tail(f)
            if tail:
                return hashlib.sha256(tail).hexdigest()
        return GENESIS

    def _purge(self, month: str) -> None:
        y, m = map(int, month.split("-"))
        cutoff = y * 12 + m - self.retention
        for f in self.dir.glob("audit-*.jsonl"):
            fy, fm = map(int, f.stem.split("-", 1)[1].split("-"))
            if fy * 12 + fm <= cutoff:
                f.unlink()
                log.info("audit: rimosso %s (oltre %d mesi)", f.name, self.retention)

    def write(self, action: str, **fields) -> None:
        if not self.enabled:
            return
        now = datetime.now(timezone.utc)
        month = now.strftime("%Y-%m")
        record = {"ts": now.isoformat(timespec="milliseconds"), "action": action,
                  **{k: v for k, v in fields.items() if v is not None}}
        with self._lock:
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
                # lettura della coda e aggiunta sotto un lock tra processi
                # (server e CLI): altrimenti due scritture simultanee
                # romperebbero la catena senza alcuna manomissione
                try:
                    with file_lock(self.dir / "audit.lock", timeout=5, stale_after=30):
                        self._append(record, month)
                except LockTimeout:
                    # meglio una catena da riverificare che un evento perso
                    log.warning("audit: lock non ottenuto, scrivo comunque (%s)", action)
                    self._append(record, month)
            except OSError as e:          # l'audit non deve fermare il servizio
                log.error("audit non scritto (%s): %s", action, e)

    def _append(self, record: dict, month: str) -> None:
        if self._month != month:
            self._purge(month)
            self._month = month
        record["prev"] = self._last_hash(month)
        record["writer"] = self._writer
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with self._file(month).open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        self._last = hashlib.sha256(line.encode("utf-8")).hexdigest()

    # -- lettura ------------------------------------------------------------------

    def search(self, *, since: str = "", user: str = "", action: str = "",
               path: str = "", limit: int = 200) -> list[dict]:
        """Eventi più recenti per primi. path: eventi in cui compare quel
        documento (tra i risultati visti o come pagina aperta)."""
        out: list[dict] = []
        for f in sorted(self.dir.glob("audit-*.jsonl"), reverse=True):
            for line in reversed(f.read_text(encoding="utf-8").splitlines()):
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if since and r.get("ts", "") < since:
                    return out
                if user and user not in (r.get("user") or ""):
                    continue
                if action and r.get("action") != action:
                    continue
                if path and path != r.get("path") and path not in (r.get("paths") or []):
                    continue
                out.append(r)
                if len(out) >= limit:
                    return out
        return out

    def verify(self) -> dict:
        """Ricontrolla la catena di hash su tutti i file, dal più vecchio."""
        prev = None
        last_record: dict = {}
        checked = 0
        for f in sorted(self.dir.glob("audit-*.jsonl")):
            for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                try:
                    r = json.loads(line)
                except ValueError:
                    return {"ok": False, "file": f.name, "line": n, "error": "riga non JSON"}
                if prev is not None and r.get("prev") != prev:
                    return {"ok": False, "file": f.name, "line": n,
                            "error": "catena interrotta: riga modificata o rimossa prima di questa"}
                prev = hashlib.sha256(line.encode("utf-8")).hexdigest()
                last_record = r
                checked += 1
        # l'ultima riga non ha una successiva che la "firmi": se l'ha scritta
        # questo processo, la si confronta con l'hash che ricorda (una coda
        # scritta prima di un riavvio o da un altro processo resta non
        # verificabile: vedi documentation.md)
        if (last_record.get("writer") == self._writer and self._last is not None
                and prev != self._last):
            return {"ok": False, "file": "ultimo", "line": checked,
                    "error": "l'ultima riga non è quella scritta dal server"}
        return {"ok": True, "records": checked}
