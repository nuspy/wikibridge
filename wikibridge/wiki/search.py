"""Indice full-text per progetto (SQLite FTS5, stdlib).

Righe di ricerca:
- per i documenti (source, riunioni, sintesi): una per ogni CHUNK
  (wiki/chunker.py); il riindicizzamento è incrementale — i chunk
  invariati non vengono toccati, quelli cambiati incrementano la propria
  versione (tabella chunk_versions, che sopravvive alle ricostruzioni
  dell'indice), quelli spariti vengono tolti;
- una per ogni SEGMENTO delle pagine entità/concetto (wiki/segments.py),
  più una per l'eventuale testo legacy non attribuibile;
- una per le altre pagine.

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

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..acl import ADMIN_ROLE, acl_key  # noqa: F401 — ADMIN_ROLE riesportato
from ..projects import Project
from .chunker import Chunk, chunk_markdown
from .segments import legacy_text, split_body
from .writer import WikiWriter

SCHEMA_VERSION = "7"

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
    heading UNINDEXED,
    page_title UNINDEXED,
    chunk_key UNINDEXED,
    chunk_version UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
);
CREATE TABLE IF NOT EXISTS row_map(
    rid INTEGER PRIMARY KEY, page TEXT NOT NULL, source_ref TEXT NOT NULL,
    chunk_key TEXT);
CREATE INDEX IF NOT EXISTS row_map_page ON row_map(page);
CREATE INDEX IF NOT EXISTS row_map_source ON row_map(source_ref);
CREATE TABLE IF NOT EXISTS docs(
    path TEXT PRIMARY KEY, doc_id TEXT, acl_id INTEGER NOT NULL,
    content_version INTEGER, title TEXT);
CREATE INDEX IF NOT EXISTS docs_acl ON docs(acl_id);
CREATE TABLE IF NOT EXISTS acl_sets(acl_id INTEGER PRIMARY KEY, key TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS chunk_history(
    page TEXT NOT NULL, chunk_key TEXT NOT NULL, version INTEGER NOT NULL,
    sha TEXT NOT NULL, heading TEXT, text TEXT, ordinal INTEGER,
    doc_version INTEGER, valid_from TEXT, valid_to TEXT,
    PRIMARY KEY (page, chunk_key, version));
CREATE INDEX IF NOT EXISTS chunk_history_current
    ON chunk_history(page) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS chunk_history_closed ON chunk_history(valid_to);
"""

# chunk_history NON è in elenco: è lo storico delle versioni dei chunk e
# deve sopravvivere alle ricostruzioni dell'indice (il testo attuale si
# rigenera dai Markdown, le versioni passate no)
_ALL_TABLES = ("pages", "row_map", "docs", "acl_sets", "meta")
# pagine che portano ACL proprie (documenti): le source, le riunioni e le
# sintesi (queste ultime visibili solo a chi vede tutte le loro fonti)
DOC_SECTIONS = ("sources", "meetings", "syntheses")
MAX_OR_TERMS = 32
_STOPWORDS = set("""
alla alle allo anche avere come con contro dalla dalle dallo degli delle
dello dopo dove essere fare fino hanno loro molto nella nelle nello noi
nostro perché però poi prima quale quando quanto quella quelle quello
questa queste questo sono sotto stato stessa stesso sulla sulle sullo tutti
tutto uno una vostro about after also been before being could from have
into more most only other over some such than that their them then there
these they this those very what when where which while will with would
""".split())
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
                    self._migrate_chunk_versions(conn)
                    self._rebuild_rows(conn)
                    conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema', ?)",
                                 (SCHEMA_VERSION,))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.isolation_level = previous

    @staticmethod
    def _migrate_chunk_versions(conn: sqlite3.Connection) -> None:
        """Schema 6 teneva solo il contatore di versione dei chunk: lo porta
        nello storico (senza testo, che per quelle versioni non esiste)."""
        if conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name='chunk_versions'").fetchone():
            conn.execute("""INSERT OR IGNORE INTO chunk_history
                            (page, chunk_key, version, sha, doc_version, valid_from)
                            SELECT page, chunk_key, version, sha, doc_version, valid_from
                            FROM chunk_versions""")
            conn.execute("DROP TABLE chunk_versions")

    # -- scrittura ---------------------------------------------------------------

    def _acl_id(self, conn: sqlite3.Connection, key: str) -> int:
        conn.execute("INSERT OR IGNORE INTO acl_sets(key) VALUES (?)", (key,))
        return conn.execute("SELECT acl_id FROM acl_sets WHERE key = ?",
                            (key,)).fetchone()[0]

    @staticmethod
    def _bump_docs(conn: sqlite3.Connection) -> None:
        """Le ACL dei documenti sono cambiate: invalida le cache che ne
        dipendono (visibilità delle sintesi)."""
        conn.execute("INSERT INTO meta VALUES ('docs_version', '1') "
                     "ON CONFLICT(k) DO UPDATE SET v = CAST(v AS INTEGER) + 1")

    def docs_version(self) -> int:
        with self._conn() as conn:
            row = conn.execute("SELECT v FROM meta WHERE k = 'docs_version'").fetchone()
        return int(row[0]) if row else 0

    def _delete_rows(self, conn: sqlite3.Connection, rel_path: str) -> None:
        rids = [r[0] for r in conn.execute(
            "SELECT rid FROM row_map WHERE page = ?", (rel_path,))]
        for rid in rids:
            conn.execute("DELETE FROM pages WHERE rowid = ?", (rid,))
        conn.execute("DELETE FROM row_map WHERE page = ?", (rel_path,))

    @staticmethod
    def _now() -> str:
        # millisecondi: due versioni scritte nello stesso secondo devono
        # restare distinguibili quando si ricostruisce il documento a una data
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds")

    @staticmethod
    def normalize_at(at: str) -> str:
        """Data richiesta (con o senza fuso, con "Z", o solo il giorno) nello
        stesso formato UTC dello storico, che si confronta come testo."""
        t = datetime.fromisoformat(at.strip().replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t.astimezone(timezone.utc).isoformat(timespec="milliseconds")

    def _delete_page(self, conn: sqlite3.Connection, rel_path: str) -> None:
        """Pagina rimossa: righe e documento; lo storico resta (chiuso),
        consultabile solo dall'admin perché non ci sono più ACL attuali."""
        self._delete_rows(conn, rel_path)
        conn.execute("UPDATE chunk_history SET valid_to = ? WHERE page = ? "
                     "AND valid_to IS NULL", (self._now(), rel_path))
        if conn.execute("DELETE FROM docs WHERE path = ?", (rel_path,)).rowcount:
            self._bump_docs(conn)

    def _purge_history(self, conn: sqlite3.Connection) -> None:
        days = int(getattr(self.project, "history_retention_days", 365) or 0)
        if days > 0:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)
                      ).isoformat(timespec="milliseconds")
            conn.execute("DELETE FROM chunk_history WHERE valid_to IS NOT NULL "
                         "AND valid_to < ?", (cutoff,))

    def _insert(self, conn, row_key, page, section, title, body, source_ref,
                doc_id="", version=0, heading="", page_title="", chunk_key="",
                chunk_version=0) -> None:
        cur = conn.execute(
            """INSERT INTO pages (row_key, page, section, title, body, source_ref,
                                  doc_id, content_version, heading, page_title,
                                  chunk_key, chunk_version)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (row_key, page, section, title, body, source_ref, doc_id, version,
             heading, page_title or title, chunk_key, chunk_version))
        conn.execute("INSERT INTO row_map VALUES (?,?,?,?)",
                     (cur.lastrowid, page, source_ref, chunk_key or None))

    def _index_doc(self, conn, rel_path: str, section: str, fm: dict,
                   body: str) -> None:
        """Documento: ACL in docs, testo in chunk. Incrementale: un chunk
        invariato (stesso hash, stesso titolo del documento) resta com'è;
        uno cambiato viene riscritto con versione +1; uno sparito tolto."""
        title = fm.get("title") or rel_path
        doc_id = fm.get("doc_id") or ""
        content_version = int(fm.get("content_version") or 0)
        key = acl_key(fm.get("import_id") or section, fm.get("acl_roles"),
                      fm.get("acl_classification"), fm.get("acl_source_clauses"),
                      fm.get("derived_from") if section == "syntheses" else None)
        old = conn.execute("SELECT title, doc_id FROM docs WHERE path = ?",
                           (rel_path,)).fetchone()
        conn.execute("INSERT OR REPLACE INTO docs VALUES (?,?,?,?,?)",
                     (rel_path, doc_id, self._acl_id(conn, key), content_version, title))
        self._bump_docs(conn)
        same_meta = old is not None and tuple(old) == (title, doc_id)

        rows = {k: rid for rid, k in conn.execute(
            "SELECT rid, chunk_key FROM row_map WHERE page = ?", (rel_path,))}
        # versione corrente di ogni chunk (quella con valid_to vuoto)
        known = {r[0]: r[1:] for r in conn.execute(
            "SELECT chunk_key, sha, version FROM chunk_history "
            "WHERE page = ? AND valid_to IS NULL", (rel_path,))}
        # un documento senza paragrafi (solo titoli, o vuoto) resta
        # comunque trovabile per titolo
        chunks = chunk_markdown(body) or [Chunk("intro~0.0", "", body.strip() or title)]
        now = self._now()
        keep: set[str] = set()
        for ordinal, c in enumerate(chunks):
            sha = hashlib.sha256(f"{c.heading}\x00{c.text}".encode("utf-8")).hexdigest()
            prev = known.get(c.key)
            changed = prev is None or prev[0] != sha
            keep.add(c.key)
            if changed:
                # storico: la versione precedente si chiude, non si cancella
                conn.execute("UPDATE chunk_history SET valid_to = ? WHERE page = ? "
                             "AND chunk_key = ? AND valid_to IS NULL",
                             (now, rel_path, c.key))
                last = conn.execute("SELECT MAX(version) FROM chunk_history WHERE "
                                    "page = ? AND chunk_key = ?",
                                    (rel_path, c.key)).fetchone()[0]
                version = (last or 0) + 1
                conn.execute("INSERT INTO chunk_history VALUES (?,?,?,?,?,?,?,?,?,NULL)",
                             (rel_path, c.key, version, sha, c.heading, c.text,
                              ordinal, content_version, now))
            else:
                version = prev[1]
                conn.execute("UPDATE chunk_history SET ordinal = ? WHERE page = ? AND "
                             "chunk_key = ? AND valid_to IS NULL",
                             (ordinal, rel_path, c.key))
                if same_meta and c.key in rows:
                    continue                              # chunk intatto
            if c.key in rows:
                conn.execute("DELETE FROM pages WHERE rowid = ?", (rows[c.key],))
                conn.execute("DELETE FROM row_map WHERE rid = ?", (rows[c.key],))
            self._insert(conn, f"{rel_path}#{c.key}", rel_path, section,
                         f"{title} — {c.heading}" if c.heading else title, c.text,
                         rel_path, doc_id, content_version, c.heading, title,
                         c.key, version)
        for k, rid in rows.items():
            if k not in keep:
                conn.execute("DELETE FROM pages WHERE rowid = ?", (rid,))
                conn.execute("DELETE FROM row_map WHERE rid = ?", (rid,))
        # chunk spariti dal documento: la loro ultima versione si chiude
        for k in set(known) - keep:
            conn.execute("UPDATE chunk_history SET valid_to = ? WHERE page = ? AND "
                         "chunk_key = ? AND valid_to IS NULL", (now, rel_path, k))
        self._purge_history(conn)

    def _index_page(self, conn, rel_path: str, section: str, fm: dict,
                    body: str) -> None:
        title = fm.get("title") or rel_path
        if section in DOC_SECTIONS:
            self._index_doc(conn, rel_path, section, fm, body)
            return
        self._delete_rows(conn, rel_path)
        if section in ("entities", "concepts"):
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
        # pagine che non esistono più: il loro storico si chiude (non si perde)
        conn.execute("UPDATE chunk_history SET valid_to = ? WHERE valid_to IS NULL "
                     "AND page NOT IN (SELECT path FROM docs)", (self._now(),))
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
        section = rel_path.split("/", 1)[0]
        key = acl_key(fm.get("import_id") or section,
                      fm.get("acl_roles"), fm.get("acl_classification"),
                      fm.get("acl_source_clauses"),
                      fm.get("derived_from") if section == "syntheses" else None)
        with self._conn() as conn:
            conn.execute("UPDATE docs SET acl_id = ? WHERE path = ?",
                         (self._acl_id(conn, key), rel_path))
            self._bump_docs(conn)

    def remove(self, rel_path: str) -> None:
        with self._conn() as conn:
            self._delete_page(conn, rel_path)

    def rebuild(self) -> int:
        """Reindicizza l'intera wiki del progetto. Ritorna il n. di pagine."""
        with self._conn() as conn:
            return self._rebuild_rows(conn)

    # -- storico ------------------------------------------------------------------

    def has_history(self, rel_path: str) -> bool:
        with self._conn() as conn:
            return conn.execute("SELECT 1 FROM chunk_history WHERE page = ? LIMIT 1",
                                (rel_path,)).fetchone() is not None

    def history(self, rel_path: str) -> list[dict]:
        """Tutte le versioni dei chunk di un documento, dalla più vecchia."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT chunk_key, heading, version, doc_version, valid_from,
                          valid_to, text IS NOT NULL FROM chunk_history
                   WHERE page = ? ORDER BY valid_from, ordinal, version""",
                (rel_path,)).fetchall()
        return [{"chunk": k, "heading": h or "", "version": v, "doc_version": dv,
                 "valid_from": vf, "valid_to": vt, "current": vt is None,
                 "has_text": bool(t)} for k, h, v, dv, vf, vt, t in rows]

    def document_at(self, rel_path: str, at: str) -> dict:
        """Il documento com'era all'istante `at` (ISO), ricostruito dai chunk
        validi in quel momento, nell'ordine in cui comparivano. I titoli
        sono resi come "## Sezione > Sottosezione" (il livello originale non
        è conservato)."""
        at = self.normalize_at(at)
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT heading, text, version, doc_version FROM chunk_history
                   WHERE page = ? AND valid_from <= ?
                     AND (valid_to IS NULL OR valid_to > ?)
                   ORDER BY ordinal""", (rel_path, at, at)).fetchall()
        parts, last_heading, missing = [], None, 0
        for heading, text, _, _ in rows:
            if text is None:
                missing += 1
                continue
            if heading and heading != last_heading:
                parts.append(f"## {heading}")
            last_heading = heading
            parts.append(text)
        return {"path": rel_path, "at": at, "chunks": len(rows),
                "doc_version": max((r[3] or 0) for r in rows) if rows else None,
                "missing_text": missing, "text": "\n\n".join(parts)}

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

    @staticmethod
    def _match_expr(q: str, any_terms: bool) -> str:
        """Ogni termine tra virgolette evita errori di sintassi FTS5 su input
        libero (trattini, due punti...). any_terms: OR tra i termini più
        significativi, per testi lunghi (trascrizioni, domande discorsive)
        dove l'AND di tutti i termini non troverebbe nulla."""
        terms = q.replace('"', " ").split()
        if not any_terms:
            return " ".join(f'"{t}"' for t in terms)
        counts: dict[str, int] = {}
        for t in terms:
            t = t.strip(".,;:!?()[]{}'«»").lower()
            if len(t) > 3 and t not in _STOPWORDS:
                counts[t] = counts.get(t, 0) + 1
        best = sorted(counts, key=lambda t: (-counts[t], -len(t)))[:MAX_OR_TERMS]
        return " OR ".join(f'"{t}"' for t in best)

    def _search_rows(self, q: str, max_rows: int, acl_ids: frozenset[int] | None,
                     any_terms: bool) -> list[dict]:
        """Righe (chunk, segmenti) in ordine di pertinenza, già filtrate per
        acl_ids (None = nessun filtro; altrimenti solo righe la cui source ha
        uno di quegli acl_id: le righe senza source restano escluse)."""
        match = self._match_expr(q, any_terms)
        if not match:
            return []
        sql = """SELECT page, section, page_title, heading, chunk_key, chunk_version,
                        snippet(pages, 4, '**', '**', ' … ', 12), body,
                        source_ref, doc_id, content_version, bm25(pages) AS rank
                 FROM pages WHERE pages MATCH ?"""
        params: list = [match]
        if acl_ids is not None:
            sql += """ AND source_ref IN (SELECT path FROM docs WHERE acl_id IN
                                          (SELECT value FROM json_each(?)))"""
            params.append(json.dumps(sorted(acl_ids)))
        sql += " ORDER BY rank LIMIT ?"
        params.append(max_rows)
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
            docs = {r[0]: r[1:] for r in conn.execute(
                "SELECT path, doc_id, content_version FROM docs WHERE path IN "
                "(SELECT value FROM json_each(?))",
                (json.dumps(sorted({r[8] for r in rows if r[8]})),))}
        if acl_ids is not None:           # rete di sicurezza
            acl = self.doc_acl(sorted({r[8] for r in rows if r[8]}))
            rows = [r for r in rows if r[8] and acl.get(r[8]) in acl_ids]
        out = []
        for (page, section, title, heading, ckey, cver, snip, body, src,
             doc_id, version, _) in rows:
            doc_id, version = docs.get(src, (doc_id, version))   # versione attuale
            out.append({"path": page, "section": section, "title": title,
                        "heading": heading or "", "chunk": ckey or "",
                        "chunk_version": cver or 0, "snippet": snip, "text": body,
                        "source": src, "doc_id": doc_id, "version": version})
        return out

    def query(self, q: str, limit: int = 20,
              acl_ids: frozenset[int] | None = None,
              any_terms: bool = False) -> list[dict]:
        """Ricerca per pagina (un risultato per pagina, dal chunk migliore);
        le virgolette doppie nel testo vengono neutralizzate."""
        hits, seen = [], set()
        for r in self._search_rows(q, limit * 4, acl_ids, any_terms):
            if r["path"] in seen:
                continue
            seen.add(r["path"])
            r.pop("text")
            hits.append(r)
            if len(hits) >= limit:
                break
        return hits

    def query_chunks(self, q: str, limit: int = 8,
                     acl_ids: frozenset[int] | None = None,
                     any_terms: bool = True, per_page: int = 3) -> list[dict]:
        """Chunk più pertinenti con il loro testo (per il contesto RAG): al
        massimo per_page dallo stesso documento, per non riempire il
        contesto con un solo documento."""
        out, per = [], {}
        for r in self._search_rows(q, limit * 4, acl_ids, any_terms):
            if per.get(r["path"], 0) >= per_page:
                continue
            per[r["path"]] = per.get(r["path"], 0) + 1
            r.pop("snippet")
            out.append(r)
            if len(out) >= limit:
                break
        return out
