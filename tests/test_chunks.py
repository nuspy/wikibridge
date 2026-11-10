"""Chunk: chiavi stabili per sezione, aggiornamento incrementale senza
duplicati, versione per chunk che sopravvive alle ricostruzioni, contesto
RAG fatto delle sezioni pertinenti."""
import sqlite3

from wikibridge.rag import build_context
from wikibridge.wiki.chunker import CHUNK_MAX, chunk_markdown
from wikibridge.wiki.search import SearchIndex
from wikibridge.wiki.writer import WikiWriter

DOC = """Introduzione generale al contratto.

# Penali
In caso di ritardo si applica una penale del cinque per cento.

# Consegna
## Tempi
La consegna avviene entro trenta giorni lavorativi.

# Recesso
Il recesso richiede un preavviso di novanta giorni.
"""


def _keys(text):
    return {c.key: c for c in chunk_markdown(text)}


def test_chunks_follow_sections_and_nested_headings():
    ch = _keys(DOC)
    assert [c.heading for c in ch.values()] == ["", "Penali", "Consegna > Tempi", "Recesso"]
    assert "cinque per cento" in ch["penali~0.0"].text


def test_editing_one_section_does_not_change_the_others():
    before = _keys(DOC)
    after = _keys(DOC.replace("entro trenta giorni lavorativi.",
                              "entro trenta giorni lavorativi.\n\nSalvo accordi diversi."))
    changed = {k for k in before if before[k] != after.get(k)}
    assert changed == {"consegna-tempi~0.0"}


def test_long_sections_and_paragraphs_are_split():
    long_section = "# Lunga\n" + "\n\n".join(f"paragrafo {i} " + "x" * 300 for i in range(10))
    parts = [c for c in chunk_markdown(long_section) if c.heading == "Lunga"]
    assert len(parts) > 1 and all(len(c.text) <= 2 * 1300 for c in parts)
    giant = "# G\n" + " ".join(["parola"] * 2000)
    assert all(len(c.text) <= CHUNK_MAX for c in chunk_markdown(giant))


def _index(project, body, title="Contratto"):
    w = WikiWriter(project)
    p, _ = w.write_source(title=title, markdown=body, doc_id="d:1",
                          rel_path="sources/contratto.md"
                          if (project.wiki_dir / "sources" / "contratto.md").exists() else None)
    rel = f"sources/{p.name}"
    idx = SearchIndex(project)
    idx.upsert_page(rel, "sources", *w.read_page(rel))
    return idx, rel


def _chunks(project):
    conn = sqlite3.connect(project.index_db)
    rows = conn.execute("SELECT m.chunk_key, m.rid, v.version FROM row_map m JOIN "
                        "chunk_history v ON v.page = m.page AND v.chunk_key = m.chunk_key "
                        "AND v.valid_to IS NULL").fetchall()
    conn.close()
    return {k: (rid, ver) for k, rid, ver in rows}


def test_incremental_update_touches_only_changed_chunks(project):
    _index(project, DOC)
    before = _chunks(project)
    assert all(v == 1 for _, v in before.values())
    _index(project, DOC.replace("cinque per cento", "dieci per cento"))
    after = _chunks(project)
    assert after["penali~0.0"][1] == 2                         # nuova versione
    for k in ("intro~0.0", "consegna-tempi~0.0", "recesso~0.0"):
        assert after[k] == before[k]                          # stessa riga, stessa versione
    assert len(after) == len(before)                          # nessun duplicato

    _index(project, DOC.replace("# Recesso\nIl recesso richiede un preavviso di "
                                "novanta giorni.\n", ""))
    assert "recesso~0.0" not in _chunks(project)              # sezione tolta


def test_chunk_versions_survive_index_rebuild(project):
    idx, _ = _index(project, DOC)
    _index(project, DOC.replace("cinque", "sei"))
    idx.rebuild()
    assert _chunks(project)["penali~0.0"][1] == 2


def test_search_and_rag_return_the_relevant_section(project):
    filler = "\n\n".join(f"# Capitolo {i}\n" + f"testo generico numero {i} " * 40
                         for i in range(12))
    idx, rel = _index(project, filler + "\n\n# Riservatezza\nObbligo di "
                      "riservatezza quinquennale sulle informazioni.")
    hit = idx.query("riservatezza")[0]
    assert hit["heading"] == "Riservatezza" and hit["chunk"].startswith("riservatezza")
    ctx = build_context(project, idx, None, "obbligo riservatezza quinquennale")
    texts = [b["text"] for b in ctx["blocks"] if b["path"] == rel]
    assert texts[0].startswith("Obbligo di riservatezza")
    assert all("testo generico" not in t for t in texts[:1])
    assert ctx["blocks"][0]["heading"] == "Riservatezza"
