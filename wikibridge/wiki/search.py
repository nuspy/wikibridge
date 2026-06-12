"""Indice full-text per progetto (SQLite FTS5, stdlib).

Una riga per pagina wiki: path relativo, sezione, titolo, corpo.
L'indice si aggiorna in modo incrementale (upsert per path) o si
ricostruisce da zero con rebuild(). Query con snippet evidenziati.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from ..projects import Project
from .writer import WikiWriter

_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS pages USING fts5(
    path UNINDEXED,
    section UNINDEXED,
    title,
    body,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""


class SearchIndex:
    def __init__(self, project: Project):
        self.project = project
        self.db_path = project.index_db

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute(_SCHEMA)
        return conn

    def upsert(self, rel_path: str, section: str, title: str, body: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM pages WHERE path = ?", (rel_path,))
            conn.execute(
                "INSERT INTO pages (path, section, title, body) VALUES (?,?,?,?)",
                (rel_path, section, title, body))

    def remove(self, rel_path: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM pages WHERE path = ?", (rel_path,))

    def rebuild(self) -> int:
        """Reindicizza l'intera wiki del progetto. Ritorna il n. di pagine."""
        writer = WikiWriter(self.project)
        n = 0
        with self._conn() as conn:
            conn.execute("DELETE FROM pages")
            for rel_path, fm, body in writer.iter_pages():
                section = rel_path.split("/", 1)[0]
                conn.execute(
                    "INSERT INTO pages (path, section, title, body) VALUES (?,?,?,?)",
                    (rel_path, section, fm.get("title") or rel_path, body))
                n += 1
        return n

    def query(self, q: str, limit: int = 20) -> list[dict]:
        """Ricerca FTS; le virgolette doppie nel testo vengono neutralizzate."""
        q = q.replace('"', " ").strip()
        if not q:
            return []
        # Ogni termine tra virgolette: evita errori di sintassi FTS5 su
        # input libero (trattini, due punti...).
        match = " ".join(f'"{t}"' for t in q.split())
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT path, section, title,
                          snippet(pages, 3, '**', '**', ' … ', 12) AS snip,
                          bm25(pages) AS rank
                   FROM pages WHERE pages MATCH ?
                   ORDER BY rank LIMIT ?""",
                (match, limit)).fetchall()
        return [
            {"path": r[0], "section": r[1], "title": r[2], "snippet": r[3]}
            for r in rows
        ]
