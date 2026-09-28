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
wikibridge sync <import-id>        # = update --import <import-id>
wikibridge update                  # controlla TUTTE le sorgenti del progetto
                  [--import ID] [--ingest ID] [--path FILE|DIR]  # (ripetibili)
                  [--all-projects] [--no-discover] [--dry-run] [--no-catalog]
wikibridge sources                 # sorgenti aggiornabili (rami wiki, cartelle, file)
wikibridge acl show|page|collection|gate|enforce   # autorizzazioni (vedi sotto)
wikibridge catalog-rebuild         # ricataloga tutto per segmenti (costo LLM)
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

## Aggiornamenti (on-demand)

Nessun watcher o scheduler interno: il controllo parte solo quando viene
richiamato — `wikibridge update`, `POST /update`, un agente o un cron
(es. Utilità di pianificazione: `wikibridge update --all-projects`, exit
code 1 se una sorgente ha errori).

- **Sorgenti**: ogni import è un *ramo wiki*; ogni `ingest` registra la
  cartella (*ramo di file*) o il file (*lista di singoli file*) in
  `ingests.json`. `--path` accetta file e cartelle: quelli nuovi vengono
  registrati, quelli già tracciati controllati nella loro sorgente.
- **Rami wiki**: pagine cambiate (revisione della sorgente), pagine nuove
  (dall'elenco del ramo per Confluence/MediaWiki `subtree`/`space`, dai
  link delle pagine cambiate per gli altri), pagine sparite.
- **File**: cambi rilevati da dimensione/mtime e confermati dallo sha256
  (niente riconversione se il contenuto è identico); rinomine e
  spostamenti riconosciuti dall'id file NTFS o dal contenuto.
- **Niente duplicati**: ogni pagina ha un `doc_id` stabile (id nativo
  della wiki, o del file) e l'aggiornamento riscrive sempre la stessa
  pagina, anche se il titolo o il nome del file cambiano. Nelle pagine
  entità/concetto ogni riassunto è un segmento legato alla source: un
  aggiornamento lo sostituisce, una rimozione lo toglie.
- **Rimozioni prudenti**: dopo 2 controlli consecutivi con la sorgente
  assente; mai se la cartella radice non è raggiungibile o se sparisce
  in un colpo più di metà del ramo.
- **Versioni (predisposte)**: ogni source registra `doc_id`,
  `native_id`, `revision` della sorgente, `content_sha256`,
  `content_version` (+1 solo se cambia il contenuto), `pipeline_version`,
  `first_seen`; l'indice di ricerca riporta `doc_id` e `version` per ogni
  risultato. Lo storico delle versioni non è ancora conservato.

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
cancel/sync), `/ingest` (+`/dir`), `/update` (controllo aggiornamenti,
body `{imports?, ingests?, paths?, discover?, dry_run?}`), `/sources`,
`/acl` (+ `PUT /acl/page`, `PUT|DELETE /acl/collections/<id>`,
`PUT /acl/gate`; body `{principals?, users?, classification?, sources?}`),
`/meetings` (push riunioni),
`/wiki/tree|page|refresh`, `/search`, `/graph`, `/catalog/drain`,
`/skill/status|install`, `/auth/profiles`, `/auth/detect`,
`/auth/login` (+`/status`: login interattivo, browser sulla macchina del
server). Bearer opzionale (`api_auth_token` in config.json).

### Accesso multi-utente (LDAP / Active Directory)

Con `identity_provider: "ldap"` il server smette di essere single-user:

- **Login una volta**: `POST /identity/login {username, password}` fa il
  bind LDAP e restituisce un token di sessione (scade dopo
  `session_idle_min` di inattività e comunque dopo `session_max_hours`);
  le richieste successive usano `Authorization: Bearer <token>` e non
  toccano più LDAP. `POST /identity/logout`, `GET /identity/whoami`,
  `POST /identity/refresh` (ricarica subito i ruoli dopo un nuovo permesso).
- **Ruoli**: gruppi LDAP (`memberOf`) → ruoli ACL via `identity_role_map`
  (DN confrontati senza badare a maiuscole/spazi), in cache per
  `entitlement_cache_ttl_s`. Il ruolo `admin` è richiesto per tutte le
  route di gestione (progetti, import, ingest, update, sources, profili
  auth, skill, catalogazione, riunioni): gli altri utenti possono solo
  leggere e cercare.
- **Chiave applicazione separata**: se `api_auth_token` è impostato, il
  client la manda in `X-Wikibridge-Key` (non più in `Authorization`, che
  porta la sessione dell'utente).
- **Protezioni**: stesso messaggio per ogni login fallito (niente
  enumerazione degli account), limite di tentativi per utente e per IP
  (`login_max_failures_user` va tenuto sotto la soglia di blocco di AD),
  `ldap://` rifiutato senza `ldap_start_tls` (`ldaps://` consigliato,
  `ldap_ca_file` per la CA), avvio rifiutato se `api_host` non è loopback
  senza `api_tls_cert`/`api_tls_key` o `behind_tls_proxy`, CORS limitato a
  `cors_origins`, `/health` anonimo senza dettagli.

### Autorizzazioni sui contenuti (ACL)

Attive per progetto con `wikibridge acl enforce on` (default globale
`acl_enforcement`) e solo con un identity provider configurato. Le ACL
sono **per riferimento**: un blocco porta un `acl_id` condiviso da tutti i
blocchi con le stesse autorizzazioni, e le clausole di livello superiore
stanno in `acl.json` del progetto, lette a ogni richiesta.

| Livello | Comando | Effetto di una modifica |
|---|---|---|
| Gate di progetto | `acl gate --role R --user U` | chi non lo soddisfa non vede nemmeno il progetto (404) |
| Collezione (ramo: import, cartelle, liste di file) | `acl collection ID --role R --user U --classification C --source imp-0001` | scrittura su `acl.json`, **nessuna reindicizzazione**, effetto immediato |
| Documento | `acl page sources/x.md --role R --user U --classification C` | una riga dell'indice, testo e versione invariati, effetto immediato |

- **Intersezione**: un documento può solo restringere la sua collezione
  (servono entrambe le clausole); senza alcuna clausola è visibile solo
  se `public` (default-deny), all'admin sempre.
- **Persona singola**: `--user mario` diventa il principal
  `user:<objectGUID>` (stabile anche se l'account viene rinominato); un
  permesso concesso dopo l'ingestione vale dalla richiesta successiva.
- **Classificazione**: `public < internal < confidential < restricted`;
  un utente vede fino al livello `default_clearance`, o superiore se ha un
  gruppo mappato a `clearance:<livello>` in `identity_role_map`.
- **Cosa vede chi non è admin**: ricerca, albero, pagine e grafo filtrati;
  le pagine entità/concetto mostrano solo i segmenti derivati da source
  visibili; `index.md` è generato per l'utente; `overview.md`, `log.md`,
  riunioni, sintesi e testo legacy non attribuibile sono solo per l'admin
  (`wikibridge catalog-rebuild` ricataloga tutto per segmenti e rimuove il
  testo legacy, al costo di una chiamata LLM per source).
- **Limite**: le ACL valgono per chi passa dall'API. Un agente o un
  processo che legge `data/` direttamente dal disco le scavalca: in
  multi-utente `data/` va resa leggibile solo all'account del servizio.

```json
{
  "identity_provider": "ldap",
  "ldap_url": "ldaps://dc.azienda.local",
  "ldap_base_dn": "dc=azienda,dc=local",
  "ldap_bind_dn": "cn=svc-wikibridge,ou=service,dc=azienda,dc=local",
  "ldap_bind_password": "env:WIKIBRIDGE_LDAP_PW",
  "identity_role_map": {
    "CN=Legal,OU=Groups,DC=azienda,DC=local": "team-legal",
    "CN=Wikibridge-Admins,OU=Groups,DC=azienda,DC=local": "admin"
  }
}
```

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
