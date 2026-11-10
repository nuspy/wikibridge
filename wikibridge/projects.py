"""Gestione progetti: la conoscenza è isolata per progetto.

Registry in data/projects.json: {"active": "<slug>", "projects": {slug: {...}}}.
Ogni progetto vive in data/projects/<slug>/ con:
  project.json   metadati
  imports.json   storico import (gestito da importer.py)
  ingests.json   sorgenti file locali registrate (gestito da updater.py)
  index.db       indice FTS5 (search.py)
  wiki/          la LLM Wiki (writer.py): index.md, overview.md, log.md,
                 sources/, entities/, concepts/, syntheses/, meetings/
  graph/         graph.json + graph.html
  _pending/      task di catalogazione per la modalità brain=agent
"""
from __future__ import annotations

import json
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import Config

DEFAULT_SLUG = "default"
_SLUG_RE = re.compile(r"[^a-z0-9-]+")

WIKI_SECTIONS = ("sources", "entities", "concepts", "syntheses", "meetings")


def slugify(name: str) -> str:
    s = _SLUG_RE.sub("-", name.lower().strip()).strip("-")
    return s or "progetto"


@dataclass
class Project:
    slug: str
    name: str
    description: str = ""
    created: str = ""
    acl_enforcement: bool = False
    default_acl_classification: str = "internal"
    history_retention_days: int = 365
    root: Path = field(default=None, repr=False)  # type: ignore[assignment]

    @property
    def wiki_dir(self) -> Path:
        return self.root / "wiki"

    @property
    def graph_dir(self) -> Path:
        return self.root / "graph"

    @property
    def pending_dir(self) -> Path:
        return self.root / "_pending"

    @property
    def index_db(self) -> Path:
        return self.root / "index.db"

    @property
    def imports_json(self) -> Path:
        return self.root / "imports.json"

    @property
    def ingests_json(self) -> Path:
        return self.root / "ingests.json"

    def ensure_layout(self) -> None:
        for d in (self.root, self.graph_dir, self.pending_dir):
            d.mkdir(parents=True, exist_ok=True)
        for section in WIKI_SECTIONS:
            (self.wiki_dir / section).mkdir(parents=True, exist_ok=True)

    def to_dict(self) -> dict:
        return {
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "created": self.created,
            "acl_enforcement": self.acl_enforcement,
        }


class ProjectStore:
    """CRUD dei progetti + progetto attivo. Thread-safe (lock sul registry)."""

    def __init__(self, cfg: Config):
        self._cfg = cfg
        self._lock = threading.Lock()
        self._registry_path = cfg.data_path() / "projects.json"

    # -- registry -----------------------------------------------------------

    def _load(self) -> dict:
        if self._registry_path.is_file():
            return json.loads(self._registry_path.read_text(encoding="utf-8"))
        return {"active": DEFAULT_SLUG, "projects": {}}

    def _save(self, reg: dict) -> None:
        self._registry_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._registry_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(reg, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(self._registry_path)

    @contextmanager
    def _write_locked(self):
        """Scritture sul registro: lock nel processo e tra processi (server,
        CLI, agenti in WSL), così due modifiche non si cancellano a vicenda."""
        from .filelock import file_lock
        with self._lock, file_lock(Path(str(self._registry_path) + ".lock")):
            yield

    def _project_root(self, slug: str) -> Path:
        return self._cfg.data_path() / "projects" / slug

    def _from_record(self, slug: str, rec: dict) -> Project:
        return Project(slug=slug, name=rec.get("name", slug),
                       description=rec.get("description", ""),
                       created=rec.get("created", ""),
                       # non impostato nel progetto = default globale
                       acl_enforcement=bool(rec.get("acl_enforcement",
                                                    self._cfg.acl_enforcement)),
                       default_acl_classification=self._cfg.default_acl_classification,
                       history_retention_days=self._cfg.history_retention_days,
                       root=self._project_root(slug))

    # -- API ----------------------------------------------------------------

    def list(self) -> list[Project]:
        with self._lock:
            reg = self._load()
        return [self._from_record(s, r) for s, r in sorted(reg["projects"].items())]

    def get(self, slug: str | None = None) -> Project:
        """Progetto per slug, o quello attivo. Crea 'default' se manca."""
        with self._lock:
            reg = self._load()
        slug = slug or reg.get("active") or DEFAULT_SLUG
        if slug not in reg["projects"]:
            if slug != DEFAULT_SLUG:
                raise KeyError(f"progetto inesistente: {slug}")
            with self._write_locked():            # solo per creare il default
                reg = self._load()
                reg["projects"].setdefault(DEFAULT_SLUG, {
                    "name": "Default",
                    "description": "Progetto predefinito",
                    "created": _now(),
                })
                self._save(reg)
        proj = self._from_record(slug, reg["projects"][slug])
        proj.ensure_layout()
        return proj

    def create(self, name: str, description: str = "") -> Project:
        slug = slugify(name)
        with self._write_locked():
            reg = self._load()
            if slug in reg["projects"]:
                raise ValueError(f"progetto già esistente: {slug}")
            reg["projects"][slug] = {
                "name": name, "description": description, "created": _now(),
            }
            self._save(reg)
            proj = self._from_record(slug, reg["projects"][slug])
        proj.ensure_layout()
        (proj.root / "project.json").write_text(
            json.dumps(proj.to_dict(), ensure_ascii=False, indent=1),
            encoding="utf-8")
        return proj

    def activate(self, slug: str) -> Project:
        with self._write_locked():
            reg = self._load()
            if slug not in reg["projects"]:
                raise KeyError(f"progetto inesistente: {slug}")
            reg["active"] = slug
            self._save(reg)
        return self.get(slug)

    def set_acl_enforcement(self, slug: str, enabled: bool) -> Project:
        """Attiva/disattiva il filtro ACL sulla ricerca per un progetto."""
        if slug == DEFAULT_SLUG:
            self.get(DEFAULT_SLUG)        # il default nasce al primo accesso
        with self._write_locked():
            reg = self._load()
            if slug not in reg["projects"]:
                raise KeyError(f"progetto inesistente: {slug}")
            reg["projects"][slug]["acl_enforcement"] = bool(enabled)
            self._save(reg)
            proj = self._from_record(slug, reg["projects"][slug])
        proj.ensure_layout()
        (proj.root / "project.json").write_text(
            json.dumps(proj.to_dict(), ensure_ascii=False, indent=1),
            encoding="utf-8")
        return proj

    def active_slug(self) -> str:
        with self._lock:
            return self._load().get("active") or DEFAULT_SLUG

    def delete(self, slug: str) -> None:
        """Rimuove dal registry; i file su disco restano (cancellazione manuale)."""
        if slug == DEFAULT_SLUG:
            raise ValueError("il progetto default non si elimina")
        with self._write_locked():
            reg = self._load()
            if slug not in reg["projects"]:
                raise KeyError(f"progetto inesistente: {slug}")
            del reg["projects"][slug]
            if reg.get("active") == slug:
                reg["active"] = DEFAULT_SLUG
            self._save(reg)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
