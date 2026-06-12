"""Catalogazione: dalle sources a entità/concetti/relazioni del progetto.

Map-reduce come callbridge/analysis.py: la pagina viene spezzata in
chunk (~catalog_chunk_chars) su confini di paragrafo, ogni chunk produce
un JSON parziale {entities, concepts, relations}, i parziali si fondono
per nome normalizzato e si scrivono in entities/<slug>.md e
concepts/<slug>.md (sezione "Menzioni" con i backlink alle sources).

In modalità brain=agent il lavoro NON avviene qui: catalog_enqueue
scrive il task in _pending/ e l'agente ospite lo drena con
`wikibridgectl catalog-apply <task> --json '<risultato>'`, che richiama
apply_catalog_result() — la scrittura resta in questo modulo.
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from pathlib import Path

from .brain import make_brain
from .config import Config
from .projects import Project
from .wiki.search import SearchIndex
from .wiki.writer import WikiWriter, now_iso, page_slug

log = logging.getLogger("wikibridge.catalog")

SYSTEM_PROMPT = (
    "Sei un catalogatore di conoscenza. Estrai dal testo entità e concetti "
    "rilevanti e rispondi SOLO con JSON valido, senza testo attorno.")

MAP_PROMPT = """Analizza questo estratto di documento e cataloga la conoscenza.

Rispondi SOLO con JSON in questo schema:
{{
 "entities": [{{"name": "...", "type": "persona|organizzazione|sistema|luogo|prodotto",
                "summary": "1-3 frasi esaustive su cosa dice il testo di questa entità"}}],
 "concepts": [{{"name": "...", "summary": "1-3 frasi che spiegano il concetto come appare nel testo"}}],
 "relations": [{{"from": "...", "to": "...", "kind": "verbo o relazione breve"}}]
}}

Includi solo elementi sostanziali (non parole generiche). Testo:

{chunk}"""

_WS_RE = re.compile(r"\s+")


def normalize_name(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return _WS_RE.sub(" ", s.lower().strip())


def _split_chunks(text: str, chunk_chars: int) -> list[str]:
    if len(text) <= chunk_chars:
        return [text]
    chunks, current = [], []
    size = 0
    for para in text.split("\n\n"):
        if size + len(para) > chunk_chars and current:
            chunks.append("\n\n".join(current))
            current, size = [], 0
        current.append(para)
        size += len(para) + 2
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def merge_partials(partials: list[dict]) -> dict:
    """Fonde i JSON parziali per nome normalizzato."""
    entities: dict[str, dict] = {}
    concepts: dict[str, dict] = {}
    relations: list[dict] = []
    seen_rel: set[tuple] = set()
    for p in partials:
        if not p:
            continue
        for e in p.get("entities", []):
            if not e.get("name"):
                continue
            key = normalize_name(e["name"])
            if key in entities:
                if e.get("summary") and e["summary"] not in entities[key]["summary"]:
                    entities[key]["summary"] += " " + e["summary"]
            else:
                entities[key] = {"name": e["name"],
                                 "type": e.get("type", ""),
                                 "summary": e.get("summary", "")}
        for c in p.get("concepts", []):
            if not c.get("name"):
                continue
            key = normalize_name(c["name"])
            if key in concepts:
                if c.get("summary") and c["summary"] not in concepts[key]["summary"]:
                    concepts[key]["summary"] += " " + c["summary"]
            else:
                concepts[key] = {"name": c["name"],
                                 "summary": c.get("summary", "")}
        for r in p.get("relations", []):
            key = (normalize_name(str(r.get("from", ""))),
                   normalize_name(str(r.get("to", ""))),
                   str(r.get("kind", "")).lower())
            if all(key) and key not in seen_rel:
                seen_rel.add(key)
                relations.append(r)
    return {"entities": list(entities.values()),
            "concepts": list(concepts.values()),
            "relations": relations}


def apply_catalog_result(project: Project, source_rel: str,
                         result: dict) -> dict:
    """Scrive entità/concetti nel wiki (merge con le pagine esistenti)."""
    writer = WikiWriter(project)
    index = SearchIndex(project)
    written = {"entities": 0, "concepts": 0}

    for kind, section in (("entities", "entities"), ("concepts", "concepts")):
        for item in result.get(kind, []):
            name = item.get("name", "").strip()
            if not name:
                continue
            slug = page_slug(name)
            rel = f"{section}/{slug}.md"
            mentions: list[str] = []
            body_intro = item.get("summary", "")
            existing_fm: dict = {}
            try:
                existing_fm, old_body = writer.read_page(rel)
                mentions = existing_fm.get("mentions", []) or []
                # mantieni il corpo già arricchito, aggiungi solo se nuovo
                if body_intro and body_intro not in old_body:
                    body = old_body.strip() + "\n\n" + body_intro
                else:
                    body = old_body
            except (FileNotFoundError, OSError):
                body = f"# {name}\n\n{body_intro}"
            if source_rel not in mentions:
                mentions.append(source_rel)
            fm = {
                "title": name,
                "kind": "entity" if kind == "entities" else "concept",
                "type": item.get("type", existing_fm.get("type", "")),
                "mentions": mentions,
                "updated": now_iso(),
            }
            mentions_md = "\n".join(f"- [[{m}]]" for m in mentions)
            full_body = (body.strip() + "\n\n## Menzioni\n\n" + mentions_md
                         if "## Menzioni" not in body
                         else re.sub(r"## Menzioni\n.*", "## Menzioni\n\n" + mentions_md,
                                     body, flags=re.DOTALL))
            writer.write_page(section, slug, fm, full_body)
            fm2, body2 = writer.read_page(rel)
            index.upsert(rel, section, name, body2)
            written[kind] += 1

    relations = result.get("relations", [])
    if relations:
        rel_store = project.graph_dir / "relations.jsonl"
        with rel_store.open("a", encoding="utf-8") as f:
            for r in relations:
                f.write(json.dumps({**r, "source": source_rel},
                                   ensure_ascii=False) + "\n")

    writer.append_log(
        f"catalogata {source_rel}: {written['entities']} entità, "
        f"{written['concepts']} concetti, {len(relations)} relazioni")
    return written


def catalog_source(cfg: Config, project: Project, source_rel: str,
                   brain=None) -> dict | None:
    """Cataloga una source. Con brain=agent accoda il task e ritorna None."""
    brain = brain or make_brain(cfg)
    writer = WikiWriter(project)
    fm, body = writer.read_page(source_rel)

    if brain.mode == "agent":
        task_path = project.pending_dir / f"{Path(source_rel).stem}.json"
        task_path.write_text(json.dumps({
            "source": source_rel, "title": fm.get("title", ""),
            "prompt": "catalog_page", "queued": now_iso(),
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        return None
    if brain.mode == "none":
        return None

    chunks = _split_chunks(body, cfg.catalog_chunk_chars)
    partials = []
    for chunk in chunks:
        out = brain.complete_json(SYSTEM_PROMPT, MAP_PROMPT.format(chunk=chunk))
        if out:
            partials.append(out)
    if not partials:
        log.warning("catalogazione senza risultati per %s", source_rel)
        return None
    merged = merge_partials(partials)
    return apply_catalog_result(project, source_rel, merged)


def make_catalog_hook(cfg: Config):
    """Hook (project, source_rel) per importer/ingest. None se brain=none."""
    brain = make_brain(cfg)
    if brain.mode == "none":
        return None

    def hook(project: Project, source_rel: str) -> None:
        try:
            catalog_source(cfg, project, source_rel, brain=brain)
        except Exception as e:  # noqa: BLE001 — la catalogazione non rompe l'import
            log.warning("catalogazione fallita per %s: %s", source_rel, e)

    return hook


def drain_pending(cfg: Config, project: Project) -> dict:
    """Cataloga tutti i task in _pending/ con il brain HTTP (uso da GUI)."""
    brain = make_brain(cfg, mode="openai_http")
    done, failed = 0, []
    for task_file in sorted(project.pending_dir.glob("*.json")):
        task = json.loads(task_file.read_text(encoding="utf-8"))
        try:
            if catalog_source(cfg, project, task["source"], brain=brain):
                done += 1
                task_file.unlink()
            else:
                failed.append(task["source"])
        except Exception as e:  # noqa: BLE001
            failed.append(f"{task['source']}: {e}")
    return {"done": done, "failed": failed}
