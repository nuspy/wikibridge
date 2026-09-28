#!/usr/bin/env python3
"""Wrapper CLI per gli agenti (Hermes in WSL, Claude Code, OpenClaw).

Inoltra i comandi al pacchetto wikibridge SENZA bisogno del venv: prova
prima il Python del venv del repo (se eseguibile dalla piattaforma
corrente), altrimenti aggiunge il repo a sys.path e usa l'interprete
corrente — le dipendenze pesanti (markitdown, trafilatura) serviranno
solo ai comandi che le usano davvero.

Comandi extra rispetto alla CLI:
  status         riepilogo progetto attivo, import recenti, _pending/
  ensure-llmwiki verifica/installa la skill llm-wiki sugli agenti
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _venv_python() -> str | None:
    """Python del venv utilizzabile su questa piattaforma (Win o WSL)."""
    win = REPO / ".venv" / "Scripts" / "python.exe"
    posix = REPO / ".venv" / "bin" / "python"
    if os.name == "nt" and win.is_file():
        return str(win)
    if os.name != "nt" and posix.is_file():
        return str(posix)
    return None


def _delegate(argv: list[str]) -> int:
    py = _venv_python()
    if py:
        return subprocess.run([py, "-m", "wikibridge", *argv]).returncode
    sys.path.insert(0, str(REPO))
    from wikibridge.cli import main
    return main(argv)


def _status(argv: list[str]) -> int:
    sys.path.insert(0, str(REPO))
    from wikibridge.config import load_config
    from wikibridge.projects import ProjectStore
    from wikibridge.importer import ImportsLog
    cfg = load_config()
    store = ProjectStore(cfg)
    slug = None
    if "--project" in argv:
        slug = argv[argv.index("--project") + 1]
    proj = store.get(slug)
    imports = ImportsLog(proj).load()
    pending = sorted(p.name for p in proj.pending_dir.glob("*.json"))
    print(json.dumps({
        "active_project": proj.slug,
        "projects": [p.slug for p in store.list()],
        "imports": [{k: r[k] for k in ("id", "url", "state", "pages")}
                    for r in imports[-5:]],
        "pending_catalog_tasks": pending,
        "wiki_index": str(proj.wiki_dir / "index.md"),
    }, ensure_ascii=False, indent=1))
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    cmd, argv = sys.argv[1], sys.argv[2:]
    if cmd == "status":
        return _status(argv)
    if cmd == "ensure-llmwiki":
        script = REPO / "scripts" / "ensure_llmwiki.py"
        return subprocess.run([sys.executable, str(script)]).returncode
    # comandi della CLI: project/import/ingest/search/sync/update/sources/
    # refresh/catalog-apply/catalog-drain/graph/index/auth/serve
    # --project è un'opzione GLOBALE della CLI: va prima del comando
    if "--project" in argv:
        i = argv.index("--project")
        proj = argv[i:i + 2]
        argv = argv[:i] + argv[i + 2:]
        return _delegate([*proj, cmd, *argv])
    return _delegate([cmd, *argv])


if __name__ == "__main__":
    sys.exit(main())
