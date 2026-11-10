"""CLI di wikibridge.

Comandi:
  project create|list|switch|delete   gestione progetti
  search <query>                      ricerca FTS nel progetto
  index                               ricostruisce indice FTS + index.md
  import <url> [...]                  import da wiki esterna   (fase P2+)
  sync <import-id>                    sync incrementale        (fase P2+)
  update [--import|--ingest|--path]   controllo aggiornamenti on-demand di
                                      rami wiki, cartelle e singoli file
  sources                             sorgenti aggiornabili del progetto
  refresh <wiki-path>                 refresh puntuale pagina  (fase P2+)
  ingest <file|dir> [...]             import documenti         (fase P5)
  auth add|list|remove|login|detect   profili di autenticazione(fase P2)
                                      login = OAuth/SSO dal browser,
                                      detect = riconosce la protezione
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

    sub.add_parser("graph", help="rigenera graph.json e graph.html")

    sub.add_parser("catalog-drain",
                   help="cataloga i task in _pending/ col brain HTTP")

    p_capply = sub.add_parser("catalog-apply",
                              help="applica un risultato di catalogazione (uso skill)")
    p_capply.add_argument("source", help="path wiki, es. sources/x.md")
    p_capply.add_argument("--json", required=True,
                          help="JSON {entities, concepts, relations}")

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
    p_imp.add_argument("--permissions", action="store_true",
                       help="legge anche i permessi di lettura alla fonte "
                            "(Confluence) e li applica ai blocchi")

    p_sync = sub.add_parser("sync", help="sync incrementale di un import")
    p_sync.add_argument("import_id")

    p_upd = sub.add_parser(
        "update", help="controlla e applica gli aggiornamenti delle sorgenti "
                       "(senza opzioni: tutte quelle del progetto)")
    p_upd.add_argument("--import", dest="imports", action="append",
                       metavar="ID", help="solo questo ramo wiki (ripetibile)")
    p_upd.add_argument("--ingest", dest="ingests", action="append",
                       metavar="ID", help="solo questa sorgente file (ripetibile)")
    p_upd.add_argument("--path", dest="paths", action="append", metavar="PATH",
                       help="file o cartella da controllare (ripetibile); "
                            "i nuovi vengono registrati")
    p_upd.add_argument("--all-projects", action="store_true",
                       help="tutte le sorgenti di tutti i progetti (uso cron)")
    p_upd.add_argument("--no-discover", action="store_true",
                       help="non cercare pagine nuove nei rami wiki")
    p_upd.add_argument("--dry-run", action="store_true",
                       help="mostra cosa cambierebbe senza scrivere")
    p_upd.add_argument("--no-catalog", action="store_true")
    p_upd.add_argument("--permissions", action="store_true",
                       help="i --path nuovi leggono anche i permessi NTFS")

    sub.add_parser("sources", help="elenca le sorgenti aggiornabili del progetto")

    sub.add_parser("catalog-rebuild",
                   help="ricataloga tutte le source (rimuove il testo legacy "
                        "non attribuibile; costa una chiamata LLM per source)")

    p_acl = sub.add_parser("acl", help="autorizzazioni (documenti, collezioni, gate)")
    acl_sub = p_acl.add_subparsers(dest="acl_cmd", required=True)

    def _principal_args(p):
        p.add_argument("--role", action="append", default=[], help="ruolo (ripetibile)")
        p.add_argument("--user", action="append", default=[],
                       help="username da autorizzare a persona singola (ripetibile)")

    a_show = acl_sub.add_parser("show", help="politica del progetto (e di una pagina)")
    a_show.add_argument("--path", help="es. sources/x.md")
    a_page = acl_sub.add_parser("page", help="principal propri di un documento "
                                             "(sostituisce quelli esistenti)")
    a_page.add_argument("path")
    _principal_args(a_page)
    a_page.add_argument("--classification",
                        choices=["public", "internal", "confidential", "restricted"])
    a_page.add_argument("--yes", action="store_true",
                        help="conferma una modifica che allarga l'accesso")
    a_coll = acl_sub.add_parser("collection", help="crea/modifica una collezione di sorgenti")
    a_coll.add_argument("id")
    _principal_args(a_coll)
    a_coll.add_argument("--classification", default="internal",
                        choices=["public", "internal", "confidential", "restricted"])
    a_coll.add_argument("--source", action="append", default=[],
                        help="id di import o sorgente file (ripetibile)")
    a_coll.add_argument("--delete", action="store_true")
    a_coll.add_argument("--yes", action="store_true",
                        help="conferma una modifica che allarga l'accesso")
    a_gate = acl_sub.add_parser("gate", help="chi può vedere il progetto (vuoto = tutti)")
    _principal_args(a_gate)
    a_gate.add_argument("--yes", action="store_true",
                        help="conferma una modifica che allarga l'accesso")

    p_his = sub.add_parser("history", help="storico delle versioni di un documento")
    p_his.add_argument("path", help="es. sources/x.md")
    p_his.add_argument("--at", default="",
                       help="data ISO: mostra il documento com'era in quel momento")

    p_syn = sub.add_parser("synthesis", help="salva una sintesi con le fonti da cui deriva")
    p_syn.add_argument("--title", required=True)
    p_syn.add_argument("--source", action="append", required=True,
                       help="documento da cui deriva, es. sources/x.md (ripetibile)")
    p_syn.add_argument("--file", required=True, help="file markdown con il testo")

    p_aud = sub.add_parser("audit", help="registro di audit (chi ha visto e fatto cosa)")
    p_aud.add_argument("--since", default="", help="data ISO, es. 2026-09-01")
    p_aud.add_argument("--user", default="")
    p_aud.add_argument("--action", default="")
    p_aud.add_argument("--path", default="", help="chi ha visto questo documento")
    p_aud.add_argument("--limit", type=int, default=200)
    p_aud.add_argument("--verify", action="store_true",
                       help="verifica l'integrità della catena di hash")
    a_enf = acl_sub.add_parser("enforce", help="attiva/disattiva il filtro ACL")
    a_enf.add_argument("state", choices=["on", "off"])
    a_enf.add_argument("--yes", action="store_true",
                       help="conferma lo spegnimento (rende tutto visibile a tutti)")

    p_ref = sub.add_parser("refresh", help="refresh puntuale di una pagina")
    p_ref.add_argument("wiki_path", help="es. sources/mia-pagina.md")

    p_ing = sub.add_parser("ingest", help="importa documenti locali")
    p_ing.add_argument("path")
    p_ing.add_argument("--recursive", action="store_true")
    p_ing.add_argument("--no-catalog", action="store_true")
    p_ing.add_argument("--permissions", action="store_true",
                       help="legge anche i permessi NTFS dei file (Windows)")

    p_auth = sub.add_parser("auth", help="profili di autenticazione")
    auth_sub = p_auth.add_subparsers(dest="auth_cmd", required=True)
    aa = auth_sub.add_parser("add")
    aa.add_argument("name")
    aa.add_argument("--type", required=True,
                    choices=["none", "bearer", "basic", "header", "cookie", "oauth2"])
    auth_sub.add_parser("list")
    ar = auth_sub.add_parser("remove")
    ar.add_argument("name")
    al = auth_sub.add_parser(
        "login", help="login interattivo dal browser (oauth2/cookie); "
                      "salva token/cookie per gli import successivi")
    al.add_argument("name")
    al.add_argument("--no-browser", action="store_true",
                    help="non aprire il browser: stampa solo l'URL")
    ad = auth_sub.add_parser(
        "detect", help="rileva la protezione di un URL (OAuth/SSO/basic) "
                       "e suggerisce il profilo")
    ad.add_argument("url")
    ad.add_argument("--save", metavar="NOME",
                    help="salva il profilo suggerito con questo nome")

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

    if args.cmd == "graph":
        from .wiki.graph import build_graph
        proj = store.get(args.project)
        _print({"project": proj.slug, **build_graph(proj)})
        return 0

    if args.cmd == "catalog-drain":
        from .catalog import drain_pending
        proj = store.get(args.project)
        _print(drain_pending(cfg, proj))
        return 0

    if args.cmd == "catalog-apply":
        from .catalog import apply_catalog_result
        proj = store.get(args.project)
        result = json.loads(args.json)
        written = apply_catalog_result(proj, args.source, result)
        # task corrispondente in _pending/: rimosso se presente
        from pathlib import Path as _P
        pending = proj.pending_dir / f"{_P(args.source).stem}.json"
        if pending.is_file():
            pending.unlink()
        _print(written)
        return 0

    if args.cmd == "import":
        from .importer import start_import_cli
        return start_import_cli(cfg, store, args)

    if args.cmd == "sync":
        from .importer import sync_import_cli
        return sync_import_cli(cfg, store, args)

    if args.cmd == "update":
        from .updater import check_updates
        catalog = None
        if not (args.no_catalog or args.dry_run):
            from .catalog import make_catalog_hook
            catalog = make_catalog_hook(cfg)
        projects = store.list() if args.all_projects else [store.get(args.project)]
        reports = [check_updates(cfg, p, imports=args.imports,
                                 ingests=args.ingests, paths=args.paths,
                                 discover=not args.no_discover,
                                 dry_run=args.dry_run, catalog=catalog,
                                 permissions=args.permissions)
                   for p in projects]
        _print(reports if args.all_projects else reports[0])
        # exit code utile a cron/agenti: 1 se qualche sorgente ha errori
        return 1 if any(r["totals"].get("errors") for r in reports) else 0

    if args.cmd == "sources":
        from .updater import list_sources
        proj = store.get(args.project)
        _print({"project": proj.slug, "sources": list_sources(proj)})
        return 0

    if args.cmd == "history":
        from .wiki.search import SearchIndex
        index = SearchIndex(store.get(args.project))
        _print(index.document_at(args.path, args.at) if args.at
               else index.history(args.path))
        return 0

    if args.cmd == "synthesis":
        from pathlib import Path as _P
        from .wiki.search import SearchIndex
        from .wiki.writer import WikiWriter
        proj = store.get(args.project)
        writer = WikiWriter(proj)
        try:
            path = writer.write_synthesis(
                title=args.title, sources=args.source,
                body=_P(args.file).read_text(encoding="utf-8"))
        except (ValueError, OSError) as e:
            _print({"error": str(e)})
            return 2
        rel = f"syntheses/{path.name}"
        SearchIndex(proj).upsert_page(rel, "syntheses", *writer.read_page(rel))
        _print({"path": rel, "derived_from": sorted(set(args.source))})
        return 0

    if args.cmd == "audit":
        from .audit import AuditLog
        log_ = AuditLog(cfg)
        if args.verify:
            result = log_.verify()
            _print(result)
            return 0 if result["ok"] else 1
        _print(log_.search(since=args.since, user=args.user, action=args.action,
                           path=args.path, limit=args.limit))
        return 0

    if args.cmd in ("catalog-rebuild", "acl"):
        from .identity import IdentityError
        try:
            if args.cmd == "acl":
                return _acl_cli(cfg, store, args)
            from .catalog import rebuild_catalog
            _print(rebuild_catalog(cfg, store.get(args.project)))
            return 0
        except (ValueError, KeyError, FileNotFoundError, RuntimeError,
                IdentityError) as e:
            _print({"error": str(e)})
            return 2

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


def _acl_cli(cfg, store: ProjectStore, args) -> int:
    from . import acl_admin
    from .identity import build_identity_provider
    proj = store.get(args.project)
    if args.acl_cmd == "show":
        _print(acl_admin.show(proj, args.path))
        return 0
    from .audit import AuditLog
    audit = AuditLog(cfg)

    def confirmed(preview: dict) -> bool:
        if preview["widening"] and not getattr(args, "yes", False):
            _print({"error": "la modifica allarga l'accesso: ripeti con --yes",
                    "preview": preview})
            return False
        return True

    if args.acl_cmd == "enforce":
        preview = acl_admin.preview_enforcement(proj, args.state == "on")
        if not confirmed(preview):
            return 2
        result = store.set_acl_enforcement(proj.slug, args.state == "on").to_dict()
        audit.write("acl_enforcement", user="local-cli", project=proj.slug,
                    preview=preview)
        _print(result)
        return 0
    provider = (build_identity_provider(cfg)
                if args.user and cfg.identity_provider != "none" else None)
    labels: dict[str, str] = {}
    principals = acl_admin.principals_from(args.role, args.user, provider, labels)

    if args.acl_cmd == "page":
        preview = acl_admin.preview_page(proj, args.path, principals, args.classification)
        if not confirmed(preview):
            return 2
        result = acl_admin.set_page_acl(proj, args.path, principals, args.classification,
                                        labels=labels)
        audit.write("acl_page", user="local-cli", project=proj.slug, preview=preview,
                    **result)
        _print(result)
    elif args.acl_cmd == "collection":
        spec = (dict(principals=[], classification="internal", sources=[]) if args.delete
                else dict(principals=principals, classification=args.classification,
                          sources=args.source))
        preview = acl_admin.preview_collection(proj, args.id, **spec)
        if not confirmed(preview):
            return 2
        result = (acl_admin.delete_collection(proj, args.id) if args.delete
                  else acl_admin.set_collection(proj, args.id, **spec, labels=labels))
        audit.write("acl_collection_delete" if args.delete else "acl_collection",
                    user="local-cli", project=proj.slug, collection=args.id,
                    preview=preview)
        _print({**result, "preview": preview})
    elif args.acl_cmd == "gate":
        preview = acl_admin.preview_gate(proj, principals)
        if not confirmed(preview):
            return 2
        result = acl_admin.set_gate(proj, principals, labels=labels)
        audit.write("acl_gate", user="local-cli", project=proj.slug, preview=preview)
        _print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
