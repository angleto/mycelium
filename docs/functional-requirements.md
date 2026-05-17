# Requisiti funzionali

## FR-1 Task management

CRUD task (titolo, descrizione markdown, priorita P1-P4, date
start/due, assegnatari, `executor`), sottotask gerarchici, checklist,
quick-add con parsing naturale (es. `domani #tag @progetto !p1`), viste
lista/board/calendario/oggi-prossimi, filtri salvati, ricerca
full-text, completamento, archiviazione, soft-delete con ripristino,
commenti, log attivita, allegati. Task ricorrenti e reminder (FR-12).
I task portano anche attributi di pianificazione personale (costo,
luogo, contesto, necessita): vedi FR-14.

Carve-out: la soft-delete **non** si applica a fatture emesse e
documenti conservati (vedi FR-9 e ADR-0009).

## FR-2 Tassonomia unificata

Un solo `tag` con `kind`. `client` e `project` hanno profili satellite
tipizzati (vincoli, validazione, FK), non JSONB libero: serve sia alla
fatturazione (dati legali) sia all'isolamento memoria per progetto.
Associazione task-tag unica. RBAC e org-scope su tutti i tag.

## FR-3 Dipendenze e grafo di workflow

Quattro tipi di dipendenza (FS/SS/FF/SF) con lag/lead; semantica nelle
disuguaglianze in tempo lavorativo (FR-4). Rilevamento cicli prima
dell'inserimento, nel service layer. Vincoli DB: no self-dependency,
unique (predecessore, successore, tipo), stesso org. Visualizzatore
grafo DAG (pan/zoom, click apre il task, layout a livelli dagre/elkjs).
"Bloccato" e un overlay derivato non persistente, non uno stato di
workflow.

## FR-4 Scheduling deterministico (completo da subito)

Modello risorse:

- ogni persona e una risorsa unaria sul proprio calendario;
- gli eventi/appuntamenti sono prenotazioni fisse esclusive;
- i task con `executor=human` della stessa persona sono **serializzati**
  (nessuna sovrapposizione temporale) attorno agli appuntamenti;
- i task con `executor=llm_agent` sono fuori dalla timeline umana
  (paralleli, schedulati solo per precedenza).

Motore: passata avanti/indietro CPM **logico** deterministico su
calendari lavorativi (ES/EF/LS/LF, slack, percorso critico logico,
onesti perche senza contesa di risorse) + **piazzamento seriale
deterministico per-persona** con regola a priorita (priority desc, due
asc, created asc, id) nelle finestre libere. Output stabile: i pin
sopravvivono al ricalcolo.

Piano vs consuntivo: `remaining_effort_h` (default = stima),
`actual_start` (da prima time entry o transizione di stato); task in
stato terminale = durata residua zero; task in corso = ES pinnato a
`actual_start`, si schedula solo il residuo.

Modalita/pin: `schedule_mode` {auto (ASAP), manual} + `constraint`
{none, SNET, MSO, MFO}. Il drag con write-back imposta manual o un
constraint e il ricalcolo lo rispetta.

Determinismo: dato l'input, output identico; `schedule` derivata con
`computed_at` + `input_fingerprint` + flag di staleness; la
ricomputazione piu recente supera la precedente.

Rollup: effort sulle foglie; il task sommario deriva start = min,
finish = max dei figli; in v1 le dipendenze solo su foglie.

Gantt: barre, dipendenze, percorso critico logico, milestone,
indicatore di sovraccarico per persona/giorno, drag.

Disuguaglianze in tempo lavorativo (lag in minuti lavorativi con segno,
calendario del predecessore):

- FS: `start_succ >= finish_pred + lag`
- SS: `start_succ >= start_pred + lag`
- FF: `finish_succ >= finish_pred + lag`
- SF: `finish_succ >= start_pred + lag`

Il leveling ottimizzante (CP-SAT) e un enhancement post-v1, non il
default. Vedi [ADR-0004](adr/0004-deterministic-scheduler.md).

## FR-5 Time tracking

Timer live (uno running per utente, start/stop/ripresa), voci manuali,
idle detection lato client, timer running visibile in GUI e MCP in
realtime, report per tag client/project/generic/utente/periodo,
billable, tariffe, export CSV/PDF. Alimenta `remaining_effort` (FR-4) e
le fatture (FR-9).

## FR-6 Workflow di stato configurabili

WorkflowDefinition per Org (una di default): stati ordinati,
transizioni, iniziale/terminale. Override per progetto: un
`project_profile` punta a un workflow che estende il default (es.
aggiunge uno stato e ne impone il passaggio). Enforcement della
macchina a stati nel service layer (unico choke point GUI/REST/MCP).

## FR-7 Email: connector, triage, invio

- Gmail: OAuth2 (XOAUTH2 IMAP/SMTP), token cifrati, refresh.
- Proton Mail: via Proton Mail Bridge (piano a pagamento), sidecar
  headless arm64; un'istanza per account, controller a scala; opex
  documentata, non blocker; introdotto dopo Gmail.
- IMAP/SMTP generico: self-hosted/Dovecot, basic o OAuth.
- Sync background idempotente e resiliente; il guasto di un account non
  degrada il resto.
- "Email to Task" da GUI o tool MCP: titolo da subject, descrizione da
  corpo, allegati riportati, assegnazione tag/client/project/
  assegnatario, link al messaggio sorgente.
- Invio/risposta SMTP nel thread. Niente gestione cartelle/label
  server-side in v1.

## FR-8 Memoria gerarchica e recupero

- Isolamento duro per (org, progetto): `memory_blobs` partizionata per
  org, predicato (org, progetto) obbligatorio, RLS obbligatoria. Default
  = progetto corrente; cross-progetto solo con autorizzazione esplicita
  e auditata. Nessun retrieval/summarization/consolidamento attraversa
  progetti o tenant.
- Provenienza esplicita `blob_sources` (N:1). Consolidamento solo entro
  stesso (org, progetto, thread/account), mai cross-soggetto; tombstone,
  non delete silenzioso.
- GDPR erasure: cancellare un messaggio propaga a embedding, summary,
  copia object-storage e a ogni blob consolidato che lo include
  (re-consolidamento dai sopravvissuti o tombstone).
- Tier: hot (corpo in PG su volume cifrato), warm (riassunto LLM se
  abilitato, corpo verso object storage), cold (embedding pgvector
  HNSW). Con LLM disabilitato: warm = corpo verso object storage +
  metadati, nessun riassunto.
- Tiering tipo memoria umana (ADR-0016): il tier e guidato da uno
  score di accesso con decay (frequenza + recency) e da un segnale di
  importanza, non solo da eta/dimensione. I concetti ricorrenti
  (cluster consolidati, provenienza preservata, entro (org, progetto))
  sono promossi a un tier compatto pre-caldo; cio che non ricorre
  decade ed e demosso. **Invariante**: la frequenza determina solo il
  tier di latenza, mai la ritenzione ne la visibilita; il cold resta
  sempre recuperabile (raro != non importante).
- Grader correttivo (CRAG adattato, ADR-0016): ogni retrieval e
  graduato (soglia deterministica sullo score RRF fuso + grader LLM
  locale opzionale). Rami: ok -> usa; incerto -> riscrivi/espandi
  query; insufficiente -> allarga lo scope entro il tenant o rispondi
  "evidenza insufficiente". Nessun ramo web (memoria privata, GDPR).
- Retrieval come tool agentico (ADR-0016): il recupero memoria e
  esposto come un tool MCP tra i tool deterministici; il planner
  LLM/MCP sceglie vector/SQL/strutturato. Decisione deterministica
  (ADR-0004/0013): l'LLM orchestra, non decide.
- Connessioni: si usa il grafo strutturale gia presente (DAG
  dipendenze, gerarchia tag/client/project, link email-task e
  provenienza), non un knowledge graph estratto via LLM dal testo
  (GraphRAG testuale differito).
- Multimodale differito: v1 text-first con estrazione testo dagli
  allegati; embedding multimodale (CLIP/ColPali, indice unico) come
  fase successiva.
- Retrieval ibrido baseline: ramo semantico (HNSW) + lessicale
  (tsvector/ts_rank, pg_trgm con indice trigram dedicato). Pre-filtro
  metadati = rilevanza; RLS + partizione = sicurezza. Per filtri molto
  selettivi (message-id, numero fattura) path esatto, non HNSW.
  `hnsw.iterative_scan = relaxed_order` tarato.
- RRF: K sovracampionato per ramo (circa 100), fusione RRF con k circa
  60, tiebreak deterministico (rank singolo, poi received_at, poi id),
  N finale; fusione entro (org, progetto).
- Embedder/LLM pluggable (pattern bitvision_phoenix: Protocol + factory
  DB-driven + DTO neutri + settings env + registry modelli). Flow
  aggiunge `EmbedderProvider`. Default modello multilingue piccolo
  CPU/ARM; scelta concreta in implementazione (BGE-M3, multilingual-E5,
  GTE-multilingual, Qwen3-Embedding su MTEB corrente).
- Re-embedding: nuova colonna/tabella per il nuovo modello (dim fissa),
  dual-write durante il backfill, backfill resumable, `CREATE INDEX
  CONCURRENTLY`. Garanzia onesta: nessun write-downtime; letture sul
  vecchio indice durante il backfill con possibile degrado di latenza
  su nodo singolo; cutover = swap del puntatore in transazione;
  rollback finche vecchia colonna+indice esistono.
- "Niente numpy" = nessuno store di similarita numpy lato app; i
  vettori stanno in pgvector server-side (l'`Embedder` locale puo
  dipendere da numpy transitivamente).

## FR-9 Fatturazione elettronica SDI (v1 B2B/B2C)

- Ruolo legale: in multi-tenant Flow trasmette per conto del tenant ed
  e quindi trasmittente/intermediario. Modello esplicito `SdiMandate`
  per-Org. Canale unico condiviso; identita del tenant nel payload
  (`CedentePrestatore`, `TerzoIntermediarioOSoggettoEmittente`), mai
  nell'identita TLS.
- Canale dietro astrazione `SdiChannel`:
  - `ManualExportChannel` (subito): XML scaricabile; le fatture cosi
    emesse sono gia legalmente emesse.
  - `SdICoopChannel` test: endpoint SOAP inbound sempre attivo esposto
    da Flow (SdI fa push, non polling), mutua TLS lato server; test di
    interoperabilita.
  - `SdICoopChannel` produzione: dopo Accordo di servizio e
    accreditamento (item pesante, non un passo finale minore).
- Conservazione a norma (obbligo art. 39 DPR 633/72, 10 anni):
  strategia = servizio AdE gratuito. `ConservationProvider =
  AdeFreeConservation`: Flow non conserva in proprio, traccia e guida
  l'adesione per-tenant nel cassetto fiscale; l'AdE conserva solo cio
  che passa da SdI; le fatture da `ManualExportChannel` in F7a sono
  marcate "fuori copertura AdE, a carico del tenant". Copertura
  effettiva da F7b.
- Firma: v1 B2B/B2C senza firma (via canale accreditato non richiesta).
  PA/B2G (CAdES/XAdES-BES + certificato qualificato) differita post-v1.
- Esiti: notifiche SdI in push sull'endpoint inbound, correlate per
  `IdentificativoSdI` (colonna indicizzata di prima classe). Ciclo
  attivo v1: RC, MC, NS, AT. NE/DT/EC/SE e ciclo passivo differiti con
  la PA.
- Profilo fiscale v1 minimo: TD01/TD04, aliquote standard + insieme
  Natura ridotto, ritenuta d'acconto opzionale, bollo come flag +
  export trimestrale manuale. Differiti: PA/split payment, reverse
  charge/autofattura TD16-TD19, clienti esteri, liquidazione
  trimestrale del bollo, cassa previdenziale avanzata.
- Immutabilita: macchina a stati; solo `draft` cancellabile; emessa =
  append-only; correzione solo via nota di credito TD04.
- Numerazione: progressiva per (Org, serie, anno), concorrenza-safe
  (sequence o `FOR UPDATE`), allocata solo alla transizione
  draft -> transmitted nella stessa transazione; mai riusata.
- Ricerca storico; marca pagata (riconciliazione manuale v1); nota di
  credito TD04.

## FR-10 MCP server (co-paritario)

Espone il dominio come tool MCP, **stesso service layer, RBAC,
isolamento (org, progetto)** della REST. Auth come utente in una Org +
progetto (token con scope), idempotenza sui tool mutanti, conflitti
optimistic = 409. Trasporto stdio (Claude locale) e HTTP/SSE (remoto).
L'isolamento memoria per progetto vale specialmente qui.

## FR-11 Multi-utente, auth, RBAC, concorrenza

Signup/login, JWT, Org multiple per utente, inviti, ruoli. RBAC nel
service layer. RLS obbligatoria su entita org-scoped (difesa primaria,
non opzionale). Optimistic concurrency: `UPDATE ... WHERE id AND
version`; 0 righe -> 409; niente lost-update silenzioso; activity log
append-only; invalidazione realtime via WebSocket. Modello idoneo a
collaborazione futura.

## FR-12 Notifiche, ricorrenze, reminder

Astrazione canale con adapter: Telegram e email in v1, altri
predisposti. Reminder su scadenze, task ricorrenti materializzati dal
worker (istanze = righe task indipendenti; in v1 ricorrenza e
dipendenze mutuamente esclusive), notifiche su eventi (assegnazione,
blocco, no-ubiquita, esito SDI). Preferenze per utente ed evento.

## FR-13 Assistente di pianificazione (advisory, core v1)

Capacita advisory di prima classe nel service layer, esposte via REST
+ tool MCP, con l'LLM/MCP come interfaccia in linguaggio naturale e il
nucleo decisionale **deterministico e spiegabile** (coerente con
ADR-0004). Tre archetipi:

- **Cosa-faccio-ora**: dato un intervallo libero (inizio, durata,
  luogo/contesto, opzionale energia), restituisce i task fattibili,
  ordinati. Fattibilita = entra nella finestra (`remaining_effort_h`
  vs durata), non bloccato da dipendenze, `executor=human` e l'utente
  disponibile (no conflitto con eventi/no-ubiquita, FR-4/ADR-0008),
  luogo/contesto compatibili. Ranking deterministico per
  urgenza/priorita/necessita/valore.
- **Errand/contesto**: dato un luogo o contesto (es. "vado al brico"),
  aggrega gli item rilevanti (task con `location`/contesto compatibile)
  tra i progetti accessibili all'utente entro la org.
- **Prioritizzazione entro budget**: dato un `budget` (envelope), una
  selezione vincolata (knapsack a priorita/valore-densita, must-have
  prima) dei task/voci con `monetary_cost` che massimizza il valore
  entro l'importo, con spiegazione.

Determinismo verificabile (stesso input, stesso output). Isolamento:
opera sui task accessibili all'utente entro una org (anche
multi-progetto); non e una violazione dell'isolamento memoria
(ADR-0007), che governa il contenuto RAG/email. Vedi
[ADR-0013](adr/0013-planning-advisory-layer.md).

## FR-14 Dominio personale e budget

- I task portano `monetary_cost?`, `location?`, `necessity`
  (must/should/nice) e contesto/precondizioni via tag `generic` con
  convenzione di namespace (es. `ctx:richiede-computer`, `place:brico`).
- Un progetto puo essere personale (project non fatturabile): stessa
  tassonomia (ADR-0003), nessuna entita parallela.
- `Budget`: envelope org-scoped per periodo (mese/trimestre/anno/
  custom) e categoria (es. spese di casa) con importo allocabile e
  valuta; i task vi si agganciano via `budget_id`; consumo vs residuo
  calcolato dal service layer.
- La selezione entro budget e deterministica (knapsack a priorita),
  non un giudizio opaco dell'LLM; l'LLM traduce la richiesta in query
  strutturata e narra il risultato. Vedi
  [ADR-0014](adr/0014-personal-domain-budgets.md).

## FR-15 Metering, crediti, billing (ADR-0019)

Billing a crediti (riuso pattern bitvision). Wallet per org + ledger
append-only idempotente + check-and-debit atomico (niente scoperto in
concorrenza). Rate card per modello (credits/token in-out, provider,
markup, is_active, tier). Basi di costo: locale = rate card; nostra
chiave = costo provider x markup; BYOK = nessun costo token, fee di
piattaforma configurabile (es. 0.0001 x unita), chiave utente cifrata
(ADR-0006). Metering: `usage_record` per operazione + debito sul
ledger. Storage misurato: DB e S3 a rate distinti configurabili
(GB-mese); allegati/documenti pesanti su S3, il DB tiene solo metadati
+ testo indicizzabile. Enforcement nel service layer (choke point come
RBAC): a crediti insufficienti le operazioni a costo (LLM, embedding,
advisory con LLM, scritture storage pesanti) sono rifiutate con codice
i18n; lettura, export GDPR e recupero documenti fiscali/conservati
restano disponibili. Admin (ruolo) aggiunge crediti, edita rate
card/percentuali/rate storage; azioni auditate. Gateway di pagamento
fuori v1 (v1 = grant manuale admin). Vedi
[ADR-0019](adr/0019-metering-credits-billing.md).
