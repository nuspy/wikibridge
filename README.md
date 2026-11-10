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
                  [--permissions]    # permessi di lettura da Confluence
wikibridge sync <import-id>        # = update --import <import-id>
wikibridge update                  # controlla TUTTE le sorgenti del progetto
                  [--import ID] [--ingest ID] [--path FILE|DIR]  # (ripetibili)
                  [--all-projects] [--no-discover] [--dry-run] [--no-catalog]
                  [--permissions]    # i --path nuovi leggono i permessi NTFS
wikibridge sources                 # sorgenti aggiornabili (rami wiki, cartelle, file)
wikibridge acl show|page|collection|gate|enforce   # autorizzazioni (vedi sotto)
wikibridge catalog-rebuild         # ricataloga tutto per segmenti (costo LLM)
wikibridge audit [--path P] [--user U] [--action A] [--since D] [--verify]
wikibridge synthesis --title T --source sources/a.md [--source ...] --file testo.md
wikibridge refresh <wiki-path>     # refresh puntuale di una pagina
wikibridge ingest <file|dir> [--recursive] [--permissions]
wikibridge history <wiki-path> [--at DATA]   # versioni di un documento
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
- **Change feed**: Confluence (ricerca CQL) e MediaWiki (RecentChanges)
  dicono in una chiamata cosa è cambiato dall'ultimo controllo, e solo
  quelle pagine vengono ricontrollate; Confluence elenca anche il ramo con
  una ricerca (~1 chiamata ogni 100 pagine). Una pagina assente dal
  risultato della ricerca (indice in ritardo) viene verificata direttamente
  prima di essere considerata sparita. Senza feed si controlla pagina per
  pagina come prima.
- **Processi concorrenti**: server, CLI, cron e agenti (anche da WSL)
  possono lavorare insieme: i file condivisi si scrivono sotto lock tra
  processi, e due aggiornamenti della stessa sorgente non partono insieme
  (il secondo riporta `state: "busy"`).
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
- **Chunk**: ogni documento (source, riunione, sintesi) è indicizzato a
  pezzi, uno o più per sezione (i titoli markdown delimitano le sezioni),
  con chiavi stabili: modificare una sezione cambia solo i suoi chunk. Un
  aggiornamento riscrive nell'indice solo i chunk cambiati; ricerca e
  contesto RAG restituiscono la sezione pertinente (`heading`, `chunk`),
  non l'inizio della pagina.
- **Versioni (predisposte)**: ogni source registra `doc_id`,
  `native_id`, `revision` della sorgente, `content_sha256`,
  `content_version` (+1 solo se cambia il contenuto), `pipeline_version`,
  `first_seen`; ogni chunk ha la propria versione (+1 solo quando cambia il
  suo testo) con la versione del documento in cui è cambiato e la data,
  in un registro che sopravvive alle ricostruzioni dell'indice. Ricerca e
  RAG riportano `doc_id`, `version` e `chunk_version`. Lo storico delle
  versioni (il testo delle versioni precedenti) non è ancora conservato.

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
`/rag/context` (contesto per l'inferenza, filtrato),
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
- **Gruppi annidati**: `ldap_nested_groups: "memberof"` (default) risale
  `memberOf` dei gruppi (AD e OpenLDAP, una lettura per gruppo, in cache e
  a prova di cicli, fino a `ldap_nested_depth`); `"in_chain"` usa la regola
  `LDAP_MATCHING_RULE_IN_CHAIN` di AD (una query, lenta su AD grandi);
  `"off"` solo appartenenza diretta.
- **Revoche e nuovi permessi prima del TTL**: `POST /identity/sync-groups`
  (admin, anche da cron) confronta `ldap_change_attr` (AD: `uSNChanged`,
  sempre sullo stesso DC; OpenLDAP: `modifyTimestamp`) degli oggetti
  `ldap_watch_filter` (gruppi e utenti) e, se qualcosa è cambiato,
  invalida i ruoli in cache. Con `ldap_change_poll_s` > 0 lo fa il server
  da solo (default 0: solo su richiesta).
- **Account disabilitati o rimossi**: alla nuova risoluzione dei ruoli
  (TTL o modifica rilevata) le loro sessioni vengono revocate. Un
  directory irraggiungibile risponde invece 503 senza disconnettere
  nessuno.
- **Amministrazione**: `POST /identity/invalidate {username?,
  revoke_sessions?}` (un utente, anche chiudendone le sessioni, o tutti),
  `GET /identity/status` (sessioni, utenti in cache, ultimo controllo,
  voci di `identity_role_map` che non corrispondono a un gruppo — segnalate
  anche nei log all'avvio).
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
| Documento | `acl page sources/x.md --role R --user U --classification C [--yes]` | una riga dell'indice, testo e versione invariati, effetto immediato |
| Filtro del progetto | `acl enforce on\|off [--yes]` | spento = nessun filtro nel progetto (serve `--yes`) |

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
- **Permessi letti alla fonte (Confluence)**: `wikibridge import <url>
  --permissions` (o `"permissions": true` in `POST /imports`) legge le
  restrizioni di lettura della pagina e di tutti i suoi antenati — in
  Confluence si ereditano e si sommano — e le applica come clausole in AND
  (`acl_source_clauses`, in aggiunta ai tag manuali). Gruppi Confluence →
  ruoli con `source_group_map` (o `source_group_passthrough: true` se i
  nomi coincidono con i ruoli); utenti → cercati nel directory per email o
  username, oppure `source_user_map`. **Fail-closed**: permessi non
  leggibili, gruppi o utenti non traducibili rendono la pagina visibile
  solo all'admin, e il report dell'import/update elenca cosa manca
  (`acl_unreadable`, `acl_unmapped`). `wikibridge update` rilegge i
  permessi anche delle pagine il cui testo non è cambiato (una restrizione
  in Confluence non cambia la versione) e aggiorna solo l'indice. I
  permessi di **spazio** non vengono letti (su Cloud servono diritti di
  amministratore): si modellano con una collezione sull'import.
- **Permessi dei file (NTFS, Windows)**: `wikibridge ingest <cartella>
  --recursive --permissions` (o `update --path ... --permissions`, o
  `"permissions": true` in `POST /ingest/dir` / `POST /update`) legge la DACL
  effettiva di ogni file (ereditarietà già inclusa) e ne ricava chi può
  leggerlo: gruppi `DOMINIO\Gruppo` → ruoli con `source_group_map`, utenti
  cercati nel directory. SYSTEM e Administrators sono ignorati; "Everyone",
  "Authenticated Users" e "Users" vanno mappati esplicitamente. Le voci
  "nega" non sono rappresentabili: il file diventa solo admin
  (`acl_unreadable`). Serve `pywin32`; senza, fail-closed. Il flag resta
  sulla sorgente e ogni `update` riallinea i permessi cambiati.
- **Controllo di `data/`**: in modalità multi-utente il server avvisa
  all'avvio (log e `GET /identity/status` → `deployment_warnings`) se la
  cartella dei dati è leggibile da tutti: chi legge il disco scavalca le ACL.
- **Sintesi**: `POST /syntheses {title, body, sources}` o `wikibridge
  synthesis --title T --source sources/a.md --file testo.md` salvano una
  risposta come articolo dichiarando le fonti da cui deriva; con le ACL
  attive la vede solo chi vede **tutte** quelle fonti, valutate a ogni
  richiesta (se una fonte si restringe o sparisce, la sintesi segue). Chi
  la salva via API deve poter vedere tutte le fonti dichiarate; una sintesi
  non può derivare da un'altra. Le sintesi scritte a mano senza fonti
  restano visibili solo all'admin.
- **Riunioni**: una pagina riunione è visibile a chi l'ha salvata e ai
  partecipanti riconosciuti dal directory (username o email), più l'admin;
  qualunque utente autenticato che vede il progetto può salvarne una.
- **Limite**: le ACL valgono per chi passa dall'API. Un agente o un
  processo che legge `data/` direttamente dal disco le scavalca: in
  multi-utente `data/` va resa leggibile solo all'account del servizio.

### Registro di audit

Ogni evento rilevante finisce in `data/audit/audit-AAAA-MM.jsonl` (una riga
JSON per evento, conservazione `audit_retention_months`, default 12):
login riusciti e falliti, logout, revoche di sessione, invalidazioni,
ricerche, contesti RAG e pagine aperte **con i documenti restituiti**
(pagine negate registrate come `denied`), modifiche alle ACL con
l'anteprima dell'effetto, e ogni altra richiesta che modifica qualcosa
(metodo, percorso, esito; mai il corpo). La query è salvata come impronta
HMAC (`audit_query_mode`: `hash`, `full` o `none`), le password mai.

- `GET /audit?since=&user=&action=&path=` (admin) o `wikibridge audit
  --path sources/x.md`: per esempio "chi ha visto questo documento?".
- `GET /audit/verify` o `wikibridge audit --verify`: ogni riga contiene
  l'hash della precedente, quindi una riga modificata o cancellata si
  rileva.
- **Conferma sulle modifiche che allargano l'accesso**: creare o allargare
  una collezione, abbassarne la classificazione, toglierle sorgenti,
  eliminarla, togliere o allargare il gate, aggiungere principal a un
  documento o togliergli tutti i suoi (decide allora la collezione),
  abbassarne la classificazione, e **spegnere il filtro** di un progetto
  (`acl enforce off`: tutto diventa visibile a chi entra nel progetto)
  rispondono `409` con l'anteprima (principal aggiunti e tolti, con il nome
  leggibile delle persone; documenti coinvolti) finché non si ripete con
  `"confirm": true` (CLI: `--yes`); `"dry_run": true` mostra solo
  l'anteprima. Restringere non chiede conferma. Anche `acl enforce` finisce
  nel registro di audit.

### Gestione delle ACL dalla GUI

Nella sezione Conoscenza di callAPIcall, un admin di un wikibridge
multi-utente trova la scheda **Permessi**: filtro del progetto (acceso o
spento), chi può aprire il progetto (gate), collezioni (sorgenti, ruoli e
persone, classificazione). Nella vista Wiki, aprendo un documento, il
riquadro **Permessi del documento** mostra la collezione e i permessi letti
dalla fonte (in sola lettura) e permette di modificare le regole proprie
del documento. Ogni modifica che allarga l'accesso mostra prima
l'anteprima e chiede conferma, come l'API. `GET /acl` fornisce alla GUI i
ruoli noti (da `identity_role_map` e `source_group_map`, più quelli già
usati) e i nomi leggibili delle persone (`labels` in `acl.json`, salvati
quando le si indica per username: servono solo a mostrarle, non entrano
mai nelle decisioni).

### Conoscenza per l'inferenza (RAG filtrato)

Chi fa inferenza non deve leggere la wiki dal disco ma chiedere il
contesto a `POST /rag/context` `{query, participants?, unresolved?,
limit?, max_chars?}`: restituisce il testo completo dei blocchi
pertinenti che l'utente della sessione può vedere, con fonte, `doc_id` e
versione (per i testi lunghi, come le trascrizioni, la ricerca usa i
termini più significativi in OR).

- **Riunioni**: con `participants` il contesto è l'**intersezione** di
  ciò che vedono il richiedente e i partecipanti riconosciuti.
  `unresolved: "public"` (default) — per ciò che viene detto a tutti i
  presenti — riduce ai soli contenuti `public` se qualcuno non è
  riconosciuto; `"ignore"` — per ciò che vedranno solo i riconosciuti,
  come la pagina della riunione — non lo fa contare.
- **callAPIcall** usa questo endpoint quando wikibridge è multi-utente
  (`knowledge_access: "auto"`): copilota a ogni enunciato con i permessi
  dell'operatore, analisi delle riunioni con l'intersezione dei
  partecipanti; se wikibridge non risponde il cervello resta senza
  conoscenza, senza ripiegare sul disco. Utente di inferenza in
  `knowledge_username`, password in `CALLBRIDGE_KNOWLEDGE_PASSWORD`.
- `GET /health` riporta `multi_user`, così i client scelgono da soli.

### SSO OIDC (Entra ID/Azure AD, Okta, Keycloak, Google)

Con `identity_provider: "oidc"` il login avviene nel browser (Authorization
Code + PKCE, state e nonce): il client apre
`GET /identity/oidc/login?return_to=<url>`, l'utente fa login presso l'IdP
(MFA compreso) e la sessione wikibridge viene consegnata **solo** a un
`return_to` ammesso (loopback, o prefissi in `oidc_return_urls`; su
loopback nella query, altrove nel fragment). Senza `return_to` la pagina
finale mostra il token. Chi ha già un `id_token` per lo stesso client lo
scambia con `POST /identity/oidc/token`.

- **Validazione**: firma via JWKS con soli algoritmi asimmetrici (un token
  HS256 firmato con la chiave pubblica viene rifiutato), `iss`, `aud`,
  `exp`, nonce; issuer `https://` obbligatorio.
- **Ruoli**: dal claim `oidc_groups_claim` via `identity_role_map`
  (confronto senza maiuscole). Entra ID: `groups` porta gli id dei gruppi
  e con troppi gruppi li omette ("overage") — meglio gli app roles
  (`oidc_groups_claim: "roles"`); `oidc_user_id_claim: "oid"` come id
  stabile della persona.
- **Revoche**: il refresh token resta sul server; a ogni scadenza del TTL
  (o con `/identity/refresh`, `/identity/invalidate`) i gruppi vengono
  riletti dall'IdP, e se l'IdP rifiuta il refresh (utente disabilitato,
  sessione revocata) la sessione wikibridge viene chiusa.
- **Persone per nome** (permessi personali, partecipanti alle riunioni):
  riconosciute dopo il loro primo login (registro `identities.json`; email
  non verificate escluse, nessuna voce passa a un'altra identità), oppure
  sempre se `oidc_user_id_claim` è `email`/`preferred_username`/`upn`.
- **callAPIcall** lo gestisce da solo: se `/health` riporta
  `"login": "sso"` apre il browser al primo bisogno (sul percorso indicato
  in `sso_login`, OIDC o SAML) e riceve il token su un mini-server loopback.

```json
{
  "identity_provider": "oidc",
  "oidc_issuer": "https://login.microsoftonline.com/<tenant-id>/v2.0",
  "oidc_client_id": "<application-id>",
  "oidc_groups_claim": "roles",
  "oidc_user_id_claim": "oid",
  "identity_role_map": {"Legal.Reader": "team-legal", "Wikibridge.Admin": "admin"}
}
```

### SSO SAML 2.0 (AD FS, Entra ID, Okta, Shibboleth)

Con `identity_provider: "saml"` (richiede `pip install "wikibridge[saml]"`,
cioè `python3-saml` + `xmlsec`) il login parte da `GET
/identity/saml/login?return_to=<url>`; l'IdP rimanda il browser con un POST
su `/identity/saml/acs`, e il token arriva al `return_to` come per OIDC
(prefissi ammessi in `saml_return_urls`). I metadati da registrare nell'IdP
sono su `GET /identity/saml/metadata`; quelli dell'IdP si danno con
`saml_idp_metadata` (URL https o file) oppure con `saml_idp_entity_id`,
`saml_idp_sso_url` e `saml_idp_cert`.

- **Verifiche**: firma dell'asserzione obbligatoria (SHA-1 rifiutato),
  destinatario, audience, scadenze, e `InResponseTo` legato a una richiesta
  monouso partita da wikibridge: niente replay e niente login avviati
  dall'IdP.
- **Ruoli** (`saml_roles_source`): `"ldap"` usa SAML solo per il login e
  legge gruppi e stato dell'account dal directory (config `ldap_*`,
  revoche in tempo reale, stesso principal del login LDAP); `"assertion"`
  prende i gruppi da `saml_groups_attr` via `identity_role_map`, ma restano
  fissi fino alla scadenza della sessione (SAML non ha refresh).
- **Non supportati**: asserzioni cifrate, richieste firmate, Single Logout.
  Con NameID "transient" serve `saml_user_id_attr`.

```json
{
  "identity_provider": "saml",
  "saml_idp_metadata": "https://adfs.azienda.it/FederationMetadata/2007-06/FederationMetadata.xml",
  "saml_acs_url": "https://wikibridge.azienda.it/identity/saml/acs",
  "saml_roles_source": "ldap",
  "saml_username_attr": "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/upn",
  "ldap_url": "ldaps://dc.azienda.local",
  "ldap_base_dn": "dc=azienda,dc=local",
  "ldap_mail_filter": "(|(mail={username})(userPrincipalName={username}))"
}
```

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
