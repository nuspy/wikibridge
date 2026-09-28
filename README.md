# wikibridge

Importa la conoscenza di **wiki esterne** (MediaWiki, Confluence, Notion,
HTML generico/GitHub wiki) e di **documenti locali** (PDF, DOCX, PPTX,
XLSX, HTML, MD, ...) in una **LLM Wiki** markdown locale, organizzata
**per progetti**, usabile:

- dagli **agenti** (Hermes in WSL, OpenClaw, Claude Code) tramite la skill
  `wiki-import` inclusa;
- dalla **GUI** Control Center di callAPIcall (sezione "Conoscenza");
- da **CLI** (`wikibridge ...`).

Le **riunioni** (meetbot e copilota/avatar di callAPIcall) possono essere
inserite nella sezione `meetings/` del wiki del progetto con contenuto,
**accordi** e **attività**.

## Concetti

- **Progetto**: unità di isolamento della conoscenza. Ogni progetto ha la
  sua wiki, il suo indice di ricerca e il suo grafo; gli agenti ricevono
  solo la conoscenza del progetto collegato (mai quella degli altri).
- **LLM Wiki** (formato llm-wiki-agent): markdown interlinkato in
  `wiki/` con `index.md` (catalogo), `overview.md`, `log.md` e le sezioni
  `sources/` (pagine/documenti importati), `entities/` e `concepts/`
  (catalogazione LLM con backlink "Menzioni"), `syntheses/` (risposte
  salvate), `meetings/` (riunioni). Grafo in `graph/graph.html`.
- **RAG istantaneo**: `wikibridge import <url>` con i soli default
  (profondità 1, max 200 pagine) produce una knowledge base navigabile e
  cercabile da un singolo link.

## Layout dati

```
data/projects/<slug>/
├── project.json   imports.json   index.db (FTS5)
├── wiki/{index.md, overview.md, log.md,
│         sources/, entities/, concepts/, syntheses/, meetings/}
├── graph/{graph.json, graph.html, relations.jsonl}
└── _pending/      task di catalogazione per la modalità brain=agent
```

`data/` e `secrets/` sono **gitignorati** (conoscenza e credenziali non
entrano nel repo).

## Installazione

```powershell
cd C:\Projects\wikibridge
py -3.12 -m venv .venv
.venv\Scripts\pip install -e .
# skill per gli agenti (junction Claude Code + symlink Hermes WSL):
.\scripts\install_skill.ps1
```

Server HTTP per la GUI (porta 8775):

```powershell
.venv\Scripts\wikibridge serve
```

Per l'avvio automatico: aggiungere una chiave `HKCU\...\Run` o un task
dell'Utilità di pianificazione che lancia `wikibridge serve` (stesso
playbook dei supervisori di callAPIcall).

## CLI

```bash
wikibridge project create|list|switch|delete ...
wikibridge import <url> [--depth 0..3] [--scope page|subtree|space]
                  [--max-pages N] [--auth-profile NOME]
                  [--adapter auto|generic|mediawiki|confluence|notion]
                  [--include GLOB] [--exclude GLOB] [--no-catalog]
wikibridge sync <import-id>        # rifetch SOLO delle pagine cambiate
wikibridge refresh <wiki-path>     # refresh puntuale di una pagina
wikibridge ingest <file|dir> [--recursive]
wikibridge search "query"          # FTS5 con snippet
wikibridge index                   # ricostruisce indice FTS + index.md
wikibridge graph                   # rigenera graph.json/graph.html
wikibridge catalog-drain           # cataloga i task _pending con LM Studio
wikibridge auth add <nome> --type bearer|basic|header|cookie|oauth2
wikibridge auth detect <url> [--save NOME]   # riconosce OAuth/SSO/basic
wikibridge auth login <nome> [--no-browser]  # login dal browser, salva i token
wikibridge serve                   # API HTTP :8775
wikibridge skill ensure-llmwiki    # verifica/installa la skill llm-wiki
```

Opzione globale `--project <slug>` (default: progetto attivo).

## Profondità, estensione, limiti

- `--scope`: `page` (la pagina), `subtree` (categoria MediaWiki / figli
  Confluence-Notion / prefisso path), `space` (spazio/namespace intero).
- `--depth`: BFS sui link **dello stesso wiki** (0 = solo seed; max 3).
- Limiti (override per import o in `config.json`): `max_pages` 200,
  `max_page_mb` 2, `max_total_mb` 200, `rate_limit_rps` 1/host,
  `max_runtime_min` 30, `respect_robots` true, allegati non scaricati.
- Import troncati salvano la **frontiera** e sono riprendibili; ogni
  source registra `revision` + `content_sha256` per il **sync
  incrementale** (MediaWiki revid, Confluence version, Notion
  last_edited_time, ETag/Last-Modified per l'HTML).

## Autenticazione

Profili in `secrets/auth_profiles.json` (mai nel repo; i valori accettano
l'indirection `env:VARIABILE`). Tipi: `bearer` (Notion, PAT Confluence
Server), `basic` (Confluence Cloud: email + API token), `header`,
`cookie` (SSO aziendale a sessione), `oauth2` (Authorization Code +
PKCE). L'API HTTP espone i profili **write-only**: i segreti non vengono
mai riletti.

**Flusso OAuth/SSO automatico** — per wiki protette da Azure AD, Google,
Okta, Auth0, Atlassian o SSO a form:

1. `wikibridge auth detect <url> --save intranet` sonda l'URL, riconosce
   l'IdP (dal redirect, dal WWW-Authenticate o dal form di login) e crea
   il profilo con endpoint e scope precompilati (per OAuth serve il
   `client_id` di una app registrata dall'amministratore, redirect
   `http://127.0.0.1:8776/callback`).
2. `wikibridge auth login intranet` apre il browser: l'utente fa login
   (MFA incluso) e i token — o i cookie di sessione, catturati dal
   browser con browser-cookie3 — vengono salvati in `secrets/`.
3. Da qui in poi tutto è automatico: il profilo è associato all'host
   (`hosts` nel profilo), quindi `import`/`sync`/`refresh` lo selezionano
   da soli senza `--auth-profile`; l'access token è rinnovato in silenzio
   col refresh token (rotazione inclusa) e, se anche il refresh scade,
   riparte il login nel browser (disattivabile con
   `auth_interactive: false` per ambienti headless).

Durante gli import la sessione riconosce i **muri di login** (401,
redirect all'IdP, form di login al posto del contenuto): niente pagine
"access denied" nella wiki — il job fa re-login e riprova, oppure si
ferma in stato `auth_required` con la frontiera salvata per riprendere
dopo `auth login`.

## Catalogazione LLM (tre modalità)

| brain_mode | chi è l'LLM | quando |
|---|---|---|
| `openai_http` | LM Studio (endpoint OpenAI-compatibile) | GUI/CLI |
| `agent` | l'agente ospite (Hermes/Claude) drena `_pending/` con `wikibridgectl catalog-apply` | skill |
| `none` | nessuno: solo import + FTS | degradazione |

La catalogazione estrae **entità**, **concetti** e **relazioni**
(map-reduce a chunk), li fonde per nome e li scrive come pagine wiki con
sezione "Menzioni"; le relazioni alimentano il grafo.

## API HTTP (:8775)

`/health`, `/projects` (+`/activate`), `/imports` (start/list/get/
cancel/sync), `/ingest` (+`/dir`), `/meetings` (push riunioni),
`/wiki/tree|page|refresh`, `/search`, `/graph`, `/catalog/drain`,
`/skill/status|install`, `/auth/profiles`, `/auth/detect`,
`/auth/login` (+`/status`: login interattivo, browser sulla macchina del
server). Bearer opzionale (`api_auth_token` in config.json).

## Integrazione callAPIcall

- Sezione **Conoscenza** nel Control Center: progetti, import guidato,
  documenti, browser wiki con "Aggiorna", ricerca, skill e profili.
- **Riunioni**: alla join si sceglie il progetto → il bot analizza con la
  conoscenza del progetto e a fine riunione la pagina
  `meetings/<data>-<titolo>.md` (Contenuto/Accordi/Attività) finisce nel
  wiki; push manuale dal dettaglio storico.
- **Copilota/avatar**: selettore progetto + "Salva riunione nel wiki".
- Il preamble del cervello riceve il puntatore alla wiki del progetto
  (path `/mnt/c/...` per Hermes in WSL) SOLO per il progetto collegato.

## Skill per gli agenti

`skill/SKILL.md` (trigger `/wiki-import`) istruisce l'agente su comandi,
regole di isolamento per progetto e drenaggio della catalogazione.
`scripts/ensure_llmwiki.py` verifica/installa la skill **llm-wiki**
(lewislulu/llm-wiki-skill) su Hermes, OpenClaw e Claude Code.

## Limiti noti

- Il crawler generico non esegue JavaScript (wiki SPA non supportate in
  v1); MediaWiki/Confluence/Notion usano le API e non ne risentono.
- Un job di import/ingest alla volta (409 se occupato).
- Macro Confluence complesse possono perdere struttura nella conversione.
- Notion richiede un'integrazione condivisa con le pagine da importare.
