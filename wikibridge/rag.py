"""Contesto RAG filtrato per l'inferenza.

È il punto da cui un LLM deve prendere la conoscenza di un progetto: al
posto di leggere la wiki dal disco (che scavalca ogni ACL), chi fa
inferenza chiede qui i blocchi pertinenti a una domanda o a una
trascrizione e riceve solo testo che chi la richiede, e ciascuno dei
partecipanti indicati, può vedere.

Politica per le riunioni: il contesto è l'INTERSEZIONE di ciò che vedono
il richiedente e i partecipanti (il bot parla a tutti i presenti). Un
partecipante non riconosciuto dal directory (esterno, nome non univoco)
conta come un utente senza ruoli: restano solo i contenuti "public".
"""
from __future__ import annotations

from .projects import Project
from .wiki.search import SearchIndex

MIN_BLOCK_CHARS = 200


def intersect(sets: list[frozenset[int] | None]) -> frozenset[int] | None:
    """None = nessun filtro (admin o ACL non attive)."""
    restricted = [s for s in sets if s is not None]
    if not restricted:
        return None
    return frozenset.intersection(*restricted)


def build_context(project: Project, index: SearchIndex,
                  acl_ids: frozenset[int] | None, query: str, *,
                  limit: int = 8, max_chars: int = 12000) -> dict:
    """I chunk più pertinenti (non l'inizio delle pagine): per i documenti
    la sezione che risponde, per le entità il riassunto derivato da una
    source visibile. Il filtro ACL è già nella ricerca: ogni riga passa solo
    se la sua source è visibile."""
    blocks, used, truncated = [], 0, False
    for h in index.query_chunks(query, limit=limit, acl_ids=acl_ids):
        remaining = max_chars - used
        if remaining < MIN_BLOCK_CHARS:
            truncated = True
            break
        text = h["text"].strip()
        if len(text) > remaining:
            text = text[:remaining].rsplit(" ", 1)[0] + " …"
            truncated = True
        used += len(text)
        blocks.append({"path": h["path"], "title": h["title"],
                       "heading": h["heading"], "chunk": h["chunk"],
                       "chunk_version": h["chunk_version"],
                       "section": h["section"], "source": h["source"],
                       "doc_id": h["doc_id"], "version": h["version"],
                       "text": text})
    return {"blocks": blocks, "truncated": truncated, "chars": used}
