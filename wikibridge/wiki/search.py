"""Indice full-text per progetto (SQLite FTS5, stdlib).

Righe di ricerca:
- una per ogni pagina source;
- una per ogni SEGMENTO delle pagine entità/concetto (wiki/segments.py),
  più una per l'eventuale testo legacy non attribuibile;
- una per le altre pagine (riunioni, sintesi).

Ogni riga porta source_ref, la source da cui deriva (se stessa, per le
source; vuoto per ciò che non è attribuibile a una source). Le ACL non
stanno nella tabella FTS: la tabella docs associa ogni source a un acl_id,
e acl_sets associa ogni acl_id a una chiave condivisa (vedi acl.py). Così
cambiare le ACL di un documento aggiorna una riga di docs e non
reindicizza testo, e cambiare quelle di una collezione non tocca l'indice.

Filtro: il chiamante passa l'insieme degli acl_id visibili (Access); la
query tiene le righe la cui source ha uno di quegli acl_id, e un secondo
controllo in Python dopo il fetch fa da rete di sicurezza.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from ..acl import ADMIN_ROLE, acl_key  # noqa: F401 — ADMIN_ROLE riesportato
from ..projects import Project
from .segments import legacy_text, split_body
from .writer import WikiWriter

SCHEMA_VERSION = "3"

_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS pages USING fts5(
    row_key UNINDEXED,
    page UNINDEXED,
    section UNINDEXED,
    title,
    body,
    source_ref UNINDEXED,
    doc_id UNINDEXED,
    content_version UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
);
CREATE TABLE IF NOT EXISTS row_map(
    rid INTEGER PRIMARY KEY, page TEXT NOT NULL, source_ref TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS row_map_page ON row_map(page);
CREATE INDEX IF NOT EXISTS row_map_source ON row_map(source_ref);
CREATE TABLE IF NOT EXISTS docs(
    path TEXT PRIMARY KEY, doc_id TEXT, acl_id INTEGER NOT NULL,
    content_version INTEGER);
CREATE INDEX IF NOT EXISTS docs_acl ON docs(acl_id);
CREATE TABLE IF NOT EXISTS acl_sets(acl_id INTEGER PRIMARY KEY, key TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""

_ALL_TABLES = ("pages", "row_map", "docs", "acl_sets", "meta")
_checked: set[str] = set()
_checked_lock = threading.Lock()


class SearchIndex:
    def __init__(self, project: Project):
        self.project = project
        self.db_path = project.index_db

    # -- connessione e schema --------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        key = str(Path(self.db_path).resolve())
        existed = Path(self.db_path).is_file()
        conn = sqlite3.connect(self.db_path, timeout=30)
        with _checked_lock:
            if key not in _checked or not existed:
                self._ensure_schema(conn)
                _checked.add(key)
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        """Una volta per processo: se lo schema è di una versione precedente
        lo ricrea e ricostruisce l'indice dai Markdown (fonte di verità).
        BEGIN IMMEDIATE serializza la migrazione anche tra processi (CLI e
        server insieme)."""
        previous = conn.isolation_level
        conn.isolation_level = None       # transazione gestita a mano
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                try:
                    row = conn.execute("SELECT v FROM meta WHERE k='schema'").fetchone()
                    version = row[0] if row else None
                except sqlite3.OperationalError:
                    version = None
                if version != SCHEMA_VERSION:
                    for t in _ALL_TABLES:
                        conn.execute(f"DROP TABLE IF EXISTS {t}")
                    for stmt in filter(str.strip, _SCHEMA.split(";")):
                        conn.execute(stmt)
                    self._rebuild_rows(conn)
                    conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema', ?)",
                                 (SCHEMA_VERSION,))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.isolation_level = previous

    # -- scrittura ---------------------------------------------------------------

    def _acl_id(self, conn: sqlite3.Connection, key: str) -> int:
        conn.execute("INSERT OR IGNORE INTO acl_sets(key) VALUES (?)", (key,))
        return conn.execute("SELECT acl_id FROM acl_sets WHERE key = ?",
                            (key,)).fetchone()[0]

    def _delete_page(self, conn: sqlite3.Connection, rel_path: str) -> None:
        rids = [r[0] for r in conn.execute(
            "SELECT rid FROM row_map WHERE page = ?", (rel_path,))]
        for rid in rids:
            conn.execute("DELETE FROM pages WHERE rowid = ?", (rid,))
        conn.execute("DELETE FROM row_map WHERE page = ?", (rel_path,))
        conn.execute("DELETE FROM docs WHERE path = ?", (rel_path,))

    def _insert(self, conn, row_key, page, section, title, body, source_ref,
                doc_id="", version=0) -> None:
        cur = conn.execute(
            """INSERT INTO pages (row_key, page, section, title, body,
                                  source_ref, doc_id, content_version)
               VALUES (?,?,?,?,?,?,?,?)""",
            (row_key, page, section, title, body, source_ref, doc_id, version))
        conn.execute("INSERT INTO row_map VALUES (?,?,?)",
                     (cur.lastrowid, page, source_ref))

    def _index_page(self, conn, rel_path: str, section: str, fm: dict,
                    body: str) -> None:
        self._delete_page(conn, rel_path)
        title = fm.get("title") or rel_path
        if section == "sources":
            key = acl_key(fm.get("import_id", ""), fm.get("acl_roles"),
                          fm.get("acl_classification"))
            version = int(fm.get("content_version") or 0)
            conn.execute("INSERT INTO docs VALUES (?,?,?,?)",
                         (rel_path, fm.get("doc_id") or "",
                          self._acl_id(conn, key), version))
            self._insert(conn, rel_path, rel_path, section, title, body,
                         rel_path, fm.get("doc_id") or "", version)
        elif section in ("entities", "concepts"):
            rest, segments = split_body(body)
            legacy = legacy_text(rest)
            if legacy:
                self._insert(conn, f"{rel_path}#legacy", rel_path, section,
                             title, legacy, "")
            for src, text in segments.items():
                doc = conn.execute("SELECT doc_id, content_version FROM docs "
                                   "WHERE path = ?", (src,)).fetchone()
                self._insert(conn, f"{rel_path}#{src}", rel_path, section,
                             title, text, src, *(doc or ("", 0)))
        else:
            self._insert(conn, rel_path, rel_path, section, title, body, "")

    def _rebuild_rows(self, conn: sqlite3.Connection) -> int:
        for t in ("pages", "row_map", "docs"):
            conn.execute(f"DELETE FROM {t}")
        n = 0
        # iter_pages elenca prima sources/: i segmenti trovano già i loro doc
        for rel_path, fm, body in WikiWriter(self.project).iter_pages():
            self._index_page(conn, rel_path, rel_path.split("/", 1)[0], fm, body)
            n += 1
        return n

    def upsert_page(self, rel_path: str, section: str, fm: dict,
                    body: str) -> None:
        """Indicizza una pagina prendendo titolo, ACL, doc_id e versione dal
        suo frontmatter; le pagine derivate per segmenti."""
        with self._conn() as conn:
            self._index_page(conn, rel_path, section, fm, body)

    def upsert(self, rel_path: str, section: str, title: str, body: str) -> None:
        self.upsert_page(rel_path, section, {"title": title}, body)

    def set_doc_acl(self, rel_path: str, fm: dict) -> None:
        """ACL di un documento cambiate: una riga di docs, niente testo."""
        key = acl_key(fm.get("import_id", ""), fm.get("acl_roles"),
                      fm.get("acl_classification"))
        with self._conn() as conn:
            conn.execute("UPDATE docs SET acl_id = ? WHERE path = ?",
                         (self._acl_id(conn, key), rel_path))

    def remove(self, rel_path: str) -> None:
        with self._conn() as conn:
            self._delete_page(conn, rel_path)

    def rebuild(self) -> int:
        """Reindicizza l'intera wiki del progetto. Ritorna il n. di pagine."""
        with self._conn() as conn:
            return self._rebuild_rows(conn)

    # -- lettura -----------------------------------------------------------------

    def acl_sets(self) -> list[tuple[int, str]]:
        with self._conn() as conn:
            return conn.execute("SELECT acl_id, key FROM acl_sets").fetchall()

    def pages_for_acl(self, acl_ids: frozenset[int]) -> set[str]:
        """Pagine con almeno una riga derivata da una source visibile."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT DISTINCT r.page FROM row_map r JOIN docs d
                   ON d.path = r.source_ref
                   WHERE d.acl_id IN (SELECT value FROM json_each(?))""",
                (json.dumps(sorted(acl_ids)),)).fetchall()
        return {r[0] for r in rows}

    def doc_acl(self, paths: list[str]) -> dict[str, int]:
        if not paths:
            return {}
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT path, acl_id FROM docs WHERE path IN "
                "(SELECT value FROM json_each(?))", (json.dumps(paths),)).fetchall()
        return dict(rows)

    def query(self, q: str, limit: int = 20,
              acl_ids: frozenset[int] | None = None) -> list[dict]:
        """Ricerca FTS; le virgolette doppie nel testo vengono neutralizzate.
        acl_ids: None = nessun filtro; altrimenti solo righe la cui source
        ha uno di questi acl_id (le righe senza source restano escluse)."""
        q = q.replace('"', " ").strip()
        if not q:
            return []
        # Ogni termine tra virgolette: evita errori di sintassi FTS5 su
        # input libero (trattini, due punti...).
        match = " ".join(f'"{t}"' for t in q.split())
        sql = """SELECT page, section, title,
                        snippet(pages, 4, '**', '**', ' … ', 12),
                        source_ref, doc_id, content_version, bm25(pages) AS rank
                 FROM pages WHERE pages MATCH ?"""
        params: list = [match]
        if acl_ids is not None:
            sql += """ AND source_ref IN (SELECT path FROM docs WHERE acl_id IN
                                          (SELECT value FROM json_each(?)))"""
            params.append(json.dumps(sorted(acl_ids)))
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit * 3)          # margine per i duplicati per pagina
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        if acl_ids is not None:           # rete di sicurezza
            acl = self.doc_acl(sorted({r[4] for r in rows if r[4]}))
            rows = [r for r in rows if r[4] and acl.get(r[4]) in acl_ids]
        hits, seen = [], set()
        for page, section, title, snip, src, doc_id, version, _ in rows:
            if page in seen:
                continue
            seen.add(page)
            hits.append({"path": page, "section": section, "title": title,
                         "snippet": snip, "source": src, "doc_id": doc_id,
                         "version": version})
            if len(hits) >= limit:
                break
        return hits
