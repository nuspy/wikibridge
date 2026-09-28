"""Segmenti delle pagine derivate (entità, concetti).

Ogni riassunto è marcato con la source da cui deriva:

    <!-- wb:src sources/x.md -->
    testo
    <!-- /wb:src -->

così un aggiornamento della source sostituisce il suo segmento, la
rimozione lo toglie, e il filtro ACL mostra a ciascuno solo i segmenti
che derivano da source che può vedere. Il testo senza marcatori è legacy
(scritto prima dei segmenti): non è attribuibile, quindi con le ACL attive
resta visibile solo all'admin finché non si ricataloga.
"""
from __future__ import annotations

import re

_SEG_RE = re.compile(r"<!-- wb:src (?P<src>\S+) -->\n(?P<text>.*?)\n<!-- /wb:src -->\n*",
                     re.DOTALL)
_MENTIONS_RE = re.compile(r"\n*## Menzioni\n.*", re.DOTALL)
_HEADER_RE = re.compile(r"\A#[^\n]*\n*")


def split_body(body: str) -> tuple[str, dict[str, str]]:
    """(testo legacy con intestazione, {source: segmento})."""
    main = _MENTIONS_RE.sub("", body)
    segments = {m.group("src"): m.group("text").strip()
                for m in _SEG_RE.finditer(main)}
    return _SEG_RE.sub("", main).strip(), segments


def legacy_text(rest: str) -> str:
    """Il testo legacy senza la riga di titolo."""
    return _HEADER_RE.sub("", rest).strip()


def render_body(rest: str, segments: dict[str, str], mentions: list[str]) -> str:
    parts = [rest] + [f"<!-- wb:src {src} -->\n{text}\n<!-- /wb:src -->"
                      for src, text in sorted(segments.items()) if text]
    body = "\n\n".join(p for p in parts if p)
    return body + "\n\n## Menzioni\n\n" + "\n".join(f"- [[{m}]]" for m in mentions)


def filter_body(title: str, body: str, visible_sources: set[str]) -> str | None:
    """Vista della pagina derivata per chi vede solo visible_sources:
    niente testo legacy, solo segmenti e menzioni visibili. None se non
    resta nulla."""
    _, segments = split_body(body)
    kept = {s: t for s, t in segments.items() if s in visible_sources}
    if not kept:
        return None
    return render_body(f"# {title}", kept, sorted(kept))


def strip_markers(body: str) -> str:
    return re.sub(r"<!-- /?wb:src[^>]*-->\n?", "", body)
