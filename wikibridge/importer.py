"""Orchestrazione degli import: BFS, limiti, progress, storico, sync.

Un import = job registrato in data/projects/<slug>/imports.json:
{
  "id": "imp-0001", "url", "adapter", "scope", "depth", "limits": {...},
  "auth_profile", "state": running|done|error|cancelled,
  "truncated": bool, "frontier": [url...],   # per riprendere se troncato
  "pages": n, "skipped": n, "errors": [..], "started", "finished",
  "page_index": {url: {"path": "sources/x.md", "revision": "..."}}
}

La BFS visita il seed set dell'adapter e segue PageContent.links finché
profondità/limiti lo consentono. Ogni pagina scritta aggiorna FTS e,
se è attivo un cataloguer, accoda la catalogazione.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Callable

from .adapters import detect_adapter
from .adapters.base import PageRef
from .auth import AuthProfileStore, AuthRequiredError
from .config import Config
from .http import make_session
from .limits import ImportLimits
from .projects import Project, ProjectStore
from .wiki.search import SearchIndex
from .wiki.writer import WikiWriter, parse_frontmatter

log = logging.getLogger("wikibridge.importer")

# Callback opzionale di catalogazione: (project, source_path) -> None.
# Impostata dal modulo catalog (fase P6); None = nessuna catalogazione.
CatalogHook = Callable[[Project, str], None] | None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ImportsLog:
    """Storico import del progetto (imports.json), thread-safe."""

    def __init__(self, project: Project):
        self._path = project.imports_json
        self._lock = threading.Lock()

    def load(self) -> list[dict]:
        if self._path.is_file():
            return json.loads(self._path.read_text(encoding="utf-8"))
        return []

    def upsert(self, record: dict) -> None:
        with self._lock:
            records = self.load()
            for i, r in enumerate(records):
                if r["id"] == record["id"]:
                    records[i] = record
                    break
            else:
                records.append(record)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(records, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(self._path)

    def get(self, import_id: str) -> dict:
        for r in self.load():
            if r["id"] == import_id:
                return r
        raise KeyError(f"import inesistente: {import_id}")

    def next_id(self) -> str:
        return f"imp-{len(self.load()) + 1:04d}"


def run_import(cfg: Config, project: Project, url: str, *,
               depth: int = 1, scope: str = "page",
               limits: ImportLimits | None = None,
               auth_profile: str | None = None,
               adapter_name: str = "auto",
               catalog: CatalogHook = None,
               progress: Callable[[dict], None] | None = None,
               cancel: threading.Event | None = None,
               resume_record: dict | None = None) -> dict:
    """Esegue un import (bloccante). Ritorna il record finale del job."""
    limits = limits or ImportLimits.from_config(cfg)
    depth = min(depth, limits.max_depth)
    cancel = cancel or threading.Event()
    imports_log = ImportsLog(project)
    writer = WikiWriter(project)
    index = SearchIndex(project)

    auth_store = AuthProfileStore(cfg)
    if not auth_profile:
        auth_profile = auth_store.find_for(url)
        if auth_profile:
            log.info("profilo auth selezionato automaticamente per %s: %s",
                     url, auth_profile)
    else:
        # ricorda l'associazione host->profilo: i prossimi import di questo
        # host non avranno bisogno di --auth-profile
        auth_store.bind_host(auth_profile, url)
    session = make_session(cfg, limits)

    rec = resume_record or {
        "id": imports_log.next_id(), "url": url, "adapter": adapter_name,
        "scope": scope, "depth": depth, "limits": limits.to_dict(),
        "auth_profile": auth_profile, "state": "running",
        "truncated": False, "frontier": [], "pages": 0, "skipped": 0,
        "errors": [], "started": _now(), "finished": None, "page_index": {},
    }
    rec["state"] = "running"
    rec["auth_profile"] = auth_profile
    imports_log.upsert(rec)

    def emit(phase: str, detail: str = "") -> None:
        if progress:
            progress({"phase": phase, "pages": rec["pages"],
                      "max_pages": limits.max_pages, "detail": detail})

    reauths = 0

    def try_reauth(reason: str) -> bool:
        """Un re-login (interattivo se serve), al massimo due per job."""
        nonlocal reauths
        if not auth_profile or reauths >= 2:
            return False
        reauths += 1
        emit("auth", f"re-login ({reason})")
        try:
            return auth_store.reauth(session, auth_profile)
        except Exception as e:  # noqa: BLE001
            log.warning("re-login del profilo %s fallito: %s", auth_profile, e)
            return False

    deadline = time.monotonic() + limits.max_runtime_min * 60
    total_bytes = 0
    visited: set[str] = set(rec["page_index"].keys())
    queue: deque[PageRef] = deque()

    try:
        emit("auth" if auth_profile else "resolving", url)
        # può già rinnovare il token o aprire il browser per il login
        auth_store.apply_profile(session, auth_profile)
        session.guard_auth = True
        try:
            adapter = detect_adapter(url, session, limits, forced=adapter_name)
        except AuthRequiredError as e:
            if not try_reauth(e.reason):
                raise
            adapter = detect_adapter(url, session, limits, forced=adapter_name)
        rec["adapter"] = adapter.name

        emit("resolving", url)
        if rec["frontier"]:
            queue = deque(PageRef(id=u, url=u, depth=d)
                          for u, d in rec["frontier"])
        else:
            queue = deque(adapter.resolve(url, scope))
            # resolve() di scope ampi può già superare max_pages
            if len(queue) > limits.max_pages:
                rec["truncated"] = True
                overflow = list(queue)[limits.max_pages:]
                queue = deque(list(queue)[:limits.max_pages])
                rec["frontier"] = [[r.url, r.depth] for r in overflow]

        while queue:
            if cancel.is_set():
                rec["state"] = "cancelled"
                break
            if time.monotonic() > deadline:
                rec["errors"].append("tempo massimo superato")
                rec["truncated"] = True
                break
            if rec["pages"] >= limits.max_pages:
                rec["truncated"] = True
                break

            ref = queue.popleft()
            if ref.url in visited:
                continue
            visited.add(ref.url)
            if not limits.title_allowed(ref.title or ref.url):
                rec["skipped"] += 1
                continue

            emit("fetching", ref.title or ref.url)
            try:
                page = adapter.fetch(ref)
            except AuthRequiredError as e:
                if not try_reauth(e.reason):
                    # la pagina torna in frontier: il resume riparte da qui
                    visited.discard(ref.url)
                    queue.appendleft(ref)
                    raise
                try:
                    page = adapter.fetch(ref)
                except Exception as e2:  # noqa: BLE001
                    rec["errors"].append(f"{ref.url}: {e2}")
                    rec["skipped"] += 1
                    continue
            except Exception as e:  # noqa: BLE001 — una pagina rotta non ferma il job
                rec["errors"].append(f"{ref.url}: {e}")
                rec["skipped"] += 1
                continue

            size = len(page.markdown.encode("utf-8"))
            total_bytes += size
            if total_bytes > limits.max_total_mb * 1024 * 1024:
                rec["errors"].append("budget dimensione totale superato")
                rec["truncated"] = True
                break

            path, changed = writer.write_source(
                title=page.ref.title or page.ref.url,
                markdown=page.markdown, url=page.ref.url,
                source_kind="web", import_id=rec["id"],
                revision=page.revision,
                extra_fm={"adapter": adapter.name})
            rel = f"sources/{path.name}"
            rec["page_index"][page.ref.url] = {
                "path": rel, "revision": page.revision, "depth": ref.depth,
            }
            rec["pages"] += 1
            if changed:
                fm, body = writer.read_page(rel)
                index.upsert(rel, "sources", fm.get("title", ""), body)
                if catalog:
                    catalog(project, rel)

            if ref.depth < depth:
                for link in page.links:
                    if link.url not in visited:
                        queue.append(link)

            if rec["pages"] % 10 == 0:
                imports_log.upsert(rec)

        else:
            rec["frontier"] = []

        if rec["truncated"] and queue:
            rec["frontier"] = [[r.url, r.depth] for r in queue]
        if rec["state"] == "running":
            rec["state"] = "done"
    except AuthRequiredError as e:
        rec["state"] = "auth_required"
        rec["truncated"] = True
        if queue:
            rec["frontier"] = [[r.url, r.depth] for r in queue]
        hint = (f"esegui `wikibridge auth login {auth_profile}`"
                if auth_profile else
                "crea il profilo: `wikibridge auth detect <url> --save <nome>`"
                " poi `wikibridge auth login <nome>`")
        rec["errors"].append(f"{e} — {hint}")
    except Exception as e:  # noqa: BLE001
        log.exception("import fallito")
        rec["state"] = "error"
        rec["errors"].append(str(e))
    finally:
        rec["finished"] = _now()
        imports_log.upsert(rec)
        writer.append_log(
            f"import {rec['id']} {rec['state']}: {rec['pages']} pagine da "
            f"{url} (depth {depth}, scope {scope})")
        writer.rebuild_index()
        emit(rec["state"])
    return rec


def sync_import(cfg: Config, project: Project, import_id: str, *,
                catalog: CatalogHook = None,
                progress: Callable[[dict], None] | None = None) -> dict:
    """Rifetcha solo le pagine cambiate di un import esistente."""
    imports_log = ImportsLog(project)
    rec = imports_log.get(import_id)
    limits = ImportLimits.from_config(cfg, **{
        k: v for k, v in rec.get("limits", {}).items()
        if k not in ("include", "exclude")})
    limits.include = rec.get("limits", {}).get("include", [])
    limits.exclude = rec.get("limits", {}).get("exclude", [])

    session = make_session(cfg, limits)
    auth_store = AuthProfileStore(cfg)
    auth_profile = rec.get("auth_profile") or auth_store.find_for(rec["url"])
    auth_store.apply_profile(session, auth_profile)
    session.guard_auth = True
    adapter = detect_adapter(rec["url"], session, limits,
                             forced=rec.get("adapter", "auto"))
    adapter.resolve(rec["url"], rec.get("scope", "page"))  # imposta lo scope

    writer = WikiWriter(project)
    index = SearchIndex(project)
    checked = refetched = 0
    reauthed = False
    for page_url, info in list(rec["page_index"].items()):
        checked += 1
        if progress:
            progress({"phase": "syncing", "pages": checked,
                      "max_pages": len(rec["page_index"]),
                      "detail": page_url})
        ref = PageRef(id=page_url, url=page_url)
        if not adapter.changed_since(ref, info.get("revision")):
            continue
        try:
            page = adapter.fetch(ref)
        except AuthRequiredError:
            if reauthed or not auth_profile \
                    or not auth_store.reauth(session, auth_profile):
                raise
            reauthed = True
            page = adapter.fetch(ref)
        except Exception as e:  # noqa: BLE001
            rec["errors"].append(f"sync {page_url}: {e}")
            continue
        path, changed = writer.write_source(
            title=page.ref.title or page.ref.url, markdown=page.markdown,
            url=page.ref.url, source_kind="web", import_id=rec["id"],
            revision=page.revision, extra_fm={"adapter": adapter.name})
        info["revision"] = page.revision
        if changed:
            refetched += 1
            rel = f"sources/{path.name}"
            fm, body = writer.read_page(rel)
            index.upsert(rel, "sources", fm.get("title", ""), body)
            if catalog:
                catalog(project, rel)

    rec["last_sync"] = _now()
    imports_log.upsert(rec)
    writer.append_log(
        f"sync {import_id}: {refetched}/{checked} pagine aggiornate")
    if refetched:
        writer.rebuild_index()
    return {"import_id": import_id, "checked": checked, "refetched": refetched}


def refresh_page(cfg: Config, project: Project, wiki_path: str, *,
                 catalog: CatalogHook = None) -> dict:
    """Refresh puntuale di una source (rifetch dall'origine)."""
    writer = WikiWriter(project)
    fm, _ = writer.read_page(wiki_path)
    url = fm.get("url")
    if not url:
        raise ValueError(f"la pagina {wiki_path} non ha un'origine (url)")
    limits = ImportLimits.from_config(cfg)
    session = make_session(cfg, limits)
    # ritrova il profilo auth dall'import di origine, se registrato
    auth_store = AuthProfileStore(cfg)
    auth_profile = None
    if fm.get("import_id"):
        try:
            auth_profile = ImportsLog(project).get(fm["import_id"]).get("auth_profile")
        except KeyError:
            pass
    auth_profile = auth_profile or auth_store.find_for(url)
    auth_store.apply_profile(session, auth_profile)
    session.guard_auth = True
    adapter = detect_adapter(url, session, limits,
                             forced=fm.get("adapter", "auto"))
    adapter.resolve(url, "page")
    page = adapter.fetch(PageRef(id=url, url=url, title=fm.get("title", "")))
    path, changed = writer.write_source(
        title=fm.get("title") or page.ref.title, markdown=page.markdown,
        url=url, source_kind=fm.get("kind", "web"),
        import_id=fm.get("import_id", ""), revision=page.revision,
        extra_fm={"adapter": adapter.name})
    if changed:
        rel = f"sources/{path.name}"
        fm2, body = writer.read_page(rel)
        SearchIndex(project).upsert(rel, "sources", fm2.get("title", ""), body)
        if catalog:
            catalog(project, rel)
    writer.append_log(f"refresh {wiki_path}: {'aggiornata' if changed else 'invariata'}")
    return {"path": wiki_path, "changed": changed}


# -- CLI wrappers -------------------------------------------------------------

def _cli_progress(state: dict) -> None:
    print(f"\r[{state['phase']}] {state['pages']}/{state['max_pages']} "
          f"{state.get('detail', '')[:60]:<60}", end="", flush=True)


def start_import_cli(cfg: Config, store: ProjectStore, args) -> int:
    project = store.get(args.project)
    limits = ImportLimits.from_config(
        cfg, max_pages=args.max_pages, max_depth=args.max_depth,
        include=args.include or None, exclude=args.exclude or None)
    catalog = None
    if not args.no_catalog:
        try:
            from .catalog import make_catalog_hook
            catalog = make_catalog_hook(cfg)
        except ImportError:
            pass
    rec = run_import(cfg, project, args.url, depth=args.depth,
                     scope=args.scope, limits=limits,
                     auth_profile=args.auth_profile,
                     adapter_name=args.adapter, catalog=catalog,
                     progress=_cli_progress)
    print()
    print(json.dumps({k: rec[k] for k in
                      ("id", "state", "pages", "skipped", "truncated", "errors")},
                     ensure_ascii=False, indent=1))
    return 0 if rec["state"] == "done" else 1


def sync_import_cli(cfg: Config, store: ProjectStore, args) -> int:
    project = store.get(args.project)
    try:
        result = sync_import(cfg, project, args.import_id,
                             progress=_cli_progress)
    except AuthRequiredError as e:
        print()
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        return 2
    print()
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


def refresh_page_cli(cfg: Config, store: ProjectStore, args) -> int:
    project = store.get(args.project)
    try:
        result = refresh_page(cfg, project, args.wiki_path)
    except AuthRequiredError as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0
