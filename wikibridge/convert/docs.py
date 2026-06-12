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
from ..wiki.search import SearchIndex
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
                progress=None) -> dict:
    """Importa un file o una directory. Ritorna il riepilogo."""
    files: list[Path]
    if path.is_dir():
        pattern = "**/*" if recursive else "*"
        files = sorted(p for p in path.glob(pattern)
                       if p.is_file() and p.suffix.lower() in SUPPORTED_EXT)
    elif path.is_file():
        files = [path]
    else:
        raise FileNotFoundError(str(path))

    writer = WikiWriter(project)
    index = SearchIndex(project)
    done, skipped, errors = 0, 0, []
    for i, f in enumerate(files):
        if progress:
            progress({"phase": "converting", "pages": i,
                      "max_pages": len(files), "detail": f.name})
        if f.stat().st_size > MAX_DOC_MB * 1024 * 1024:
            errors.append(f"{f.name}: oltre {MAX_DOC_MB} MB")
            skipped += 1
            continue
        try:
            markdown = convert_document(f)
        except Exception as e:  # noqa: BLE001 — un file rotto non ferma il batch
            errors.append(f"{f.name}: {e}")
            skipped += 1
            continue
        if not markdown.strip():
            errors.append(f"{f.name}: conversione vuota")
            skipped += 1
            continue
        p, changed = writer.write_source(
            title=f.stem, markdown=markdown, url=f.as_uri(),
            source_kind="document", import_id="ingest",
            extra_fm={"original_path": str(f), "format": f.suffix.lower()})
        done += 1
        if changed:
            rel = f"sources/{p.name}"
            fm, body = writer.read_page(rel)
            index.upsert(rel, "sources", fm.get("title", ""), body)
            if catalog:
                catalog(project, rel)

    writer.append_log(f"ingest {path}: {done} documenti, {skipped} saltati")
    writer.rebuild_index()
    return {"files": len(files), "imported": done, "skipped": skipped,
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
                         progress=progress)
    print()
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0 if not result["errors"] else 1
