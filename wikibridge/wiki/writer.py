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

# Versione della pipeline di conversione/scrittura: un suo cambio altera il
# markdown senza che la sorgente sia cambiata, e va distinto da una nuova
# versione del documento.
PIPELINE_VERSION = "1"

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

    def _source_target(self, title: str, doc_id: str, url: str) -> Path:
        """Path per una source nuova: leggibile (slug del titolo) ma mai
        condiviso tra documenti diversi — due documenti con lo stesso
        titolo ricevono un suffisso invece di sovrascriversi."""
        slug = page_slug(title)
        path = self.wiki / "sources" / f"{slug}.md"
        if not path.is_file() or not (doc_id or url):
            return path
        fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        if doc_id and fm.get("doc_id") == doc_id:
            return path
        if not fm.get("doc_id") and url and fm.get("url") == url:
            return path                   # pagina legacy dello stesso documento
        suffix = hashlib.sha1((doc_id or url).encode("utf-8")).hexdigest()[:8]
        return self.wiki / "sources" / f"{slug}-{suffix}.md"

    def write_source(self, *, title: str, markdown: str, url: str = "",
                     source_kind: str = "web", import_id: str = "",
                     revision: str | None = None,
                     acl_roles: list[str] | None = None,
                     acl_classification: str | None = None,
                     extra_fm: dict | None = None,
                     rel_path: str | None = None,
                     doc_id: str = "",
                     native_id: str = "",
                     acl_source_clauses: list[list[str]] | None = None
                     ) -> tuple[Path, bool]:
        """Scrive (o aggiorna) una source. Ritorna (path, changed), con
        changed = contenuto, ACL o titolo cambiati (serve reindicizzare).

        Identità: con rel_path si aggiorna esattamente quella pagina, anche
        se alla sorgente il titolo è cambiato (niente duplicati); senza,
        il path viene da _source_target.

        ACL: None = eredita quelle già presenti nella pagina, così un
        aggiornamento del contenuto non azzera i tag.

        Versioni: content_version parte da 1 e cresce solo quando cambia il
        contenuto; first_seen, doc_id, native_id e pipeline_version
        predispongono il versioning (lo storico non è ancora conservato).
        """
        sha = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
        if rel_path:
            path = (self.wiki / rel_path).resolve()
            if path.parent != (self.wiki / "sources").resolve():
                raise ValueError(f"path non in sources/: {rel_path}")
        else:
            path = self._source_target(title, doc_id, url)
        old_fm: dict = {}
        if path.is_file():
            old_fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))

        old_roles = sorted(set(old_fm.get("acl_roles") or []))
        old_class = (old_fm.get("acl_classification")
                     or getattr(self.project, "default_acl_classification", "internal"))
        roles = old_roles if acl_roles is None else sorted(set(acl_roles))
        classification = acl_classification or old_class
        old_clauses = old_fm.get("acl_source_clauses") or []
        clauses = (old_clauses if acl_source_clauses is None else
                   sorted(sorted(set(c)) for c in acl_source_clauses))
        doc_id = doc_id or old_fm.get("doc_id", "")
        native_id = native_id or old_fm.get("native_id", "")

        content_changed = old_fm.get("content_sha256") != sha
        acl_changed = bool(old_fm) and (roles != old_roles
                                        or classification != old_class
                                        or clauses != old_clauses)
        meta_changed = bool(old_fm) and (
            old_fm.get("title") != title or old_fm.get("url") != url
            or old_fm.get("doc_id") != doc_id
            or "content_version" not in old_fm)
        if old_fm and not (content_changed or acl_changed or meta_changed):
            return path, False

        version = (int(old_fm.get("content_version", 1))
                   + (1 if content_changed else 0)) if old_fm else 1
        fm = {
            "title": title,
            "kind": source_kind,          # web | document | ...
            "url": url,
            "import_id": import_id or old_fm.get("import_id", ""),
            "revision": revision if content_changed else old_fm.get("revision", revision),
            "content_sha256": sha,
            "updated": now_iso() if content_changed else old_fm.get("updated", now_iso()),
            "acl_roles": roles,
            "acl_classification": classification,
            "acl_source_clauses": clauses,
            "doc_id": doc_id,
            "native_id": native_id,
            "content_version": version,
            "pipeline_version": PIPELINE_VERSION,
            "first_seen": old_fm.get("first_seen") or now_iso(),
        }
        if extra_fm:
            fm.update(extra_fm)
        self.write_page("sources", path.stem, fm, markdown)
        title_changed = bool(old_fm) and old_fm.get("title") != title
        return path, content_changed or acl_changed or title_changed

    def write_synthesis(self, *, title: str, body: str, sources: list[str]) -> Path:
        """Sintesi (risposta salvata come articolo) che deriva da `sources`:
        con le ACL attive la vede solo chi vede tutte quelle fonti. Le fonti
        devono essere documenti (sources/ o meetings/) esistenti; una sintesi
        non può derivare da un'altra sintesi."""
        sources = sorted(set(sources))
        if not sources:
            raise ValueError("una sintesi deve dichiarare le fonti da cui deriva")
        for s in sources:
            if s.split("/", 1)[0] not in ("sources", "meetings"):
                raise ValueError(f"fonte non ammessa (solo sources/ e meetings/): {s}")
            self.read_page(s)                      # FileNotFoundError se non esiste
        fm = {"title": title, "kind": "synthesis", "derived_from": sources,
              "import_id": "syntheses",
              "acl_classification": getattr(self.project, "default_acl_classification",
                                            "internal"),
              "updated": now_iso()}
        return self.write_page("syntheses", page_slug(title), fm, body)

    def update_source_fm(self, rel_path: str, **fields) -> dict:
        """Cambia solo il frontmatter (es. le ACL): corpo, content_sha256 e
        content_version restano invariati. Ritorna il nuovo frontmatter."""
        p = (self.wiki / rel_path).resolve()
        if p.parent != (self.wiki / "sources").resolve():
            raise ValueError(f"path non in sources/: {rel_path}")
        with self._lock:
            text = p.read_text(encoding="utf-8")
            m = _FM_RE.match(text)
            fm, _ = parse_frontmatter(text)
            fm.update(fields)
            body = text[m.end():] if m else text
            dumped = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False,
                                    default_flow_style=False).strip()
            p.write_text(f"---\n{dumped}\n---\n{body}", encoding="utf-8")
        return fm

    def remove_source(self, rel_path: str) -> None:
        p = (self.wiki / rel_path).resolve()
        if p.parent != (self.wiki / "sources").resolve():
            raise ValueError(f"path non in sources/: {rel_path}")
        p.unlink(missing_ok=True)

    def write_meeting(self, *, meeting_id: str, title: str, date: str,
                      source: str, participants: list[str],
                      summary: str, highlights: list[dict],
                      agreements: list[dict], tasks: list[dict],
                      transcript_path: str = "",
                      acl_roles: list[str] | None = None) -> Path:
        """Pagina riunione: Contenuto / Accordi / Attività. acl_roles: chi
        può vederla con le ACL attive (chi l'ha salvata e i partecipanti
        riconosciuti); l'admin la vede sempre."""
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
            "import_id": "meetings",
            "acl_roles": sorted(set(acl_roles or [])),
            "acl_classification": getattr(self.project, "default_acl_classification",
                                          "internal"),
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
