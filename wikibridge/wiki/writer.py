"""Scrittura della LLM Wiki (formato llm-wiki-agent).

Layout per progetto, dentro wiki/:
  index.md      catalogo di tutte le pagine, per sezione
  overview.md   sintesi corrente (aggiornata dal cataloguer)
  log.md        registro cronologico delle operazioni
  sources/      una pagina per documento/pagina importata
  entities/     persone/organizzazioni/sistemi (dal cataloguer)
  concepts/     idee e temi (dal cataloguer)
  syntheses/    risposte/sintesi salvate come articoli
  meetings/     una pagina per riunione (Contenuto/Accordi/Attività)

Ogni pagina ha frontmatter YAML; le sources portano url, revision e
content_sha256 per la dedup e il sync incrementale.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

import yaml

from ..projects import Project, WIKI_SECTIONS

_SLUG_RE = re.compile(r"[^a-z0-9-]+")
_FM_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)

SECTION_TITLES = {
    "sources": "Fonti",
    "entities": "Entità",
    "concepts": "Concetti",
    "syntheses": "Sintesi",
    "meetings": "Riunioni",
}


def page_slug(title: str, max_len: int = 80) -> str:
    s = _SLUG_RE.sub("-", title.lower().strip()).strip("-")[:max_len]
    return s.strip("-") or "pagina"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Ritorna (frontmatter, corpo). Frontmatter vuoto se assente/invalido."""
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return {}, text
    return (fm if isinstance(fm, dict) else {}), text[m.end():]


def render_page(frontmatter: dict, body: str) -> str:
    fm = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False,
                        default_flow_style=False).strip()
    return f"---\n{fm}\n---\n\n{body.strip()}\n"


class WikiWriter:
    """Tutte le scritture della wiki di UN progetto passano da qui."""

    def __init__(self, project: Project):
        self.project = project
        self.wiki = project.wiki_dir
        self._lock = threading.Lock()
        project.ensure_layout()

    # -- pagine --------------------------------------------------------------

    def write_page(self, section: str, slug: str, frontmatter: dict,
                   body: str) -> Path:
        if section not in WIKI_SECTIONS:
            raise ValueError(f"sezione sconosciuta: {section}")
        frontmatter.setdefault("updated", now_iso())
        path = self.wiki / section / f"{slug}.md"
        with self._lock:
            path.write_text(render_page(frontmatter, body), encoding="utf-8")
        return path

    def read_page(self, rel_path: str) -> tuple[dict, str]:
        p = (self.wiki / rel_path).resolve()
        if not str(p).startswith(str(self.wiki.resolve())):
            raise ValueError("path fuori dalla wiki")
        fm, body = parse_frontmatter(p.read_text(encoding="utf-8"))
        return fm, body

    def write_source(self, *, title: str, markdown: str, url: str = "",
                     source_kind: str = "web", import_id: str = "",
                     revision: str | None = None,
                     extra_fm: dict | None = None) -> tuple[Path, bool]:
        """Scrive (o aggiorna) una source. Ritorna (path, changed).

        Dedup: se la pagina esiste con lo stesso content_sha256 non riscrive
        (changed=False) — il sync incrementale conta su questo.
        """
        slug = page_slug(title)
        sha = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
        path = self.wiki / "sources" / f"{slug}.md"
        if path.is_file():
            old_fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
            if old_fm.get("content_sha256") == sha:
                return path, False
        fm = {
            "title": title,
            "kind": source_kind,          # web | document | ...
            "url": url,
            "import_id": import_id,
            "revision": revision,
            "content_sha256": sha,
            "updated": now_iso(),
        }
        if extra_fm:
            fm.update(extra_fm)
        self.write_page("sources", slug, fm, markdown)
        return path, True

    def write_meeting(self, *, meeting_id: str, title: str, date: str,
                      source: str, participants: list[str],
                      summary: str, highlights: list[dict],
                      agreements: list[dict], tasks: list[dict],
                      transcript_path: str = "") -> Path:
        """Pagina riunione: Contenuto / Accordi / Attività."""
        slug = page_slug(f"{date[:10]}-{title}")
        lines = [f"# {title}", ""]
        lines += ["## Contenuto", "", summary.strip(), ""]
        for h in highlights:
            lines += [f"### {h.get('title', '')}", "", str(h.get("summary", "")).strip(), ""]
        lines += ["## Accordi", ""]
        if agreements:
            for a in agreements:
                detail = str(a.get("detail", "")).strip()
                lines.append(f"- **{a.get('title', '')}**" + (f": {detail}" if detail else ""))
        else:
            lines.append("_Nessun accordo registrato._")
        lines += ["", "## Attività", ""]
        if tasks:
            for t in tasks:
                lines.append(f"- [ ] **{t.get('assignee', '?')}** — {t.get('task', '')}")
        else:
            lines.append("_Nessuna attività registrata._")
        if transcript_path:
            lines += ["", f"[Trascrizione completa]({transcript_path})"]
        fm = {
            "title": title,
            "meeting_id": meeting_id,
            "date": date,
            "source": source,             # meetbot | copilot | avatar
            "participants": participants,
            "updated": now_iso(),
        }
        return self.write_page("meetings", slug, fm, "\n".join(lines))

    # -- index / log / overview ----------------------------------------------

    def append_log(self, line: str) -> None:
        log = self.wiki / "log.md"
        with self._lock:
            if not log.is_file():
                log.write_text("# Log\n\n", encoding="utf-8")
            with log.open("a", encoding="utf-8") as f:
                f.write(f"- {now_iso()} — {line}\n")

    def rebuild_index(self) -> Path:
        """Rigenera index.md elencando ogni pagina per sezione."""
        out = [f"# Indice — {self.project.name}", ""]
        for section in WIKI_SECTIONS:
            pages = sorted((self.wiki / section).glob("*.md"))
            if not pages:
                continue
            out += [f"## {SECTION_TITLES[section]}", ""]
            for p in pages:
                fm, _ = parse_frontmatter(p.read_text(encoding="utf-8"))
                title = fm.get("title") or p.stem
                out.append(f"- [{title}]({section}/{p.name})")
            out.append("")
        idx = self.wiki / "index.md"
        with self._lock:
            idx.write_text("\n".join(out) + "\n", encoding="utf-8")
        return idx

    def write_overview(self, markdown: str) -> Path:
        p = self.wiki / "overview.md"
        with self._lock:
            p.write_text(markdown.strip() + "\n", encoding="utf-8")
        return p

    # -- navigazione -----------------------------------------------------------

    def tree(self) -> dict:
        """{section: [{file, title, updated}]} per la GUI."""
        result: dict = {}
        for section in WIKI_SECTIONS:
            entries = []
            for p in sorted((self.wiki / section).glob("*.md")):
                fm, _ = parse_frontmatter(p.read_text(encoding="utf-8"))
                entries.append({
                    "file": f"{section}/{p.name}",
                    "title": fm.get("title") or p.stem,
                    "updated": fm.get("updated", ""),
                })
            result[section] = entries
        return result

    def iter_pages(self):
        """Genera (rel_path, frontmatter, body) per tutte le pagine."""
        for section in WIKI_SECTIONS:
            for p in sorted((self.wiki / section).glob("*.md")):
                fm, body = parse_frontmatter(p.read_text(encoding="utf-8"))
                yield f"{section}/{p.name}", fm, body
