"""Lock tra processi su file condivisi (server, CLI, agenti).

I lock di sistema (fcntl su Linux, msvcrt su Windows) non si vedono tra
un processo Windows e uno in WSL che lavorano sugli stessi file di
/mnt/c: qui il lock è un file creato in modo atomico (O_CREAT|O_EXCL),
che entrambi i mondi vedono allo stesso modo sul filesystem NTFS.

Un processo che muore tenendo il lock lo lascia sul disco: dopo
stale_after secondi il lock è considerato abbandonato e viene rimosso.
Il file contiene chi lo tiene (host, pid, ora), per la diagnosi.

Windows: un file appena cancellato mentre un altro processo lo ha aperto
resta "in cancellazione" per un istante, e in quel momento crearlo,
leggerne l'età o cancellarlo dà "accesso negato" (PermissionError) invece
di "esiste"/"non esiste". Qui significa solo "occupato, riprova": trattarlo
come errore faceva perdere scritture sotto carico.
"""
from __future__ import annotations

import json
import os
import socket
import time
from contextlib import contextmanager
from pathlib import Path


class LockTimeout(RuntimeError):
    """Il lock è tenuto da un altro processo oltre il tempo di attesa."""

    def __init__(self, path: Path, holder: str):
        super().__init__(f"risorsa occupata ({path.name}): {holder}")
        self.holder = holder


def _holder(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return "?"


@contextmanager
def file_lock(path: Path, *, timeout: float = 30.0, stale_after: float = 120.0,
              poll: float = 0.05):
    """Lock esclusivo su `path` (di solito <file>.lock). timeout=0: non
    attende, solleva LockTimeout se occupato."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    info = json.dumps({"host": socket.gethostname(), "pid": os.getpid(),
                       "since": time.strftime("%Y-%m-%dT%H:%M:%S")})
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(info)
            break
        except FileExistsError:
            try:
                age = time.time() - path.stat().st_mtime
            except FileNotFoundError:
                continue                       # liberato nel frattempo
            except PermissionError:
                age = 0.0                      # in cancellazione: occupato
            if age > stale_after:
                _unlink(path)                  # abbandonato da un processo morto
                continue
        except PermissionError:
            pass                               # in cancellazione: occupato
        if time.monotonic() >= deadline:
            raise LockTimeout(path, _holder(path))
        time.sleep(poll)
    try:
        yield
    finally:
        _unlink(path)


def _unlink(path: Path, attempts: int = 40) -> None:
    """Cancella il lock; se un altro processo lo tiene aperto in quel
    momento (sta leggendo chi lo possiede), riprova per un paio di secondi
    invece di lasciarlo lì fino a stale_after."""
    for _ in range(attempts):
        try:
            path.unlink()
            return
        except FileNotFoundError:
            return
        except PermissionError:
            time.sleep(0.05)


def touch(path: Path) -> None:
    """Rinnova un lock tenuto a lungo (lavori lunghi), perché non sembri
    abbandonato."""
    try:
        os.utime(path)
    except OSError:
        pass
