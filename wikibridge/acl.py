"""Politica ACL per riferimento.

Un blocco (pagina source, o segmento di entità che ne deriva) non porta
con sé la lista completa delle autorizzazioni. Porta un acl_id, che
rimanda a una chiave (sorgente, principal propri, classificazione)
condivisa da tutti i blocchi con le stesse autorizzazioni. Le clausole di
livello superiore stanno in acl.json del progetto e vengono lette a query
time:

- gate di progetto: chi non lo soddisfa non vede nemmeno che il progetto
  esiste;
- collezioni: un insieme di sorgenti (import, cartelle, liste di file) con
  un unico array di principal e una classificazione. Cambiarne i principal
  è una scrittura su acl.json, non una riscrittura dell'indice: ha effetto
  alla richiesta successiva.

Visibilità (modello a intersezione, un figlio può solo restringere):
  admin                                  → tutto
  clearance utente < classificazione     → no (max tra documento e collezione)
  clausole = [collezione?] + [principal propri del documento?]
  nessuna clausola                       → solo se "public" (default-deny)
  altrimenti                             → ogni clausola ha un principal in comune

Principal: ruoli (da identity_role_map), "user:<objectGUID>" per le
autorizzazioni a persona singola, "clearance:<livello>" per il livello di
sensibilità massimo che l'utente può vedere.
"""
from __future__ import annotations

import json
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from .projects import Project

ADMIN_ROLE = "admin"
LEVELS = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}


def level(classification: str | None) -> int:
    # classificazione sconosciuta = massima: meglio un falso diniego che un leak
    return LEVELS.get(classification or "internal", LEVELS["restricted"])


def user_clearance(principals: frozenset[str], default: str) -> int:
    levels = [level(p.split(":", 1)[1]) for p in principals
              if p.startswith("clearance:") and p.split(":", 1)[1] in LEVELS]
    return max([level(default), *levels])


def acl_key(source_id: str, own: list[str] | None, classification: str | None) -> str:
    return json.dumps([source_id or "", sorted(set(own or [])),
                       classification or "internal"])


@dataclass
class AclPolicy:
    path: Path
    version: int = 0
    gate: list[str] = field(default_factory=list)          # vuoto = nessun gate
    collections: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def load(cls, project: Project) -> "AclPolicy":
        path = project.root / "acl.json"
        if not path.is_file():
            return cls(path=path)
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(path=path, version=int(data.get("version", 0)),
                   gate=list(data.get("gate", [])),
                   collections=dict(data.get("collections", {})))

    def save(self) -> None:
        self.version += 1
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": self.version, "gate": self.gate,
                                   "collections": self.collections},
                                  ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def collection_of(self, source_id: str) -> dict | None:
        for coll in self.collections.values():
            if source_id and source_id in coll.get("sources", []):
                return coll
        return None

    def set_collection(self, coll_id: str, *, principals: list[str],
                       classification: str, sources: list[str]) -> None:
        if classification not in LEVELS:
            raise ValueError(f"classificazione sconosciuta: {classification}")
        for other_id, other in self.collections.items():
            clash = set(sources) & set(other.get("sources", []))
            if other_id != coll_id and clash:
                raise ValueError(f"sorgenti già nella collezione {other_id}: "
                                 f"{sorted(clash)}")
        self.collections[coll_id] = {"principals": sorted(set(principals)),
                                     "classification": classification,
                                     "sources": sorted(set(sources))}

    def gate_ok(self, principals: frozenset[str]) -> bool:
        return not self.gate or bool(set(self.gate) & principals)

    def key_visible(self, key: str, principals: frozenset[str],
                    clearance: int) -> bool:
        source_id, own, classification = json.loads(key)
        coll = self.collection_of(source_id)
        lvl = level(classification)
        if coll:
            lvl = max(lvl, level(coll.get("classification")))
        if lvl > clearance:
            return False
        clauses = []
        if coll and coll.get("principals"):
            clauses.append(set(coll["principals"]))
        if own:
            clauses.append(set(own))
        if not clauses:
            return classification == "public"
        return all(c & principals for c in clauses)


# A(P): insieme degli acl_id visibili, calcolato una volta per (indice,
# versione politica, numero di acl, principal, clearance) e riusato.
_CACHE: "OrderedDict[tuple, frozenset[int]]" = OrderedDict()
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 512


class Access:
    """Cosa può vedere chi fa la richiesta, in un progetto."""

    def __init__(self, project: Project, *, principals: frozenset[str] | None,
                 is_admin: bool, enforce: bool, default_clearance: str = "internal"):
        self.project = project
        self.principals = frozenset(principals or ())
        self.is_admin = is_admin
        self.enforce = enforce
        self.default_clearance = default_clearance
        self.policy = AclPolicy.load(project)

    @property
    def unrestricted(self) -> bool:
        return self.is_admin or not self.enforce

    def project_visible(self) -> bool:
        return self.unrestricted or self.policy.gate_ok(self.principals)

    def acl_ids(self, index) -> frozenset[int] | None:
        """None = nessun filtro."""
        if self.unrestricted:
            return None
        if not self.policy.gate_ok(self.principals):
            return frozenset()
        clearance = user_clearance(self.principals, self.default_clearance)
        sets = index.acl_sets()
        ck = (str(index.db_path), self.policy.version, len(sets),
              self.principals, clearance)
        with _CACHE_LOCK:
            hit = _CACHE.get(ck)
            if hit is not None:
                _CACHE.move_to_end(ck)
                return hit
        visible = frozenset(i for i, key in sets
                            if self.policy.key_visible(key, self.principals, clearance))
        with _CACHE_LOCK:
            _CACHE[ck] = visible
            while len(_CACHE) > _CACHE_MAX:
                _CACHE.popitem(last=False)
        return visible

    def visible_pages(self, index) -> set[str] | None:
        """Pagine con almeno un contenuto visibile (source visibili, entità
        con almeno un segmento da una source visibile). None = tutte."""
        ids = self.acl_ids(index)
        return None if ids is None else index.pages_for_acl(ids)
