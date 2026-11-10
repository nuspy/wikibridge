"""Suddivisione deterministica dei documenti in chunk.

Un chunk è un pezzo di una sezione del documento (i titoli markdown
delimitano le sezioni). La chiave di un chunk dipende dal percorso dei
titoli ("Contratti > Penali"), dall'occorrenza di quel percorso e dalla
posizione nella sezione: modificare o allungare una sezione cambia solo i
chunk di quella sezione, non quelli del resto del documento. Così un
aggiornamento riscrive nell'indice solo ciò che è cambiato, e ogni chunk
può tenere la propria versione.

I paragrafi non vengono spezzati, salvo quelli più lunghi di CHUNK_MAX.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

CHUNK_TARGET = 1200        # caratteri: si chiude il chunk dopo questa soglia
CHUNK_MAX = 2400           # un singolo paragrafo oltre questa lunghezza si divide
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_KEY_SAFE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class Chunk:
    key: str          # stabile: <percorso-titoli>~<occorrenza>.<parte>
    heading: str      # "Contratti > Penali" ("" = testo prima di ogni titolo)
    text: str


def _slug(path: str) -> str:
    return _KEY_SAFE.sub("-", path.lower()).strip("-")[:80] or "intro"


def _split_long(paragraph: str) -> list[str]:
    """Un paragrafo lunghissimo (tabella, testo senza a capo) in pezzi da
    CHUNK_MAX caratteri, tagliando a uno spazio."""
    parts = []
    while len(paragraph) > CHUNK_MAX:
        cut = paragraph.rfind(" ", 0, CHUNK_MAX)
        cut = cut if cut > CHUNK_MAX // 2 else CHUNK_MAX
        parts.append(paragraph[:cut].strip())
        paragraph = paragraph[cut:].strip()
    if paragraph:
        parts.append(paragraph)
    return parts


def chunk_markdown(text: str) -> list[Chunk]:
    sections: list[tuple[str, list[str]]] = [("", [])]
    stack: list[tuple[int, str]] = []
    for line in text.splitlines():
        m = _HEADING.match(line)
        if m:
            level, title = len(m.group(1)), m.group(2).strip()
            stack = [(lv, t) for lv, t in stack if lv < level] + [(level, title)]
            sections.append((" > ".join(t for _, t in stack), []))
        else:
            sections[-1][1].append(line)

    chunks: list[Chunk] = []
    seen: dict[str, int] = {}
    for heading, lines in sections:
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", "\n".join(lines))]
        paragraphs = [piece for p in paragraphs if p for piece in _split_long(p)]
        if not paragraphs:
            continue
        occurrence = seen.get(heading, 0)
        seen[heading] = occurrence + 1
        base = f"{_slug(heading)}~{occurrence}"
        part, current = 0, []
        for p in paragraphs:
            current.append(p)
            if sum(len(x) for x in current) >= CHUNK_TARGET:
                chunks.append(Chunk(f"{base}.{part}", heading, "\n\n".join(current)))
                part, current = part + 1, []
        if current:
            chunks.append(Chunk(f"{base}.{part}", heading, "\n\n".join(current)))
    return chunks
