"""CLI di wikibridge.

Comandi:
  project create|list|switch|delete   gestione progetti
  search <query>                      ricerca FTS nel progetto
  index                               ricostruisce indice FTS + index.md
  import <url> [...]                  import da wiki esterna   (fase P2+)
  sync <import-id>                    sync incrementale        (fase P2+)
  refresh <wiki-path>                 refresh puntuale pagina  (fase P2+)
  ingest <file|dir> [...]             import documenti         (fase P5)
  auth add|list|remove                profili di autenticazione(fase P2)
  serve                               server HTTP :8775        (fase P8)
  skill ensure-llmwiki                verifica/installa llm-wiki (fase P7)
"""
from __future__ import annotations

import argparse
import json
import sys

from .config import load_config
from .projects import ProjectStore


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=1))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="wikibridge",
                                 description="Import di wiki e documenti nella LLM Wiki locale")
    ap.add_argument("--project", help="slug del progetto (default: attivo)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_proj = sub.add_parser("project", help="gestione progetti")
    proj_sub = p_proj.add_subparsers(dest="proj_cmd", required=True)
    pc = proj_sub.add_parser("create")
    pc.add_argument("name")
    pc.add_argument("--description", default="")
    proj_sub.add_parser("list")
    ps = proj_sub.add_parser("switch")
    ps.add_argument("slug")
    pd = proj_sub.add_parser("delete")
    pd.add_argument("slug")

    p_search = sub.add_parser("search", help="ricerca full-text")
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=20)

    sub.add_parser("index", help="ricostruisce indice FTS e index.md")

    p_imp = sub.add_parser("import", help="importa da una wiki esterna")
    p_imp.add_argument("url")
    p_imp.add_argument("--depth", type=int, default=1)
    p_imp.add_argument("--scope", choices=["page", "subtree", "space"],
                       default="page")
    p_imp.add_argument("--auth-profile")
    p_imp.add_argument("--adapter",
                       choices=["auto", "generic", "mediawiki", "confluence", "notion"],
                       default="auto")
    p_imp.add_argument("--max-pages", type=int)
    p_imp.add_argument("--max-depth", type=int)
    p_imp.add_argument("--include", action="append", default=[])
    p_imp.add_argument("--exclude", action="append", default=[])
    p_imp.add_argument("--no-catalog", action="store_true",
                       help="salta la catalogazione LLM")

    p_sync = sub.add_parser("sync", help="sync incrementale di un import")
    p_sync.add_argument("import_id")

    p_ref = sub.add_parser("refresh", help="refresh puntuale di una pagina")
    p_ref.add_argument("wiki_path", help="es. sources/mia-pagina.md")

    p_ing = sub.add_parser("ingest", help="importa documenti locali")
    p_ing.add_argument("path")
    p_ing.add_argument("--recursive", action="store_true")
    p_ing.add_argument("--no-catalog", action="store_true")

    p_auth = sub.add_parser("auth", help="profili di autenticazione")
    auth_sub = p_auth.add_subparsers(dest="auth_cmd", required=True)
    aa = auth_sub.add_parser("add")
    aa.add_argument("name")
    aa.add_argument("--type", required=True,
                    choices=["none", "bearer", "basic", "header", "cookie", "oauth2"])
    auth_sub.add_parser("list")
    ar = auth_sub.add_parser("remove")
    ar.add_argument("name")

    sub.add_parser("serve", help="avvia il server HTTP")

    p_skill = sub.add_parser("skill", help="gestione skill llm-wiki")
    skill_sub = p_skill.add_subparsers(dest="skill_cmd", required=True)
    skill_sub.add_parser("ensure-llmwiki")

    args = ap.parse_args(argv)
    cfg = load_config()
    store = ProjectStore(cfg)

    if args.cmd == "project":
        if args.proj_cmd == "create":
            proj = store.create(args.name, args.description)
            _print({"created": proj.to_dict()})
        elif args.proj_cmd == "list":
            active = store.active_slug()
            _print([{**p.to_dict(), "active": p.slug == active}
                    for p in store.list()])
        elif args.proj_cmd == "switch":
            proj = store.activate(args.slug)
            _print({"active": proj.slug})
        elif args.proj_cmd == "delete":
            store.delete(args.slug)
            _print({"deleted": args.slug})
        return 0

    if args.cmd == "search":
        from .wiki.search import SearchIndex
        proj = store.get(args.project)
        hits = SearchIndex(proj).query(args.query, limit=args.limit)
        _print({"project": proj.slug, "hits": hits})
        return 0

    if args.cmd == "index":
        from .wiki.search import SearchIndex
        from .wiki.writer import WikiWriter
        proj = store.get(args.project)
        n = SearchIndex(proj).rebuild()
        WikiWriter(proj).rebuild_index()
        _print({"project": proj.slug, "pages": n})
        return 0

    if args.cmd == "import":
        from .importer import start_import_cli
        return start_import_cli(cfg, store, args)

    if args.cmd == "sync":
        from .importer import sync_import_cli
        return sync_import_cli(cfg, store, args)

    if args.cmd == "refresh":
        from .importer import refresh_page_cli
        return refresh_page_cli(cfg, store, args)

    if args.cmd == "ingest":
        from .convert.docs import ingest_cli
        return ingest_cli(cfg, store, args)

    if args.cmd == "auth":
        from .auth import auth_cli
        return auth_cli(cfg, args)

    if args.cmd == "serve":
        from .server import serve
        serve(cfg)
        return 0

    if args.cmd == "skill":
        import subprocess
        from .config import REPO_DIR
        r = subprocess.run([sys.executable,
                            str(REPO_DIR / "scripts" / "ensure_llmwiki.py")])
        return r.returncode

    return 1


if __name__ == "__main__":
    sys.exit(main())
