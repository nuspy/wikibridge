"""Ingest di documenti locali (singolo file o directory).

markitdown converte PDF/DOCX/PPTX/XLSX/HTML/CSV/JSON/XML in markdown;
md/txt passano diretti. Ogni documento diventa una source
(kind=document) con dedup sha256, indicizzata in FTS e accodata alla
catalogazione come le pagine web.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from ..config import Config
from ..projects import Project, ProjectStore
from ..wiki.writer import WikiWriter

log = logging.getLogger("wikibridge.docs")

MARKITDOWN_EXT = {".pdf", ".docx", ".pptx", ".xlsx", ".xls", ".html",
                  ".htm", ".csv", ".json", ".xml", ".epub", ".msg"}
PLAIN_EXT = {".md", ".markdown", ".txt", ".rst"}
SUPPORTED_EXT = MARKITDOWN_EXT | PLAIN_EXT

MAX_DOC_MB = 50


def convert_document(path: Path) -> str:
    """File → markdown. Solleva ValueError per formati non supportati."""
    ext = path.suffix.lower()
    if ext in PLAIN_EXT:
        return path.read_text(encoding="utf-8", errors="replace")
    if ext not in MARKITDOWN_EXT:
        raise ValueError(f"formato non supportato: {ext}")
    from markitdown import MarkItDown
    md = MarkItDown(enable_plugins=False)
    result = md.convert(str(path))
    return result.text_content or ""


def ingest_path(cfg: Config, project: Project, path: Path, *,
                recursive: bool = False, catalog=None,
                progress=None, permissions: bool = False) -> dict:
    """Importa un file o una directory e la registra come sorgente
    aggiornabile (ingests.json): reingerire lo stesso percorso aggiorna
    le pagine esistenti invece di duplicarle. permissions: legge anche i
    permessi NTFS dei file (ntfs_acl.py). Ritorna il riepilogo."""
    from ..updater import update_files_source, register_paths

    if not path.exists():
        raise FileNotFoundError(str(path))
    targets, errors = register_paths(project, [str(path)], recursive=recursive,
                                     permissions=permissions)
    checked = imported = 0
    for source_id, keys in targets:
        r = update_files_source(cfg, project, source_id, only_keys=keys,
                                catalog=catalog, progress=progress)
        checked += r.checked
        imported += r.added + r.updated + r.renamed + r.unchanged
        errors += r.errors
    writer = WikiWriter(project)
    writer.append_log(f"ingest {path}: {imported} documenti, {len(errors)} errori")
    writer.rebuild_index()
    return {"files": checked, "imported": imported, "skipped": len(errors),
            "errors": errors}


def ingest_cli(cfg: Config, store: ProjectStore, args) -> int:
    project = store.get(args.project)
    catalog = None
    if not args.no_catalog:
        try:
            from ..catalog import make_catalog_hook
            catalog = make_catalog_hook(cfg)
        except ImportError:
            pass

    def progress(state):
        print(f"\r[{state['phase']}] {state['pages']}/{state['max_pages']} "
              f"{state.get('detail', '')[:50]:<50}", end="", flush=True)

    result = ingest_path(cfg, project, Path(args.path),
                         recursive=args.recursive, catalog=catalog,
                         progress=progress,
                         permissions=getattr(args, "permissions", False))
    print()
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0 if not result["errors"] else 1
