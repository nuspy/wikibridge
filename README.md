# wikibridge

Importa la conoscenza di wiki esterne (MediaWiki, Confluence, Notion, HTML
generico/GitHub wiki) e di documenti locali (PDF, DOCX, ...) in una **LLM
Wiki** markdown locale, organizzata **per progetti**, usabile dagli agenti
(Hermes, OpenClaw, Claude Code) e dalla GUI Control Center di callAPIcall.

> README completo in arrivo a fine implementazione (fase P10).

## Avvio rapido

```bash
python -m venv .venv && .venv\Scripts\pip install -e .
wikibridge import https://en.wikipedia.org/wiki/Knowledge_graph
wikibridge search "knowledge graph"
```
