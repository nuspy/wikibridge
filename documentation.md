# wikibridge e callAPIcall — cosa manca, perché, e i problemi aperti

Questo documento raccoglie, in un unico posto, tutto ciò che nel lavoro
sulle autorizzazioni aziendali (LDAP/AD, OIDC, ACL sulla conoscenza,
aggiornamenti, integrazione con callAPIcall) **non è stato fatto** o **non è
risolto**. Per ogni punto spiega che cosa manca, perché non è stato fatto,
che conseguenze pratiche ha oggi, e che cosa servirebbe per chiuderlo.

Serve a chi deve decidere se e come portare il sistema in produzione, e a
chi riprenderà il lavoro: la parte "fatta" è documentata nel `README.md`;
qui c'è la parte "non fatta" e i rischi che restano.

Ultimo aggiornamento: 29 settembre 2026.

---

## 1. Il quadro in breve

Cosa c'è oggi (dettagli nel README):

- **Identità degli utenti**: login LDAP/Active Directory (username e
  password, una volta per sessione), SSO OIDC (Entra ID/Azure AD, Okta,
  Keycloak, Google) oppure SSO SAML 2.0 (AD FS, Entra ID, Okta,
  Shibboleth). Sessioni revocabili, cache dei ruoli con scadenza, gruppi
  annidati, rilevamento delle modifiche nel directory, revoca delle
  sessioni per account disabilitati.
- **Autorizzazioni sulla conoscenza (ACL)**: ogni blocco di conoscenza ha
  un riferimento a una "chiave di autorizzazione" condivisa; collezioni di
  sorgenti e gate di progetto; permessi a persona singola; livelli di
  classificazione. Il filtro vale per ricerca, pagine, albero, grafo,
  progetti e per il contesto passato all'LLM (`/rag/context`).
- **Permessi letti dalla fonte**: per Confluence (`import --permissions`)
  le restrizioni di lettura delle pagine e degli antenati, e per i file su
  Windows (`ingest --permissions`) i permessi NTFS, vengono letti, tradotti
  e tenuti aggiornati.
- **Aggiornamenti**: controllo su richiesta di rami wiki, cartelle e file,
  senza duplicati, con i campi di versione predisposti.
- **Registro di audit**: chi ha fatto login, chi ha visto quali documenti,
  chi ha cambiato le ACL; catena di hash contro le manomissioni; conferma
  obbligatoria sulle modifiche che allargano l'accesso.
- **Sintesi** visibili solo a chi vede tutte le loro fonti.
- **Chunk**: documenti indicizzati per sezione, aggiornati chunk per chunk,
  con versione per chunk; il contesto RAG usa le sezioni pertinenti.
- **Change feed** per Confluence e MediaWiki; **lock tra processi** per
  server, CLI e agenti che lavorano insieme.
- **callAPIcall**: in modalità multi-utente il "cervello" riceve estratti
  già filtrati invece del percorso della wiki su disco; le riunioni usano
  l'intersezione dei permessi dei partecipanti.

Lo stato dei repository al momento della scrittura:

- **wikibridge**: due commit fatti (il tuo lavoro precedente sull'OAuth, e
  il primo blocco del mio lavoro); tutto quello che è venuto dopo (contesto
  RAG filtrato, ACL sulle riunioni, Fase C su LDAP, OIDC, SAML, permessi da
  Confluence e NTFS, registro di audit, lock tra processi, change feed,
  sintesi, chunk e storico, manuale, questo documento) è **nel working
  tree, non committato**.
- **callAPIcall**: le mie modifiche sono **non committate** e stanno in
  file precisi: nel backend `knowledge_client.py`, `copilot.py`,
  `meetbot/summary.py`, il nuovo `api_knowledge.py`, due righe di
  registrazione in `control_server.py` e un blocco della sezione conoscenza
  in `config.py`; nella GUI `KnowledgeSection.tsx`, i nuovi
  `KnowledgeSession.tsx` e `KnowledgeAccess.tsx`, `knowledgeApi.ts` e i
  blocchi `knowledge.session` e `knowledge.access` nei 15 file di lingua. Convivono con molte tue
  modifiche non committate in altri file (telefono, Android, audio); in
  `config.py` le mie righe sono in un blocco separato dalle tue, quindi i
  due lavori restano separabili.

---

## 2. Cose non fatte

### 2.1 SAML

**Stato: fatto** (`identity_provider = "saml"`, `wikibridge/saml_identity.py`),
con i limiti elencati sotto.

**Come funziona.** Il login parte da wikibridge (`GET
/identity/saml/login`), l'IdP autentica l'utente e rimanda il browser con
un POST su `/identity/saml/acs`. La verifica della risposta usa
`python3-saml` (OneLogin), la libreria SAML più usata in Python, che si
appoggia a `xmlsec` per le firme. Il timore che mi aveva fermato era
l'installazione di `xmlsec` su Windows: in realtà oggi esistono i pacchetti
binari ufficiali per Windows e Linux (provato: `xmlsec 1.3.17` su Python
3.10, Windows 11, si installa senza compilare nulla). Per questo è un extra
opzionale (`pip install "wikibridge[saml]"`) e non una dipendenza fissa.

**Cosa viene verificato su ogni risposta**, con un test per ciascun
attacco (un IdP finto firma davvero le asserzioni): firma dell'asserzione
con il certificato configurato (una risposta non firmata, firmata con
un'altra chiave o modificata dopo la firma è rifiutata); destinatario e
audience (una risposta emessa per un'altra applicazione non vale qui);
scadenze; `InResponseTo`, cioè la risposta deve corrispondere a una
richiesta partita da wikibridge, e ogni richiesta vale una volta sola
(niente replay, niente risposte non richieste). Le firme SHA-1 sono
rifiutate.

**Da dove vengono i ruoli** (`saml_roles_source`) — è la scelta importante:

- `"ldap"` (consigliato con AD FS o quando c'è un Active Directory
  raggiungibile): SAML serve solo a sapere *chi* è l'utente; gruppi,
  account disabilitati e modifiche vengono letti dal directory esattamente
  come nel login LDAP. Le revoche sono quindi in tempo reale (rilevamento
  delle modifiche, TTL della cache), e la persona ha lo stesso principal
  (`user:<objectGUID>`) che avrebbe con il login LDAP: i permessi personali
  già assegnati restano validi.
- `"assertion"`: i gruppi arrivano nell'attributo `saml_groups_attr`
  dell'asserzione. Il limite è strutturale: **SAML non ha un meccanismo di
  refresh** (a differenza di OIDC, dove il refresh token permette di
  rileggere i gruppi). I gruppi restano quelli del momento del login fino
  alla scadenza della sessione wikibridge (`session_max_hours`, 8 ore di
  default), e un account disabilitato nell'IdP resta dentro fino ad
  allora. Se serve una revoca più rapida: abbassare `session_max_hours`,
  revocare a mano la sessione, o usare `"ldap"`.

**Cosa non è supportato, e perché:**

- **Asserzioni cifrate.** Richiedono una chiave privata di wikibridge
  registrata nell'IdP e la sua gestione (rotazione, protezione). Con HTTPS
  l'asserzione è già protetta in transito; se l'IdP la cifra
  obbligatoriamente, il login fallisce con un errore esplicito.
- **Richieste firmate** (AuthnRequest firmate) e **Single Logout**: stessa
  ragione (serve una chiave di wikibridge). Il logout chiude la sessione
  wikibridge, non quella dell'IdP.
- **Login avviato dall'IdP** (l'utente clicca l'app dal portale dell'IdP):
  rifiutato di proposito, perché senza una richiesta di partenza non c'è
  modo di distinguere una risposta legittima da una rubata e riproposta,
  e non si saprebbe a quale applicazione consegnare il token.
- **NameID "transient"** senza un attributo stabile: il NameID transient
  cambia a ogni login, quindi la stessa persona risulterebbe ogni volta
  diversa. In quel caso va impostato `saml_user_id_attr` (per Entra ID:
  `http://schemas.microsoft.com/identity/claims/objectidentifier`), altrimenti
  il login viene rifiutato.
- **Utenti non ancora visti** (`"assertion"`): come per OIDC, una persona
  si può indicare per nome (permessi personali, partecipanti) solo dopo il
  suo primo login.

**Conseguenze oggi.** Non provato contro un IdP reale (AD FS, Entra ID,
Okta): i test usano un IdP simulato che produce risposte firmate nel formato
standard. Nomi degli attributi e formato del NameID variano da IdP a IdP e
vanno verificati sul posto (l'endpoint `/identity/saml/metadata` fornisce i
metadati da registrare nell'IdP). Con `"ldap"` e NameID in forma di UPN
(`mario@azienda.it`) serve un `ldap_mail_filter` che cerchi anche
`userPrincipalName`, per esempio
`(|(mail={username})(userPrincipalName={username}))`.

### 2.2 Lettura automatica dei permessi dalle sorgenti

**Stato: fatta per Confluence e per i file locali (NTFS); non per
SharePoint, MediaWiki, Notion e HTML.**

Per **Confluence** (`import --permissions`) i permessi di lettura delle
pagine vengono letti e applicati: restrizioni della pagina e di tutti gli
antenati, tradotte in ruoli e persone di wikibridge, aggiornate a ogni
`update` anche quando il testo non cambia, e negate (visibili solo
all'admin) quando non si possono leggere o tradurre. Restano questi limiti:

- **Permessi di spazio non letti.** Su Confluence Cloud l'API dei permessi
  di spazio richiede diritti di amministratore, che l'account di import di
  solito non ha (e non dovrebbe avere). Chi può vedere lo spazio va quindi
  dichiarato in wikibridge con una collezione sull'import. Finché non la
  si crea, le pagine **senza** restrizioni proprie sono visibili solo
  all'admin (default-deny): è voluto, perché "nessuna restrizione sulla
  pagina" in Confluence vuol dire "tutti quelli che vedono lo spazio", e
  wikibridge non sa chi siano.
- **Utenti di Confluence Cloud.** Cloud identifica le persone con un
  `accountId` opaco e di solito nasconde l'email: senza email wikibridge
  non riesce a trovarle nel directory aziendale, e le deve ricevere da
  `source_user_map` (accountId → persona). Una restrizione a persone non
  mappate diventa "nessuno" (solo admin). Su Confluence Server/Data Center
  gli utenti hanno lo username di AD e vengono trovati da soli.
- **Nomi dei gruppi.** I gruppi Confluence vanno collegati ai ruoli con
  `source_group_map`; se in azienda i gruppi Confluence sono i gruppi AD
  con lo stesso nome dei ruoli, basta `source_group_passthrough`. Un gruppo
  non mappato rende la pagina visibile solo all'admin e compare nel report
  (`acl_unmapped`).
- **Pagine incluse in altre pagine.** L'import legge il formato "storage"
  di Confluence, in cui una macro di inclusione è solo un riferimento: il
  contenuto incluso **non** viene copiato nella pagina (quindi non c'è il
  rischio che finisca con i permessi sbagliati), ma di conseguenza manca
  anche dalla conoscenza.
- **Costo.** Con i permessi attivi ogni controllo aggiornamenti fa almeno
  due chiamate in più per pagina (antenati e restrizioni; le restrizioni
  degli antenati condivisi sono lette una volta sola per esecuzione).
- **Non provato contro un Confluence reale**: solo contro un Confluence
  simulato che risponde come la REST API v1.

Per i **file locali e le cartelle di rete su Windows** (`ingest
--permissions`, `update --path ... --permissions`, oppure `"permissions":
true` nel corpo di `POST /ingest/dir` e `POST /update`) si legge la DACL di ogni
file (`wikibridge/ntfs_acl.py`). Il flag viene ricordato nel registro della
sorgente, quindi gli `update` successivi rileggono i permessi da soli.

- **Perché qui non si risale l'albero.** In NTFS i permessi ereditati sono
  già scritti sul file da Windows: la DACL del file è quella effettiva,
  comprese l'ereditarietà interrotta e le cartelle padre. Il problema che
  impediva di riusare il modello di Confluence (che fa l'intersezione con
  gli antenati) qui non si pone: basta leggere il file.
- **Chi conta.** Si considerano solo le voci "consenti" con un diritto di
  lettura (lettura dati, lettura generica, controllo completo). Le identità
  di sistema (SYSTEM, Administrators, CREATOR OWNER, i servizi) sono
  ignorate: in wikibridge l'admin vede comunque tutto. I gruppi "Everyone",
  "Authenticated Users" e "Users" non sono tradotti da soli: vanno mappati
  esplicitamente in `source_group_map` (per esempio `"Authenticated Users":
  "dipendenti"`). Altrimenti il file diventa visibile solo all'admin e il
  gruppo compare in `acl_unmapped`: è voluto, perché "tutti gli utenti del
  dominio" deve essere una scelta dichiarata, non un effetto collaterale.
- **Nomi.** I gruppi arrivano come `DOMINIO\Gruppo` e vanno mappati con
  `source_group_map` (o con `source_group_passthrough` se il nome coincide
  con il ruolo). Gli utenti arrivano come username e vengono cercati nel
  directory (LDAP/AD).
- **Voci "nega".** NTFS permette di negare esplicitamente la lettura a un
  gruppo o a una persona. Il modello di wikibridge (ruoli in OR dentro una
  clausola, clausole in AND) non sa esprimere "tutti quelli del gruppo A
  tranne B". Un file con una voce "nega" è quindi trattato come permessi
  non leggibili: solo admin, e contato in `acl_unreadable`. Si sbaglia per
  eccesso di chiusura, mai di apertura.
- **Senza pywin32, o fuori da Windows,** i permessi non si leggono e tutti i
  file della sorgente con `--permissions` diventano solo admin (non si
  ricade mai su "visibile a tutti"). `pywin32` è nei requisiti, solo per
  Windows. Sulle condivisioni Samba o su Linux le ACL POSIX non sono lette.
- **Condivisioni di rete.** Si legge la DACL del file (NTFS), non i
  permessi della condivisione SMB: se la condivisione è più restrittiva dei
  file, wikibridge può essere più largo della condivisione. Di solito le
  condivisioni sono "Everyone: lettura" e il controllo vero è su NTFS, ma va
  verificato caso per caso.
- **Costo.** Una lettura della DACL per file a ogni `update`, anche per i
  file non modificati (i permessi possono cambiare senza toccare il file).
  Sono chiamate locali, trascurabili rispetto alla conversione dei
  documenti. Sulle condivisioni di rete ogni lettura è una chiamata SMB.
- **Non provato con un dominio reale**: i test simulano le DACL; la
  lettura vera è stata provata solo sui file di questa macchina.

**Le altre sorgenti non sono fatte**, per questi motivi:

- **SharePoint / OneDrive**: un elemento può avere permessi propri che
  *sostituiscono* quelli del padre, i link di condivisione aggiungono
  accessi "a chiunque abbia il link", e serve la Microsoft Graph API con
  un'app registrata nel tenant (permessi `Sites.Read.All` concessi da un
  amministratore). Non c'è un adapter SharePoint: andrebbe scritto da zero,
  e senza un tenant di prova non si può verificare.
- **MediaWiki**: di norma non ha permessi di lettura per pagina (l'accesso
  è per l'intera wiki): si modella con una collezione.
- **Notion**: per quanto mi risulta, l'API pubblica non espone alle
  integrazioni con chi è condivisa una pagina; va verificato, ma
  probabilmente si può solo modellare con una collezione.
- **HTML generico**: non ha un concetto di permessi leggibile.

Il resto di questa sezione spiega perché la lettura dei permessi alla
fonte è delicata in generale (vale anche per le sorgenti future).

**Perché è delicata.** Ogni sorgente ha un modello di permessi diverso e
servono decisioni che non si possono prendere a tavolino:

- *Semantica*: in Confluence una pagina figlia può solo **restringere** i
  permessi del padre (intersezione); in SharePoint e nelle cartelle NTFS un
  elemento può avere permessi propri che **sostituiscono** quelli ereditati;
  in Google Drive una condivisione diretta si **aggiunge**. Trattarle tutte
  allo stesso modo produce o accessi negati per errore o, peggio, accessi
  concessi per errore.
- *Nomi*: i gruppi e gli utenti della sorgente non coincidono con quelli di
  AD (Confluence Cloud usa `accountId` opachi e gruppi propri), quindi
  serve una tabella di corrispondenza, e una corrispondenza sbagliata è un
  leak.
- *Permessi non leggibili*: l'account usato per importare potrebbe non
  avere il diritto di leggere le restrizioni; in quel caso bisogna negare
  (fail-closed), non ereditare qualcosa di più largo.

**Conseguenze oggi per le sorgenti non coperte.** Se un documento è
riservato alla fonte ma non viene etichettato a mano in wikibridge, con le
ACL attive è visibile **solo all'admin** (per il default-deny), non a chi
alla fonte ha diritto di vederlo. È sicuro ma scomodo: l'etichettatura è
manuale.

### 2.3 Registro di audit

**Stato: fatto** (vedi README, "Registro di audit"), con l'anteprima e la
conferma obbligatoria sulle modifiche che allargano l'accesso. Quello che
resta aperto:

- **Le scelte di policy sono mie, da confermare.** Non avendo una tua
  risposta ho scelto default prudenti: la query salvata come impronta
  (HMAC) e non come testo, conservazione di 12 mesi, lettura riservata
  all'admin, corpi delle richieste mai registrati. Se la vostra policy
  (o il DPO) chiede altro, si cambia da configurazione
  (`audit_query_mode`, `audit_retention_months`).
- **Cosa l'impronta della query permette e cosa no.** Permette di sapere
  che due ricerche erano uguali e quanto erano lunghe, non di leggerle. Con
  la chiave (`secrets/audit.key`) chi ha accesso al server può però
  provare delle frasi candidate e vedere se l'impronta coincide: la chiave
  va protetta come un segreto.
- **Integrità, non immutabilità.** Ogni riga contiene l'hash della
  precedente, quindi una riga modificata o cancellata in mezzo al file
  viene rilevata da `verify`. Ma:
  - chi ha accesso al file può **riscriverlo tutto** ricalcolando la
    catena, o **cancellare un file intero**: la catena rileva le
    manomissioni maldestre, non quelle di chi sa come è costruita;
  - l'**ultima riga** viene verificata solo dal processo che l'ha scritta,
    finché è in esecuzione; dopo un riavvio una modifica della sola coda
    del file non è rilevabile.

  Per un registro davvero immodificabile servono permessi del filesystem
  (il servizio scrive, nessun altro modifica) o, meglio, l'invio degli
  eventi in tempo reale a un sistema esterno (SIEM, syslog remoto). Non è
  implementato.
- **Due processi che scrivono insieme.** Server e CLI scrivono lo stesso
  registro sotto un lock tra processi, quindi la catena resta coerente.
  Se però il lock non arriva entro 5 secondi l'evento viene scritto
  comunque (perdere un evento di audit sarebbe peggio): in quel caso
  eccezionale `verify` può segnalare un'interruzione della catena che non
  è una manomissione.
- **Cosa non viene registrato.** Le ricerche fatte da riga di comando
  (`wikibridge search`) e le letture dirette del disco (vedi 3.1) non
  passano dal server e non finiscono nel registro. Gli eventi di sola
  navigazione (`/wiki/tree`, `/graph`) non sono registrati uno per uno.
- **Nessuna interfaccia.** Il registro si consulta da API o da CLI; la GUI
  di callAPIcall non lo mostra.

### 2.4 Chunk e storico delle versioni

**Correzione.** In una versione precedente di questo documento avevo
scritto che avevi rimandato sia il chunking sia lo storico: non è così. Il
chunking l'avevi chiesto ("le pagine e i chunk hanno riferimenti a cascata…
aggiorna i chunk senza duplicarli"); avevi rimandato solo lo storico delle
versioni. Il chunking ora c'è.

**Stato del chunking: fatto.** Ogni documento è diviso in chunk per
sezione, con chiavi stabili; un aggiornamento riscrive nell'indice solo i
chunk cambiati; ogni chunk ha la propria versione, registrata in una
tabella che sopravvive alle ricostruzioni dell'indice; ricerca e contesto
RAG restituiscono la sezione pertinente. Limiti:

- **Documenti senza titoli.** Le chiavi sono stabili rispetto alle
  *sezioni*. In un documento lungo senza titoli (un PDF convertito male,
  un testo continuo) le parti sono numerate in ordine: inserire un
  paragrafo all'inizio cambia la chiave di tutte le parti successive, che
  vengono quindi riscritte e prendono una nuova versione.
- **Titolo di sezione rinominato.** Per il sistema è una sezione nuova: i
  suoi chunk ripartono dalla versione 1 e quelli vecchi spariscono.
- **Registro delle versioni dentro `index.db`.** Sopravvive alla
  ricostruzione dell'indice (`wikibridge index`) e ai cambi di schema, ma
  se il file `index.db` viene cancellato i contatori dei chunk ripartono da
  1. La versione del *documento* invece è nel suo frontmatter e non si
  perde mai.
- **Entità e concetti** restano divisi per fonte (un segmento per fonte),
  non per sezione.

**Storico delle versioni: non fatto (rimandato da te).** Oggi un
aggiornamento sovrascrive: non si può chiedere "com'era questo documento o
questa sezione il mese scorso". Tutto è predisposto: ogni chunk ha
versione, data di inizio validità e un campo di fine validità (oggi sempre
vuoto); per attivare lo storico basta, invece di cancellare un chunk
cambiato, chiuderne la validità e conservarne il testo. **Regola da
rispettare quando lo si farà**: una versione vecchia va mostrata con le ACL
*attuali*, mai con quelle che aveva allora, altrimenti una versione con
permessi più larghi diventa un leak.

### 2.5 Pagine di sintesi con i permessi delle fonti

**Stato: fatto** (`POST /syntheses`, `wikibridge synthesis`). Una sintesi
dichiara le fonti da cui deriva e la vede solo chi vede tutte quelle fonti,
ricalcolato a ogni richiesta: se una fonte si restringe o sparisce, la
sintesi segue senza essere riscritta. Quello che resta:

- **Le sintesi vecchie** (scritte prima, senza elenco delle fonti) restano
  visibili solo all'admin: non si può sapere da dove vengono.
- **Dipende da chi scrive.** Il sistema si fida delle fonti dichiarate. Un
  agente o un utente che mette in una sintesi un'informazione riservata e
  dichiara solo fonti pubbliche la rende visibile a tutti: nessun codice
  può verificare da dove arriva davvero un testo. Per questo via API chi
  scrive deve almeno poter vedere tutte le fonti che dichiara, e la skill
  chiede agli agenti di dichiarare sempre tutte le pagine usate.
- **Scrittura diretta su disco.** Un agente che scrive un file in
  `syntheses/` senza usare il comando crea una sintesi senza fonti (quindi
  visibile solo all'admin): non è un leak, ma è lavoro perso.

### 2.6 Aggiornamenti efficienti (change feed)

**Stato: fatto per Confluence e MediaWiki.** Con una ricerca (CQL per
Confluence, RecentChanges per MediaWiki) si sa cosa è cambiato dall'ultimo
controllo, e solo quelle pagine vengono ricontrollate; Confluence elenca
anche il ramo con una ricerca invece di una chiamata per pagina. In una
prova con 31 pagine di cui una modificata: 1 controllo di revisione e 2
ricerche, invece di 31 controlli. Limiti:

- **Margine di 24 ore su Confluence.** CQL interpreta le date nel fuso
  orario dell'utente Confluence, che wikibridge non conosce: per non perdere
  modifiche, il feed include anche le pagine modificate nelle 24 ore prima
  dell'ultimo controllo. Quelle pagine vengono ricontrollate (con una
  chiamata leggera, non riscaricate): se si aggiorna più volte al giorno il
  risparmio è minore.
- **Indice di ricerca in ritardo.** L'indice di Confluence può essere in
  ritardo o incompleto. Una pagina che manca dall'elenco non viene mai
  cancellata sulla fiducia: prima si chiede a Confluence se esiste ancora
  ed è ancora nel ramo. Una pagina **nuova** però può comparire solo al
  controllo successivo, quando l'indice l'ha registrata.
- **MediaWiki conserva le modifiche recenti per un periodo limitato** (di
  norma 90 giorni): se l'ultimo controllo è più vecchio di 30 giorni si
  torna al controllo pagina per pagina.
- **Permessi.** Con `--permissions` le restrizioni vanno comunque rilette
  pagina per pagina, perché in Confluence cambiarle non modifica la pagina
  e il feed non le vede.
- **Altre sorgenti.** Notion (la cui ricerca permette di ordinare per data
  di modifica) e l'HTML generico non hanno un feed: controllo pagina per
  pagina. Per l'HTML senza ETag né Last-Modified la pagina viene
  riscaricata a ogni controllo (non riscritta, se il testo non cambia).
- **Primo aggiornamento degli import vecchi.** Gli import creati prima di
  questa modifica non hanno il punto di partenza del feed: il loro primo
  aggiornamento è ancora pagina per pagina.

### 2.7 Login e gestione ACL nella GUI di callAPIcall

**Stato: login e gestione delle ACL fatti.**

**Cosa c'è.** Nella sezione Conoscenza, se wikibridge è multi-utente, la
GUI mostra un pannello di accesso: username e password (LDAP/AD) oppure
"Accedi con l'account aziendale" (OIDC o SAML: si apre il browser e la GUI
aspetta la fine del login). Dopo l'accesso una barra "Vedi la conoscenza
come" mostra nome, livello di classificazione e ruoli (quelli oltre il
quarto si aprono a richiesta), con il pulsante Esci. Se wikibridge rifiuta
la sessione (scaduta, revocata, account disattivato) la GUI torna al
pannello di accesso con un avviso.

La sessione è **una sola per l'app**: la tiene il backend call-bridge
(nuove route `GET /knowledge/session`, `POST /knowledge/login`, `POST
/knowledge/logout`), e la GUI la usa per le sue chiamate dirette a
wikibridge. Così quello che l'operatore vede nella GUI e quello che
l'assistente riceve nelle chiamate passano dallo stesso filtro. Il token
resta in memoria (mai su disco né nel `localStorage`). Traduzioni in tutte
le 15 lingue dell'interfaccia; verificato a 360×640, 390×844 e 1280×800
(nessuno scroll orizzontale, pulsanti di almeno 44 px, focus visibile,
animazioni disattivate con "riduci movimento").

**Cosa serve in configurazione.** In multi-utente wikibridge accetta
richieste dal browser solo dalle origini elencate in `cors_origins`: per la
GUI Tauri va aggiunta `"http://tauri.localhost"` (Windows; su macOS/Linux
`"tauri://localhost"`). Se manca, la GUI lo dice esplicitamente invece di
dare un generico "server non raggiungibile".

**Gestione delle ACL.** Un admin trova nella sezione Conoscenza la scheda
**Permessi**: stato del filtro del progetto (acceso/spento), chi può aprire
il progetto, collezioni (nome, sorgenti da spuntare, ruoli e persone,
classificazione; modifica ed eliminazione). Nella vista Wiki, aprendo un
documento, il riquadro **Permessi del documento** mostra la collezione e i
permessi letti dalla fonte (in sola lettura, perché li decide la fonte) e
permette di cambiare le regole proprie del documento. Chi non è admin non
vede né la scheda né il riquadro.

Il punto delicato era l'anteprima, ed è risolto così: ogni modifica parte
senza conferma; se allarga l'accesso wikibridge risponde con l'anteprima
e la GUI la mostra **in linea**, sopra i pulsanti "Conferma e salva" e
"Annulla": chi potrà vedere, chi viene tolto dall'elenco, quali sorgenti
escono dalla collezione, il cambio di classificazione, quanti documenti
sono coinvolti. Restringere non chiede conferma. I ruoli si scelgono da un
elenco proposto (i ruoli configurati nelle mappe dei gruppi e quelli già
in uso), le persone si indicano per username e compaiono con il loro nome
("Mario Rossi (mario)") anche nell'anteprima, prima di essere salvate.

Per farlo ho dovuto completare anche il server:

- **Spegnere il filtro** di un progetto ora chiede conferma (API e CLI,
  `acl enforce off --yes`) ed è registrato nell'audit. Prima bastava una
  richiesta: era l'allargamento più ampio possibile e l'unico senza
  anteprima, e la CLI non lo registrava nemmeno.
- **Le modifiche a un documento** ora hanno anteprima e conferma come
  collezioni e gate, compreso il caso meno ovvio: togliere tutte le regole
  proprie di un documento allarga l'accesso (decide la sola collezione).
- **Nomi leggibili**: `acl.json` conserva un'etichetta per ogni persona
  indicata per username. È solo per la visualizzazione: le decisioni usano
  sempre `user:<objectGUID>`. Le persone assegnate prima di questa modifica
  compaiono come "persona xxxxxxxx…" finché non vengono riassegnate.

Verifica: percorso completo in Chrome headless contro un wikibridge vero
(directory LDAP simulato) e il blueprint vero del backend. Il percorso:
login admin; attivazione del filtro; gate; nuova collezione con anteprima e
conferma; spegnimento del filtro annullato; permessi di un documento. Alla
fine lo stato su wikibridge coincide con quello impostato, e un utente non
admin non vede la scheda né, per effetto del gate appena impostato, il
progetto. Layout a 360, 390 e 1280 px senza scroll orizzontale, controlli
di almeno 44 px, contrasto del testo ≥ 4,5:1 misurato in tema chiaro e
scuro. Nel farlo ho corretto anche due difetti già presenti nella sezione:
le schede non stavano in 360 px e le etichette delle schede non attive
avevano contrasto 3,6:1.

**Cosa manca e perché.**
- **Più operatori sulla stessa installazione** contemporaneamente: la
  sessione è una per app. Va bene quando ogni operatore ha la sua istanza
  di callAPIcall; un'istanza condivisa da più persone richiederebbe una
  sessione per utente anche nel backend.
- **Verifica nell'app Tauri vera**: login e gestione ACL sono stati provati
  in Chrome (la porta del backend reale era occupata dalla tua istanza in
  esecuzione, che non ho toccato). Non ho ricompilato l'app Tauri.
- **Permessi di sintesi e riunioni dalla GUI**: il riquadro compare anche
  sulle riunioni, ma sintesi, entità e concetti non si modificano da qui,
  perché i loro permessi derivano dai documenti di origine.

### 2.8 Prove contro sistemi reali

Tutto è stato provato con sistemi **simulati**: un directory LDAP finto
(ldap3 in modalità mock), un IdP OIDC finto ma reale via HTTP, un IdP SAML
finto che firma davvero le asserzioni, un Confluence simulato e permessi
NTFS simulati. Non sono stati provati contro sistemi veri:

- un **Active Directory** reale (gruppi annidati con la regola "in chain",
  attributi binari come `objectGUID`, paginazione, replica tra DC);
- **StartTLS / ldaps** con certificati veri;
- un **IdP OIDC reale** (Entra ID o Keycloak), con i suoi claim specifici,
  l'"overage" dei gruppi, la rotazione delle chiavi di firma;
- un **IdP SAML reale** (AD FS, Entra ID), con i suoi nomi di attributo e
  formati di NameID;
- **permessi NTFS di un dominio** (gruppi `DOMINIO\...`, condivisioni di
  rete): la lettura vera è stata provata solo sui file locali di questa
  macchina;
- la **GUI dentro l'app Tauri** compilata (provata in Chrome, vedi 2.7);
- il server **waitress** e il TLS diretto di wikibridge.

Il simulato copre la logica; non copre le differenze di comportamento dei
sistemi reali, che vanno verificate in un ambiente di prova prima della
produzione.

### 2.8.1 Ambiente per eseguire i test

La suite di test (`tests/`, 150 test) ora gira nella **venv del
progetto**, dove ho installato anche `pytest`:

```
C:\Projects\wikibridge\.venv\Scripts\python -m pytest
```

Risultato: 138 passati e i 12 test SAML saltati, perché `python3-saml` non
è installato nella venv (vedi 2.9). I test SAML passano nell'ambiente di
prova della sessione, che ha `python3-saml`.

**Attenzione ai blocchi di rete locale su questa macchina.** Il 29
settembre, dalle 3 circa, i test che avviano server HTTP finti in locale
(Confluence, IdP OIDC: 29 test in 4 file) hanno cominciato a bloccarsi a
tratti per 20–30 secondi e a volte a fallire per timeout. Lo stack
catturato durante il blocco mostra una connessione TCP a 127.0.0.1 che non
completa l'handshake, mentre il server finto è inattivo in attesa di
connessioni. Non è un problema di wikibridge: nello stesso momento restano
bloccate a metà handshake anche connessioni locali di altri programmi
(PostgreSQL sulla 5432, il tuo call-bridge sulla 8770). Tra i processi con
connessioni anomale c'è l'antivirus (Avast), i cui filtri di rete sono una
causa nota di questo sintomo; non l'ho verificato e non ho toccato le sue
impostazioni. Gli altri 109 test, che non usano la rete, passano in 10
secondi; i 29 test di rete passano quando il disturbo si attenua. Se la
suite ti sembra lenta o fallisce solo su `test_change_feed`, `test_oidc`,
`test_source_permissions` o `test_update_wiki` con un timeout, controlla
prima la rete locale (per esempio escludendo `127.0.0.1` dalla protezione
web dell'antivirus).

Eseguire la suite nella venv vera ha fatto emergere due problemi che
l'ambiente di prova nascondeva:

- **PyJWT 2.15** (installato nella venv; l'ambiente di prova aveva una
  versione più vecchia) rifiuta anche solo di *creare* il token HS256
  malevolo che il test usa per simulare l'attacco di "confusione di
  algoritmo". Il server era corretto; ho riscritto il test perché forgi il
  token a mano.
- **Un evento di audit perso** sotto carico: vedi 3.5.

### 2.9 Dipendenze della venv

**Stato: installate** (29 settembre 2026, su tua richiesta). Nella venv di
wikibridge (`C:\Projects\wikibridge\.venv`, Python 3.12), quella che
callAPIcall usa per avviare il server, ci sono ora `ldap3` 2.9.1, `PyJWT`
2.15.1 con `cryptography` 50, `waitress` 3.0.2, `pywin32` 312 e `pytest`
9.1.1, installati da `requirements.txt`. Verificati: import di tutti i
moduli, suite completa, e i due script di integrazione con callAPIcall
(password e OIDC) eseguiti con il Python della venv.

**Non installato: `python3-saml`**, perché non era nella lista e porta con
sé `xmlsec`, una libreria nativa che deve essere compilata sulla stessa
versione di `libxml2` di `lxml`, già presente nella venv (6.1.1). Serve solo
con `identity_provider = "saml"`; in quel caso:

```
C:\Projects\wikibridge\.venv\Scripts\pip install -e "C:\Projects\wikibridge[saml]"
C:\Projects\wikibridge\.venv\Scripts\python -m pytest tests/test_saml.py
```

Se i test SAML falliscono con un errore di versione di `libxml2`, è il
disallineamento tra `lxml` e `xmlsec`: si risolve reinstallando `lxml`
senza i pacchetti binari (`pip install --no-binary lxml lxml`).

### 2.10 Manuale utente

**Stato: aggiornato in parte.** Il manuale `docs/WikiBridge-User-Manual.html`
(in inglese, come il resto) ha una nuova **Parte IX — Multi-User Access
Control**: modello dei permessi, scelta del provider (LDAP, OIDC, SAML),
sessioni e revoche, etichettatura, permessi da Confluence e NTFS,
aggiornamenti e storico, sintesi e riunioni, registro di audit, elenco
delle nuove API e integrazione con callAPIcall. Sono stati corretti anche i
paragrafi delle parti esistenti che non erano più veri (ingest,
aggiornamenti, `/health`, `/ingest/dir`).

**Cosa manca.** Le parti di riferimento simbolo per simbolo (VI moduli
Python, VII formati dei file, III tutti i campi di configurazione) non
documentano ancora i moduli nuovi (`acl`, `identity`, `oidc_identity`,
`saml_identity`, `updater`, `audit`, `rag`, `source_acl`, `ntfs_acl`, ...)
né i nuovi file (`acl.json`, `ingests.json`, `identities.json`, registro di
audit). Il manuale lo dichiara nella sezione "Version covered". La ragione
è di proporzione: quelle parti descrivono ogni parametro di ogni funzione,
e farlo per circa quindici moduli significa raddoppiare il manuale; nel
frattempo i campi di configurazione sono commentati in `config.py` e
spiegati nel `README.md`.

### 2.11 Commit

Il lavoro successivo al secondo commit non è committato, in nessuno dei due
repository, perché non me l'hai chiesto esplicitamente. Più si accumula,
più è difficile separarlo in commit leggibili.

Nella prima separazione dei commit avevo proposto **un commit per fase**,
ma non è stato possibile: le fasi successive avevano riscritto gli stessi
file, quindi le versioni intermedie non esistevano più, e ricostruirle
avrebbe prodotto commit che non funzionano. Il mio lavoro è finito in un
unico commit con il messaggio organizzato per fasi.

---

## 3. Problemi irrisolti e limiti noti

### 3.1 Chi legge il disco scavalca ogni permesso

Le ACL valgono per chi passa dall'API di wikibridge. Qualunque processo che
legge i file in `data/` direttamente — un agente con accesso al
filesystem, la skill `wiki-import`, uno script — vede tutto.

- In callAPIcall, in modalità multi-utente, il cervello **non riceve più**
  il percorso della wiki, e il prompt gli dice di non cercarla. Ma l'agente
  Hermes ha comunque accesso al filesystem: se decidesse di leggere
  `data/` lo potrebbe fare. Un'istruzione nel prompt non è un controllo di
  sicurezza.
- **Non è risolvibile via codice**: serve una misura di deployment.
  `data/` deve essere leggibile solo dall'account di sistema con cui gira
  il servizio wikibridge, e gli agenti devono girare con un account
  diverso.
- **Controllo all'avvio.** In modalità multi-utente il server controlla i
  permessi di `data/` e, se è leggibile da "Everyone", "Authenticated
  Users" o "Users" (su Linux: da tutti), scrive un avviso nel log e lo
  espone in `GET /identity/status` (`deployment_warnings`). Il controllo
  avvisa, non blocca: su questa macchina di sviluppo l'avviso compare,
  perché `data/` eredita i permessi della cartella utente.
- **Come chiudere la cartella** (Windows, da amministratore, sostituendo
  l'account del servizio): `icacls data /inheritance:r /grant:r
  "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F" "DOMINIO\svc-wikibridge:(OI)(CI)M"`.
  Non lo faccio io: cambia chi può aprire i dati sulla tua macchina ed è
  una scelta di deployment.

### 3.2 Riunioni: partecipanti non riconosciuti

Il contesto di una riunione è l'intersezione dei permessi dei
partecipanti, ma un partecipante viene riconosciuto solo se arriva come
username o email del directory. Il meetbot spesso riceve i **nomi
visualizzati** ("Mario Rossi"): non riconosciuti.

- Per ciò che viene detto a tutti (politica `public`), un non riconosciuto
  riduce il contesto ai soli contenuti pubblici: sicuro, ma a volte troppo
  restrittivo.
- Per l'analisi di fine riunione (politica `ignore`) i non riconosciuti
  non contano, e la pagina della riunione è visibile solo a chi l'ha
  salvata e ai riconosciuti.
- Con OIDC le persone sono riconoscibili per nome solo **dopo il loro primo
  login** a wikibridge (non c'è un directory da interrogare).

Servirebbe che callAPIcall passasse le email dei partecipanti (dal
calendario o dalla piattaforma di videochiamata).

### 3.3 Suggerimenti del copilota letti all'interlocutore

Il copilota costruisce i suggerimenti con i permessi dell'**operatore**,
perché sono mostrati a lui. Se l'operatore ne legge uno all'interlocutore
(anche facendolo pronunciare all'avatar), può condividere informazioni che
l'interlocutore non dovrebbe avere. È una scelta consapevole: la decisione
resta a una persona. Se l'avatar dovesse rispondere **da solo**, senza
l'operatore, andrebbe usata la politica "riunione" con l'interlocutore come
partecipante (quindi solo contenuti pubblici, se esterno).

### 3.4 Sessioni e cache in memoria

Sessioni, cache dei ruoli e stato dei login SSO in corso vivono nella
memoria del processo:

- un **riavvio** del server disconnette tutti (serve un nuovo login);
- con **più istanze** di wikibridge dietro un bilanciatore, una revoca o
  un'invalidazione su un'istanza non vale per le altre, e una sessione
  creata su un'istanza non è riconosciuta dalle altre.

Per più istanze servirebbe uno store condiviso (per esempio Redis). Per
l'uso attuale (un processo locale) non è un problema.

### 3.5 Scritture concorrenti tra processi

**Risolto in gran parte.** Server, CLI, cron e agenti (anche in WSL)
scrivono `imports.json`, `ingests.json`, il registro dei progetti,
`acl.json` e il registro di audit sotto un lock tra processi, e due
aggiornamenti della stessa sorgente non partono insieme (il secondo
riporta `busy`). Prima non era solo un rischio di perdere dati: una prova
senza lock ha mostrato che su Windows i processi concorrenti andavano in
**errore**, perché usavano lo stesso file temporaneo. Il lock è un file
creato in modo atomico, perché i lock di sistema di Windows e di Linux non
si vedono tra loro su `/mnt/c`.

**Difetto trovato e corretto il 29 settembre.** Eseguendo la suite nella
venv vera, un test ha perso un evento di audit su 60, ma solo una volta su
quattro esecuzioni. Non l'ho archiviato come test instabile: con un test di
carico (8 processi insieme) si riproduceva sempre, con 6–11 eventi di audit
persi su 1200 e un record di `imports.json` perso su 320. La causa è un
comportamento di Windows. Quando un processo rilascia il lock cancellando
il file, se nello stesso istante un altro lo sta leggendo (per sapere chi
lo tiene), il file resta "in cancellazione" per un attimo. In quell'attimo
chi prova a crearlo riceve "accesso negato" invece di "esiste già", e il
mio codice lo trattava come un errore vero: l'audit rinunciava a scrivere.
Ora "accesso negato" sul lock vuol dire "occupato, riprova", e anche la
cancellazione del lock viene ritentata per un paio di secondi. Dopo la
correzione: zero perdite in tre giri di carico. Un test nuovo simula questo
comportamento di Windows in modo deterministico, e fallisce sulla versione
vecchia del codice.

Limiti:

- **Processo che muore tenendo un lock.** Il lock resta sul disco finché
  non scade: 2 minuti per le scritture dei file, **1 ora** per un
  aggiornamento di sorgente. In quell'ora gli aggiornamenti di quella
  sorgente rispondono `busy`; per sbloccare prima si cancella a mano il
  file in `data/projects/<progetto>/locks/`, che contiene chi lo teneva e
  da quando.
- **Singole pagine.** La modifica delle ACL di una pagina (`acl page`) e
  l'aggiornamento del suo contenuto da parte di un altro processo nello
  stesso istante non sono coordinati: l'ultimo che scrive vince. È molto
  improbabile, e il controllo aggiornamenti successivo rimette le cose a
  posto.

### 3.6 Tempi di propagazione delle revoche

Quando a qualcuno viene tolto un permesso nel directory, quanto tempo
passa prima che wikibridge se ne accorga:

| Situazione | Tempo |
|---|---|
| Invalidazione manuale dell'admin (`/identity/invalidate`) | immediato |
| Controllo modifiche richiamato (`/identity/sync-groups`) o automatico (`ldap_change_poll_s`) | al controllo successivo |
| Senza controlli | alla scadenza della cache (`entitlement_cache_ttl_s`, 5 minuti di default) |
| OIDC | alla scadenza della cache, quando il refresh token viene riusato |

Limiti del controllo modifiche su AD:

- `uSNChanged` è un contatore **del singolo domain controller**: il
  controllo deve interrogare sempre lo stesso DC, e una modifica fatta su
  un DC di un'altra sede arriva solo dopo la replica (secondi o minuti).
- La **cancellazione di un intero gruppo** non viene vista (gli oggetti
  cancellati non compaiono nelle ricerche normali): vale la scadenza della
  cache.
- Le modifiche ai **gruppi annidati intermedi** sono viste solo se quei
  gruppi rientrano nel filtro osservato (`ldap_watch_filter`).

### 3.7 OIDC: limiti specifici

- **Gruppi omessi ("overage")**: Entra ID, se un utente ha troppi gruppi,
  non li mette nel token e al loro posto mette un rimando a Microsoft
  Graph. wikibridge lo segnala nel log ma non interroga Graph: quell'utente
  risulta senza gruppi. Soluzione consigliata: usare gli *app roles*
  (`oidc_groups_claim: "roles"`).
- **Nessun directory**: per concedere un permesso "a Mario" serve che Mario
  abbia già fatto login una volta (registro `identities.json`), a meno di
  usare l'email come identificativo.
- **Refresh senza nuovo id_token**: alcuni IdP al refresh non restituiscono
  un nuovo `id_token`; in quel caso i gruppi restano quelli del login fino
  al login successivo.

### 3.8 Default-deny e documenti già importati

Con le ACL attive, un documento senza ruoli espliciti è visibile solo
all'admin (a meno che sia `public` o appartenga a una collezione). È la
scelta sicura, ma significa che **attivare le ACL su un progetto esistente
rende invisibile agli utenti tutto ciò che non è stato etichettato**. Prima
di attivarle vanno create le collezioni o etichettati i documenti. Non
esiste ancora uno strumento di migrazione che faccia un'etichettatura
iniziale (arriverà in parte con la lettura dei permessi dalle sorgenti).

### 3.9 Testo "legacy" nelle pagine entità

Le pagine entità/concetto create prima dei segmenti per fonte contengono
testo non attribuibile a una fonte precisa. Con le ACL attive è visibile
solo all'admin (quarantena). Per ripulirlo c'è `wikibridge catalog-rebuild`,
che rifà la catalogazione di tutte le fonti con l'LLM: **costa una
chiamata LLM per fonte** e, durante l'esecuzione, le pagine entità
scompaiono per poi essere ricreate.

### 3.10 Contenuti visibili solo all'admin

Con le ACL attive `overview.md`, `log.md`, le sintesi salvate senza
dichiarare le fonti e il testo legacy delle entità sono visibili solo
all'admin. `overview.md` è una
sintesi generata dall'LLM che mescola tutte le fonti: non si può filtrarla
per utente senza rigenerarla per ogni combinazione di permessi.

### 3.11 Limiti di sicurezza residui minori

- **Blocco del login per un utente**: dopo 5 tentativi falliti in 5 minuti
  il login di quell'utente a wikibridge viene bloccato per la durata della
  finestra. Un attaccante può quindi impedire temporaneamente a una
  persona di entrare in wikibridge (ma non far bloccare il suo account
  AD, perché la soglia di wikibridge è più bassa di quella di AD).
- **Tempi di risposta al login**: il bind fittizio rende simili i tempi di
  "utente inesistente" e "password errata", ma una piccola differenza può
  restare.
- **Statistiche di ricerca**: il ranking BM25 è calcolato su tutto
  l'indice, compresi i documenti che l'utente non vede. In teoria l'ordine
  dei risultati visibili può lasciar intuire l'esistenza di termini rari
  nei documenti riservati. Il rischio è molto basso (i punteggi non sono
  restituiti), ma esiste.
- **Relazioni vecchie senza fonte** nel grafo: sono nascoste agli utenti
  non admin, perché non si sa da quale fonte provengano.

### 3.12 Limiti del controllo aggiornamenti

- **Anteprima (`--dry-run`) imprecisa** per le pagine importate prima dei
  campi di revisione: vengono contate tutte come "da aggiornare", perché
  non si può sapere se sono cambiate senza scaricarle.
- **Rinomina di un titolo di sezione**: i suoi chunk risultano cancellati
  e ricreati (vedi 2.4).
- **Modifiche ai soli permessi alla fonte** (per esempio una restrizione
  aggiunta in Confluence senza cambiare il testo) non cambiano la
  revisione: per Confluence importato con `--permissions` vengono viste
  dal controllo aggiornamenti; per le altre sorgenti no.
- **Gruppi di file spariti tutti insieme**: se sparisce più di metà di un
  ramo in un colpo, per prudenza non si cancella nulla e si segnala il
  problema; se la sparizione era voluta, bisogna rimuovere la sorgente a
  mano.

### 3.13 Scala

Il filtro e la ricerca sono dimensionati per progetti fino a qualche
decina di migliaia di pagine (SQLite FTS5 locale). Oltre circa 100.000
blocchi, o con ACL molto frammentate (molte condivisioni a persona
singola), servirebbe un motore diverso (per esempio un database vettoriale
con filtri sui metadati).

---

## 4. Decisioni che spettano a te

1. **Commit**: posso separare e committare il lavoro non salvato nei due
   repository?
2. **SharePoint**: serve? Richiede un'app registrata nel tenant (2.2).
3. **Confluence**: Cloud o Server/Data Center? Su Cloud servirà una mappa
   `source_user_map` per le restrizioni a persone singole (vedi 2.2).
4. **SAML**: ruoli dal directory (`saml_roles_source = "ldap"`, revoche in
   tempo reale) o dall'asserzione (più semplice, revoche solo alla scadenza
   della sessione)? Vedi 2.1.
5. **Audit**: vanno bene i default scelti (query come impronta, 12 mesi,
   solo admin)? Serve l'invio a un SIEM esterno (vedi 2.3)?
6. **Avatar autonomo**: l'avatar risponderà mai da solo, senza operatore?
   Se sì, va applicata la politica delle riunioni (vedi 3.3).

## 5. Prima di andare in produzione

- [x] Installare le dipendenze nella venv (2.9); `python3-saml` solo se si
      usa SAML.
- [ ] Rendere `data/` leggibile solo all'account del servizio (3.1):
      `GET /identity/status` non deve mostrare `deployment_warnings`.
- [ ] Per le cartelle di documenti riservati usare `--permissions` e
      mappare i gruppi Windows in `source_group_map` (2.2).
- [ ] Provare LDAP/AD, OIDC o SAML contro i sistemi reali (2.8).
- [ ] Aggiungere l'origine della GUI (`http://tauri.localhost`) a
      `cors_origins` e provare il login dall'app Tauri compilata (2.7).
- [ ] Configurare TLS (`ldaps://` o StartTLS; HTTPS o reverse proxy se il
      server è esposto in rete).
- [ ] Etichettare i documenti o creare le collezioni prima di attivare le
      ACL (3.8); per Confluence importare con `--permissions`, completare
      `source_group_map` finché il report non mostra più `acl_unmapped`, e
      creare una collezione per ogni spazio (2.2).
- [ ] Attivare un controllo periodico delle modifiche (`ldap_change_poll_s`
      o un cron su `/identity/sync-groups`) (3.6).
- [ ] Confermare la policy del registro di audit, proteggere
      `secrets/audit.key` e la cartella `data/audit/` (solo l'account del
      servizio), e valutare l'invio a un SIEM (2.3).
- [ ] Committare il lavoro (2.11).
