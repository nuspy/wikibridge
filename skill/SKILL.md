---
name: wiki-import
description: Importa wiki esterne (MediaWiki, Confluence, Notion, HTML generico)
  e documenti locali (PDF, DOCX, ...) nella LLM Wiki locale organizzata per
  progetti. Usare quando l'utente chiede di importare conoscenza da una wiki,
  creare un RAG locale da un link, catalogare documenti, gestire progetti di
  conoscenza o consultare la wiki di un progetto.
external: true
path: /mnt/c/Projects/wikibridge/skill
trigger: /wiki-import
category: knowledge
metadata:
  requires:
    bins: [python3, git]
---

# wiki-import — conoscenza esterna nella LLM Wiki locale

Tutti i comandi passano da:

```bash
python3 /mnt/c/Projects/wikibridge/skill/wikibridgectl.py <comando> [...]
```

I dati vivono in `/mnt/c/Projects/wikibridge/data/projects/<slug>/`:
`wiki/` (index.md, overview.md, log.md, sources/, entities/, concepts/,
syntheses/, meetings/), `graph/`, `index.db` (ricerca FTS), `_pending/`
(task di catalogazione che TU devi drenare, vedi sotto).

## Comandi

| Intento utente | Comando |
|---|---|
| /wiki-import <url> — importa una wiki | `wikibridgectl.py import <url> [--depth N] [--scope page\|subtree\|space] [--project slug] [--auth-profile p] [--max-pages N] [--adapter auto\|generic\|mediawiki\|confluence\|notion]` |
| /wiki-ingest <file\|dir> — importa documenti | `wikibridgectl.py ingest <path> [--recursive] [--project slug]` |
| /wiki-status — stato | `wikibridgectl.py status [--project slug]` |
| cerca nella conoscenza | `wikibridgectl.py search "<query>" [--project slug]` |
| gestione progetti | `wikibridgectl.py project create\|list\|switch\|delete ...` |
| sync di un import | `wikibridgectl.py sync <import-id>` |
| refresh di una pagina | `wikibridgectl.py refresh <wiki-path>` |
| /wiki-install-llmwiki — installa la skill llm-wiki | `wikibridgectl.py ensure-llmwiki` |
| profili auth | `wikibridgectl.py auth add <nome> --type bearer\|basic\|header\|cookie\|oauth2` |
| protezione OAuth/SSO di un URL | `wikibridgectl.py auth detect <url> [--save nome]` |
| login OAuth/SSO dal browser | `wikibridgectl.py auth login <nome>` (salva token/cookie; poi gli import sono automatici) |

Default sensati: depth 1, scope page, max 200 pagine — il caso "dammi un
RAG da questo link" funziona senza altri parametri.

## REGOLE per te (agente)

1. **Rispondere a domande di conoscenza di un progetto**: leggi PRIMA
   `/mnt/c/Projects/wikibridge/data/projects/<slug>/wiki/index.md`, poi
   apri le pagine linkate pertinenti. Usa `search` per query mirate.
   NON mescolare conoscenza tra progetti diversi.
2. **Catalogazione (modalità agent)**: in contesto skill il brain è TU.
   Dopo ogni `import`/`ingest` controlla `_pending/`:
   per ogni task leggi la source indicata, applica il prompt
   `/mnt/c/Projects/wikibridge/skill/prompts/catalog_page.md` e applica
   il risultato con:
   `wikibridgectl.py catalog-apply <source-path> --json '<json>' [--project slug]`
   (rimuove da solo il task da _pending/). Non chiamare API LLM esterne.
3. **Autorizzazione**: se l'import finisce in stato `auth_required` (o
   fallisce con 401/403), esegui `auth detect <url> --save <nome>` per
   riconoscere la protezione, poi `auth login <nome>`: si apre il browser,
   l'utente fa login (OAuth/SSO, MFA incluso) e token/cookie vengono
   salvati in secrets/ — da lì gli import futuri sono automatici (profilo
   selezionato per host, refresh silenzioso). Per credenziali statiche usa
   `auth add`: Notion integrazione condivisa con la pagina (bearer);
   Confluence Cloud email+API token (basic). I segreti non vanno mai nel
   repo.
4. **Prima volta**: esegui `ensure-llmwiki` per verificare/installare la
   skill llm-wiki sugli agenti (Hermes, OpenClaw, Claude Code).
