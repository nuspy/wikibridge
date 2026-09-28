"""Controllo aggiornamenti on-demand di tutte le sorgenti di un progetto.

Nessun watcher o scheduler interno: il controllo parte solo quando viene
invocato (CLI `wikibridge update`, API POST /update, un agente, un cron).
Copre:
- rami wiki: ogni import (URL + scope + depth) — pagine cambiate, pagine
  nuove del ramo, pagine sparite;
- file locali: cartelle registrate (rami di file) e liste di singoli file,
  registrate in ingests.json.

Identità stabile: una pagina wiki è legata al suo id nativo (page_index
dell'import), un file al registro ingests.json. Un aggiornamento riscrive
sempre la STESSA pagina, quindi niente duplicati se alla sorgente cambia il
titolo o se il file viene rinominato/spostato (riconosciuto dall'id file
del filesystem o, in mancanza, dallo sha256).

Rimozioni prudenti: una pagina o un file assente viene rimosso solo dopo
REMOVE_AFTER controlli consecutivi; mai se la sorgente è irraggiungibile
(cartella radice mancante, auth fallita) né se sparisce in un colpo più di
MASS_MISSING_RATIO del ramo (tipico di un disco non montato o di un elenco
troncato), nel qual caso si segnala e non si tocca nulla.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections import Counter, deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import requests

from .adapters.base import PageGoneError, PageRef
from .auth import AuthRequiredError
from .config import Config
from .convert.docs import MAX_DOC_MB, SUPPORTED_EXT, convert_document
from .importer import (ImportsLog, _now, import_limits, open_import_adapter,
                       wiki_doc_id)
from .projects import Project
from .wiki.search import SearchIndex
from .wiki.writer import WikiWriter, parse_frontmatter

log = logging.getLogger("wikibridge.updater")

REMOVE_AFTER = 2
MASS_MISSING_MIN = 5
MASS_MISSING_RATIO = 0.5
DEFAULT_FILES_SOURCE = "ing-files"

CatalogHook = Callable[[Project, str], None] | None
Progress = Callable[[dict], None] | None


@dataclass
class SourceReport:
    kind: str                  # wiki | dir | files
    id: str
    target: str
    state: str = "ok"          # ok | unreachable | auth_required | error
    checked: int = 0
    unchanged: int = 0
    updated: int = 0
    added: int = 0
    renamed: int = 0
    missing: int = 0           # assenti, in attesa di conferma
    removed: int = 0
    errors: list[str] = field(default_factory=list)

    def changed(self) -> bool:
        return bool(self.updated or self.added or self.renamed or self.removed)


# -- registro delle sorgenti file ------------------------------------------------

def file_key(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def file_doc_id(key: str) -> str:
    """Deterministico dal primo percorso visto; poi resta nel registro
    anche se il file viene rinominato."""
    return "file:" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def _file_id(st: os.stat_result) -> str:
    # su NTFS st_ino è l'id del file: stabile a rinomina/spostamento nello
    # stesso volume, ma NON a un salvataggio "atomico" (file temporaneo +
    # rinomina) — per questo è solo un indizio di rinomina, non l'identità.
    return f"{st.st_dev}:{st.st_ino}" if st.st_ino else ""


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


class IngestsLog:
    """ingests.json: sorgenti file del progetto, thread-safe.

    [{"id", "kind": "dir"|"files", "root", "recursive", "paths", "created",
      "last_check", "state",
      "files": {<file_key>: {"path", "doc_id", "page", "file_id", "size",
                             "mtime_ns", "sha256", "missing",
                             "last_checked"}}}]
    """

    _locks: dict[str, threading.Lock] = {}

    def __init__(self, project: Project):
        self._path = project.ingests_json
        self._lock = self._locks.setdefault(str(self._path), threading.Lock())

    def load(self) -> list[dict]:
        if self._path.is_file():
            return json.loads(self._path.read_text(encoding="utf-8"))
        return []

    def _save(self, records: list[dict]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(records, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(self._path)

    def upsert(self, record: dict) -> None:
        with self._lock:
            records = self.load()
            for i, r in enumerate(records):
                if r["id"] == record["id"]:
                    records[i] = record
                    break
            else:
                records.append(record)
            self._save(records)

    def get(self, source_id: str) -> dict:
        for r in self.load():
            if r["id"] == source_id:
                return r
        raise KeyError(f"sorgente file inesistente: {source_id}")

    def register_dir(self, root: Path, recursive: bool) -> dict:
        key = file_key(root)
        for r in self.load():
            if r["kind"] == "dir" and file_key(r["root"]) == key:
                if recursive and not r.get("recursive"):
                    r["recursive"] = True
                    self.upsert(r)
                return r
        records = self.load()
        rec = {"id": f"ing-{len(records) + 1:04d}", "kind": "dir",
               "root": str(Path(root).resolve()), "recursive": recursive,
               "created": _now(), "last_check": None, "state": "ok",
               "files": {}}
        self.upsert(rec)
        return rec

    def files_source(self) -> dict:
        """La sorgente 'lista di singoli file' di default (creata se serve)."""
        try:
            return self.get(DEFAULT_FILES_SOURCE)
        except KeyError:
            rec = {"id": DEFAULT_FILES_SOURCE, "kind": "files", "paths": [],
                   "created": _now(), "last_check": None, "state": "ok",
                   "files": {}}
            self.upsert(rec)
            return rec

    def source_tracking(self, key: str) -> dict | None:
        """La sorgente che traccia (o dovrebbe tracciare) il file: quella
        che lo ha già nel registro, oppure la cartella registrata che lo
        contiene."""
        records = self.load()
        for r in records:
            if key in r.get("files", {}):
                return r
            if r["kind"] == "files" and key in {file_key(p) for p in r.get("paths", [])}:
                return r
        parent = os.path.dirname(key)
        for r in records:
            if r["kind"] != "dir":
                continue
            root = file_key(r["root"])
            if parent == root or (r.get("recursive")
                                  and parent.startswith(root + os.sep)):
                return r
        return None


# -- riferimenti e rimozione pagine ---------------------------------------------

def _page_refcount(project: Project) -> Counter:
    """Quante sorgenti (import o file) referenziano ciascuna pagina: una
    pagina condivisa da due rami non si cancella se sparisce da uno solo."""
    refs: Counter = Counter()
    for rec in ImportsLog(project).load():
        for info in rec.get("page_index", {}).values():
            refs[info.get("path")] += 1
    for rec in IngestsLog(project).load():
        for entry in rec.get("files", {}).values():
            refs[entry.get("page")] += 1
    return refs


def remove_page(project: Project, rel: str) -> None:
    """Rimuove una source e tutto ciò che ne deriva (entità, relazioni,
    indice, task di catalogazione)."""
    from .catalog import remove_source_contributions
    remove_source_contributions(project, rel)
    WikiWriter(project).remove_source(rel)
    SearchIndex(project).remove(rel)
    (project.pending_dir / f"{Path(rel).stem}.json").unlink(missing_ok=True)


def _mass_missing(n_missing: int, n_known: int) -> bool:
    return n_missing >= MASS_MISSING_MIN and n_missing > MASS_MISSING_RATIO * n_known


# -- rami wiki -----------------------------------------------------------------

def _is_gone(exc: Exception) -> bool:
    if isinstance(exc, PageGoneError):
        return True
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return exc.response.status_code in (404, 410)
    return False


def update_import(cfg: Config, project: Project, import_id: str, *,
                  discover: bool = True, dry_run: bool = False,
                  catalog: CatalogHook = None, progress: Progress = None) -> SourceReport:
    imports_log = ImportsLog(project)
    rec = imports_log.get(import_id)
    report = SourceReport(kind="wiki", id=import_id, target=rec["url"])
    limits = import_limits(cfg, rec)
    writer = WikiWriter(project)
    index = SearchIndex(project)
    known: dict = rec.setdefault("page_index", {})
    refcount = _page_refcount(project)
    max_depth = int(rec.get("depth", 1))

    try:
        adapter, session, auth_store, auth_profile = open_import_adapter(cfg, rec, limits)
        # resolve() imposta anche lo scope dell'adapter (serve al crawler
        # generico per restare nel ramo) e, per LISTING_SCOPES, elenca il ramo
        seed = adapter.resolve(rec["url"], rec.get("scope", "page"))
    except AuthRequiredError as e:
        report.state = "auth_required"
        report.errors.append(str(e))
        return report
    except Exception as e:  # noqa: BLE001
        report.state = "unreachable"
        report.errors.append(f"ramo non raggiungibile: {e}")
        return report

    listing_complete = (discover and rec.get("scope") in adapter.LISTING_SCOPES
                        and len(seed) < limits.max_pages)
    reauthed = False

    def fetch(ref: PageRef):
        nonlocal reauthed
        try:
            return adapter.fetch(ref)
        except AuthRequiredError:
            if reauthed or not auth_profile or not auth_store.reauth(session, auth_profile):
                raise
            reauthed = True
            return adapter.fetch(ref)

    def store(page, rel_path: str | None) -> tuple[str, bool]:
        doc_id = wiki_doc_id(adapter.name, page.ref.url, page.ref.id)
        path, changed = writer.write_source(
            title=page.ref.title or page.ref.url, markdown=page.markdown,
            url=page.ref.url, source_kind="web", import_id=rec["id"],
            revision=page.revision, rel_path=rel_path, doc_id=doc_id,
            native_id=page.ref.id, extra_fm={"adapter": adapter.name})
        rel = f"sources/{path.name}"
        if changed:
            fm, body = writer.read_page(rel)
            index.upsert_page(rel, "sources", fm, body)
            if catalog:
                catalog(project, rel)
        return rel, changed

    # 1. pagine nuove e pagine uscite dal ramo, dall'elenco completo
    by_native = {i.get("native_id"): u for u, i in known.items() if i.get("native_id")}
    in_listing: set[str] = set()
    new_refs: list[PageRef] = []
    if listing_complete:
        for ref in seed:
            url = by_native.get(ref.id) or (ref.url if ref.url in known else None)
            if url is None:
                new_refs.append(ref)
                continue
            if url != ref.url and ref.url not in known:
                known[ref.url] = known.pop(url)     # URL cambiato (es. titolo)
                url = ref.url
            in_listing.add(url)

    # 2. pagine note: cambiate / invariate / sparite
    candidates_missing: list[str] = []
    total = len(known)
    try:
        for n, (url, info) in enumerate(list(known.items()), 1):
            if progress:
                progress({"phase": "updating", "pages": n, "max_pages": total,
                          "detail": url})
            report.checked += 1
            # dall'elenco si deduce l'assenza solo per le pagine che ne fanno
            # parte (depth 0); quelle raggiunte via link si verificano una a una
            if listing_complete and info.get("depth", 0) == 0 and url not in in_listing:
                candidates_missing.append(url)
                continue
            ref = PageRef(id=info.get("native_id", ""), url=url,
                          depth=info.get("depth", 0))
            if not adapter.changed_since(ref, info.get("revision")):
                report.unchanged += 1
                info["missing"] = 0
                continue
            if dry_run:
                report.updated += 1
                continue
            try:
                page = fetch(ref)
            except AuthRequiredError:
                raise
            except Exception as e:  # noqa: BLE001
                if _is_gone(e):
                    candidates_missing.append(url)
                else:
                    report.errors.append(f"{url}: {e}")
                continue
            _, changed = store(page, info["path"])
            report.updated += 1 if changed else 0
            report.unchanged += 0 if changed else 1
            info.update(revision=page.revision, native_id=page.ref.id,
                        doc_id=wiki_doc_id(adapter.name, page.ref.url, page.ref.id),
                        missing=0, last_checked=_now())
            if discover and not listing_complete and ref.depth < max_depth:
                new_refs += [l for l in page.links if l.url not in known]

        # 3. pagine sparite: rimozione solo dopo conferme ripetute
        if _mass_missing(len(candidates_missing), len(known)):
            report.errors.append(
                f"{len(candidates_missing)}/{len(known)} pagine assenti in un "
                "solo controllo: sospetta indisponibilità, nessuna rimozione")
        else:
            for url in candidates_missing:
                info = known[url]
                if dry_run:
                    report.missing += 1
                    continue
                info["missing"] = int(info.get("missing", 0)) + 1
                if info["missing"] < REMOVE_AFTER:
                    report.missing += 1
                    continue
                if refcount[info["path"]] <= 1:
                    remove_page(project, info["path"])
                del known[url]
                report.removed += 1

        # 4. pagine nuove (dall'elenco o dai link delle pagine cambiate)
        queue = deque(new_refs)
        seen = set(known)
        budget = max(0, limits.max_pages - len(known))
        while queue and budget > 0:
            ref = queue.popleft()
            if ref.url in seen or not limits.title_allowed(ref.title or ref.url):
                continue
            seen.add(ref.url)
            if dry_run:
                report.added += 1
                budget -= 1
                continue
            try:
                page = fetch(ref)
            except AuthRequiredError:
                raise
            except Exception as e:  # noqa: BLE001
                report.errors.append(f"{ref.url}: {e}")
                continue
            rel, _ = store(page, None)
            known[page.ref.url] = {
                "path": rel, "revision": page.revision, "depth": ref.depth,
                "native_id": page.ref.id,
                "doc_id": wiki_doc_id(adapter.name, page.ref.url, page.ref.id),
                "missing": 0, "last_checked": _now()}
            report.added += 1
            budget -= 1
            if not listing_complete and ref.depth < max_depth:
                queue.extend(l for l in page.links if l.url not in seen)
    except AuthRequiredError as e:
        report.state = "auth_required"
        report.errors.append(str(e))

    if not dry_run:
        rec["last_sync"] = _now()
        rec["pages"] = len(known)
        imports_log.upsert(rec)
    return report


# -- file locali ---------------------------------------------------------------

def _scan(src: dict) -> dict[str, Path] | None:
    """File attuali della sorgente; None se la radice non è raggiungibile."""
    if src["kind"] == "dir":
        root = Path(src["root"])
        if not root.is_dir():
            return None
        pattern = "**/*" if src.get("recursive") else "*"
        return {file_key(p): p for p in root.glob(pattern)
                if p.is_file() and p.suffix.lower() in SUPPORTED_EXT}
    return {file_key(p): Path(p) for p in src.get("paths", []) if Path(p).is_file()}


def update_files_source(cfg: Config, project: Project, source_id: str, *,
                        only_keys: set[str] | None = None,
                        dry_run: bool = False, catalog: CatalogHook = None,
                        progress: Progress = None) -> SourceReport:
    ingests = IngestsLog(project)
    src = ingests.get(source_id)
    target = src.get("root") or ", ".join(src.get("paths", []))[:200]
    report = SourceReport(kind=src["kind"], id=source_id, target=target)
    writer = WikiWriter(project)
    index = SearchIndex(project)
    tracked: dict = src.setdefault("files", {})

    current = _scan(src)
    if current is None:
        report.state = "unreachable"
        report.errors.append(f"cartella non raggiungibile: {src.get('root')}")
        if not dry_run:
            src["state"] = "unreachable"
            src["last_check"] = _now()
            ingests.upsert(src)
        return report
    if only_keys is not None:
        current = {k: p for k, p in current.items() if k in only_keys}
        scope_known = {k for k in tracked if k in only_keys}
    else:
        scope_known = set(tracked)

    missing_keys = [k for k in scope_known if k not in current]
    new_keys = [k for k in current if k not in tracked]

    # rinomine/spostamenti: stesso id file oppure stesso contenuto. La voce
    # del registro (doc_id, pagina) passa al nuovo percorso; il "path" resta
    # quello vecchio, così il ciclo sotto riscrive titolo e URL della pagina.
    renamed: set[str] = set()
    if only_keys is None and missing_keys and new_keys:
        by_fid = {tracked[k].get("file_id"): k for k in missing_keys
                  if tracked[k].get("file_id")}
        by_sha = {tracked[k].get("sha256"): k for k in missing_keys
                  if tracked[k].get("sha256")}
        for nk in list(new_keys):
            p = current[nk]
            old = by_fid.get(_file_id(p.stat()))
            if old is None and by_sha:
                old = by_sha.get(_sha256_file(p))
            if old is None or old not in missing_keys:
                continue
            if not dry_run:
                tracked[nk] = tracked.pop(old)
            missing_keys.remove(old)
            new_keys.remove(nk)
            renamed.add(nk)
            report.renamed += 1

    total = len(current)
    for n, (key, p) in enumerate(sorted(current.items()), 1):
        if progress:
            progress({"phase": "updating", "pages": n, "max_pages": total,
                      "detail": p.name})
        report.checked += 1
        if dry_run and key in renamed:
            continue
        entry = tracked.get(key)
        try:
            st = p.stat()
            if (entry and entry.get("size") == st.st_size
                    and entry.get("mtime_ns") == st.st_mtime_ns
                    and entry.get("path") == str(p)):
                report.unchanged += 1
                continue
            sha = _sha256_file(p)
            if entry and entry.get("sha256") == sha and entry.get("path") == str(p):
                if not dry_run:
                    entry.update(size=st.st_size, mtime_ns=st.st_mtime_ns,
                                 file_id=_file_id(st), missing=0,
                                 last_checked=_now())
                report.unchanged += 1
                continue
            if dry_run:
                if entry:
                    report.updated += 1
                else:
                    report.added += 1
                continue
            if st.st_size > MAX_DOC_MB * 1024 * 1024:
                report.errors.append(f"{p.name}: oltre {MAX_DOC_MB} MB")
                continue
            markdown = convert_document(p)
            if not markdown.strip():
                report.errors.append(f"{p.name}: conversione vuota")
                continue
        except Exception as e:  # noqa: BLE001 — un file rotto non ferma il ramo
            report.errors.append(f"{p.name}: {e}")
            continue
        doc_id = entry["doc_id"] if entry else file_doc_id(key)
        path, changed = writer.write_source(
            title=p.stem, markdown=markdown, url=p.as_uri(),
            source_kind="document", import_id=source_id,
            rel_path=entry.get("page") if entry else None,
            doc_id=doc_id, native_id=str(p),
            extra_fm={"original_path": str(p), "format": p.suffix.lower()})
        rel = f"sources/{path.name}"
        if changed:
            fm, body = writer.read_page(rel)
            index.upsert_page(rel, "sources", fm, body)
            if catalog:
                catalog(project, rel)
        if entry is None:
            report.added += 1
        elif key in renamed:
            pass                               # già contato in renamed
        elif changed:
            report.updated += 1
        else:
            report.unchanged += 1
        tracked[key] = {"path": str(p), "doc_id": doc_id, "page": rel,
                        "file_id": _file_id(st), "size": st.st_size,
                        "mtime_ns": st.st_mtime_ns, "sha256": sha,
                        "missing": 0, "last_checked": _now()}

    if _mass_missing(len(missing_keys), len(scope_known)):
        report.errors.append(
            f"{len(missing_keys)}/{len(scope_known)} file assenti in un solo "
            "controllo: sospetta indisponibilità, nessuna rimozione")
    else:
        refcount = _page_refcount(project) if missing_keys else Counter()
        for key in missing_keys:
            entry = tracked[key]
            if dry_run:
                report.missing += 1
                continue
            entry["missing"] = int(entry.get("missing", 0)) + 1
            if entry["missing"] < REMOVE_AFTER:
                report.missing += 1
                continue
            if refcount[entry["page"]] <= 1:
                remove_page(project, entry["page"])
            del tracked[key]
            report.removed += 1

    if not dry_run:
        src["state"] = "ok"
        src["last_check"] = _now()
        ingests.upsert(src)
    return report


def _bootstrap_legacy_documents(project: Project, ingests: IngestsLog) -> int:
    """Documenti ingeriti prima del registro: li aggancia alla sorgente
    'lista di file', così diventano aggiornabili senza reimportarli."""
    tracked_pages = {e.get("page") for r in ingests.load()
                     for e in r.get("files", {}).values()}
    added = 0
    src = None
    for p in sorted((project.wiki_dir / "sources").glob("*.md")):
        rel = f"sources/{p.name}"
        if rel in tracked_pages:
            continue
        fm, _ = parse_frontmatter(p.read_text(encoding="utf-8"))
        if fm.get("kind") != "document" or not fm.get("original_path"):
            continue
        src = src or ingests.files_source()
        key = file_key(fm["original_path"])
        if key in src["files"]:
            continue
        src["paths"].append(fm["original_path"])
        src["files"][key] = {"path": fm["original_path"],
                             "doc_id": fm.get("doc_id") or file_doc_id(key),
                             "page": rel, "file_id": "", "size": None,
                             "mtime_ns": None, "sha256": None, "missing": 0,
                             "last_checked": None}
        added += 1
    if src is not None and added:
        ingests.upsert(src)
    return added


# -- punto d'ingresso ------------------------------------------------------------

def register_paths(project: Project, paths: list[str], *, recursive: bool = True
                   ) -> tuple[list[tuple[str, set[str] | None]], list[str]]:
    """Traduce percorsi (file o cartelle) in (sorgente, chiavi da
    controllare). Cartella = ramo registrato (riusato se esiste già).
    File già tracciato, o dentro una cartella registrata = controllo di quel
    solo file nella sua sorgente; altrimenti viene aggiunto alla lista di
    singoli file. Ritorna (target, errori per percorso)."""
    ingests = IngestsLog(project)
    targets: dict[str, set[str] | None] = {}
    errors: list[str] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            targets[ingests.register_dir(p, recursive)["id"]] = None
            continue
        if p.is_file() and p.suffix.lower() not in SUPPORTED_EXT:
            errors.append(f"{raw}: formato non supportato")
            continue
        key = file_key(p)
        src = ingests.source_tracking(key)
        if src is None:
            if not p.is_file():
                errors.append(f"{raw}: percorso inesistente")
                continue
            src = ingests.files_source()
            src["paths"].append(str(p.resolve()))
            ingests.upsert(src)
        if targets.get(src["id"], set()) is not None:
            targets.setdefault(src["id"], set()).add(key)
    return list(targets.items()), errors


def check_updates(cfg: Config, project: Project, *,
                  imports: list[str] | None = None,
                  ingests: list[str] | None = None,
                  paths: list[str] | None = None,
                  discover: bool = True, dry_run: bool = False,
                  catalog: CatalogHook = None,
                  progress: Progress = None) -> dict:
    """Controlla e applica gli aggiornamenti. Senza selettori: TUTTE le
    sorgenti del progetto (ogni import + ogni sorgente file). Con
    imports/ingests/paths: solo quelle.

    discover: cerca pagine nuove nei rami wiki.
    dry_run: riporta cosa cambierebbe senza scrivere nulla.
    """
    t0 = time.monotonic()
    ingests_log = IngestsLog(project)
    if not dry_run:
        _bootstrap_legacy_documents(project, ingests_log)

    select_all = imports is None and ingests is None and paths is None
    wiki_ids = ([r["id"] for r in ImportsLog(project).load()
                 if r.get("page_index") and r.get("state") != "running"]
                if select_all else list(imports or []))
    file_targets: list[tuple[str, set[str] | None]] = (
        [(r["id"], None) for r in ingests_log.load()] if select_all
        else [(i, None) for i in (ingests or [])])

    reports: list[SourceReport] = []
    if paths:
        targets, path_errors = register_paths(project, paths)
        file_targets += targets
        if path_errors:
            reports.append(SourceReport(kind="files", id="-", target="percorsi",
                                        state="error", errors=path_errors))

    for import_id in wiki_ids:
        try:
            reports.append(update_import(cfg, project, import_id,
                                         discover=discover, dry_run=dry_run,
                                         catalog=catalog, progress=progress))
        except Exception as e:  # noqa: BLE001 — un ramo rotto non ferma gli altri
            log.exception("update dell'import %s fallito", import_id)
            reports.append(SourceReport(kind="wiki", id=import_id, target="",
                                        state="error", errors=[str(e)]))
    merged: dict[str, set[str] | None] = {}
    for source_id, keys in file_targets:        # stessa sorgente una sola volta
        if source_id in merged and (merged[source_id] is None or keys is None):
            merged[source_id] = None
        else:
            merged[source_id] = (merged.get(source_id) or set()) | keys \
                if keys is not None else None
    for source_id, keys in merged.items():
        try:
            reports.append(update_files_source(cfg, project, source_id,
                                               only_keys=keys, dry_run=dry_run,
                                               catalog=catalog, progress=progress))
        except Exception as e:  # noqa: BLE001
            log.exception("update della sorgente %s fallito", source_id)
            reports.append(SourceReport(kind="files", id=source_id, target="",
                                        state="error", errors=[str(e)]))

    totals = Counter()
    for r in reports:
        for k in ("checked", "unchanged", "updated", "added", "renamed",
                  "missing", "removed"):
            totals[k] += getattr(r, k)
        totals["errors"] += len(r.errors)
    if not dry_run and any(r.changed() for r in reports):
        writer = WikiWriter(project)
        writer.append_log(
            f"update: {totals['updated']} aggiornati, {totals['added']} nuovi, "
            f"{totals['renamed']} rinominati, {totals['removed']} rimossi "
            f"su {len(reports)} sorgenti")
        writer.rebuild_index()
    return {"project": project.slug, "dry_run": dry_run,
            "duration_s": round(time.monotonic() - t0, 2),
            "totals": dict(totals),
            "sources": [asdict(r) for r in reports]}


def list_sources(project: Project) -> list[dict]:
    """Sorgenti aggiornabili del progetto (per agenti, cron e GUI)."""
    out = []
    for r in ImportsLog(project).load():
        out.append({"kind": "wiki", "id": r["id"], "target": r["url"],
                    "scope": r.get("scope"), "depth": r.get("depth"),
                    "pages": len(r.get("page_index", {})),
                    "last_check": r.get("last_sync") or r.get("finished")})
    for r in IngestsLog(project).load():
        out.append({"kind": r["kind"], "id": r["id"],
                    "target": r.get("root") or r.get("paths"),
                    "files": len(r.get("files", {})),
                    "last_check": r.get("last_check"), "state": r.get("state")})
    return out
