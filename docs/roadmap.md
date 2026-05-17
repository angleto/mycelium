# Roadmap a fasi e criteri di verifica

Ogni fase di dominio espone subito REST + tool MCP (l'MCP non e una
fase finale a se). Il repo parte vuoto: i criteri sono end-to-end per
fase.

## Fasi

- **F0 Fondamenta**: scaffold monorepo, CI, Docker Compose arm64,
  Postgres+pgvector con **RLS obbligatoria e partizione memoria per
  org**, Alembic, auth/JWT, Org + profilo fiscale / User / Membership /
  RBAC, **optimistic concurrency** + activity log append-only,
  scheletro `api`/`mcp`/`web`/`worker`/`sdi-inbound`.
- **F1 Task + tassonomia**: Task/sottotask/commenti, tag con kind +
  profili satellite client/project, `executor`, viste lista/board,
  ricerca/filtri.
- **F2 Workflow + dipendenze + grafo**: WorkflowDefinition + override
  progetto, 4 tipi di dipendenza + lag, rilevamento cicli, grafo DAG.
- **F3 Scheduler deterministico**: CPM logico + serializzazione
  per-persona + actuals + pin; Events + no-ubiquita; Gantt con
  percorso critico logico e drag.
- **F4 Time tracking**: timer realtime (un solo timer attivo per
  utente, garantito da indice unico parziale), voci manuali, report
  per progetto/cliente/generico/utente/task con totali billable e
  snapshot tariffa, export CSV; alimenta F3 via `actual_start`.
  Export PDF = follow-up sottile di sola presentazione (il CSV
  soddisfa il requisito di export dati), non un blocker.
- **F4b Dominio personale, budget, assistente advisory**: attributi
  task (costo, luogo, contesto, necessita), `budgets` envelope,
  capacita advisory (cosa-faccio-ora / errand bundling /
  prioritizzazione entro budget) nel service layer esposte via
  REST + MCP; nucleo deterministico, LLM/MCP frontend.
- **F5 Email**: connector Gmail OAuth2 + IMAP generico, sync, triage,
  email-to-task, invio SMTP (Proton Bridge a seguire).
- **F5b Billing & metering core**: wallet + credit_ledger
  (append-only, idempotente, check-and-debit atomico) + rate card
  modelli + storage rates (DB vs S3) + enforcement nel service layer +
  admin grant/rate (ADR-0019). Precede le fasi a costo (F6); hook di
  metering aggiunti con ogni subsistema misurato.
- **F6 Memoria**: tiering per frequenza/recency/importanza (ADR-0016,
  invariante: il cold resta sempre recuperabile), summarization,
  `Embedder` pluggable + pgvector, retrieval ibrido RRF entro
  (org, progetto), **grader correttivo** (no ramo web), retrieval
  esposto come tool MCP agentico, consolidamento con provenienza, job
  di re-embedding, erasure GDPR. Multimodale e GraphRAG testuale
  esplicitamente fuori (differiti).
- **F6b Note vocali e cattura conversazionale**: entita `Note`,
  cattura PWA offline-first + upload S3 (non metered), pipeline worker
  STT (`TranscriptionProvider` locale, ADR-0012/0019) -> transcript,
  LLM opzionale (titolo/summary/action item -> Task), embedding in
  memoria (ADR-0016); conversazione testo/voce con risposta LLM
  (online dal vivo, offline differita + notifica FR-12); ADR-0020;
  comandi NL canonici deterministici + fallback LLM (ADR-0021); TTS
  voce-out in v1 (`TtsProvider`, metered). Dopo F5b/F6.
- **F7a Fatture B2B/B2C**: XML FatturaPA + validazione,
  `ManualExportChannel`, immutabilita, numerazione concorrenza-safe,
  ricerca, marca pagata, nota di credito TD04; tracciamento adesione
  conservazione AdE + marcatura fatture fuori copertura.
- **F7b SdICoop test**: `SdICoopChannel` su ambiente di test + endpoint
  SOAP inbound + parsing ricevute (RC/MC/NS/AT) + test di
  interoperabilita; da qui conservazione AdE effettiva.
- **F7c SdICoop produzione**: Accordo di servizio + accreditamento +
  switch del canale in produzione (item pesante, risorsato come tale).
- **Post-v1**: PA/B2G (firma CAdES/XAdES + certificato qualificato,
  NE/DT/EC/SE), ciclo passivo, reverse charge/autofattura TD16-TD19,
  clienti esteri, liquidazione trimestrale del bollo, leveling
  ottimizzante CP-SAT, Proton Drive `ArchiveBackupTarget` (sidecar
  rclone, poi SDK ufficiale Proton; ADR-0018), app companion nativa
  per hands-free (pulsante cuffie / assistente OS) e cattura
  always-on (ADR-0022).
- **F8 Notifiche + ricorrenze + rifiniture**: Telegram + email,
  reminder, task ricorrenti, hardening sicurezza/privacy, audit,
  `ArchiveBackupTarget` con backend object storage S3 EU (doppia copia
  DB + esterno, async/idempotente; ADR-0018; distinto dalla
  conservazione legale AdE, ADR-0010).

## Criteri di verifica end-to-end

- **F0**: `docker compose up` (arm64) avvia tutto; signup/login; Org A
  non vede dati Org B; progetto P1 non vede memoria di P2 (RLS +
  partizione + predicato); scrittura concorrente stale -> 409.
- **F1**: stesso task creato da GUI/REST/MCP -> stato identico; tag
  client+project su un task e filtro coerente.
- **F2**: arco che crea ciclo rifiutato; override workflow di progetto
  impone lo stato extra; il grafo renderizza il DAG.
- **F3**: due task umani stesso assegnatario senza dipendenza non si
  sovrappongono; un task delegato a LLM puo essere parallelo;
  appuntamento sovrapposto per la stessa persona rifiutato; SS con
  lag +2 giorni lavorativi a cavallo di una festivita -> date esatte
  attese; task in corso con 12h loggate vs stima 4h -> regola del
  residuo + ES pinnato; drag che sopravvive a un ricalcolo per modifica
  non correlata; stesso input -> schedule identico.
- **F4**: timer da MCP visibile in GUI in realtime; report per
  cliente/progetto con totali attesi; export apribile.
- **F4b**: data una finestra libera (durata + luogo) l'assistente
  propone solo task fattibili e non in conflitto no-ubiquita, con
  ranking deterministico e spiegabile; "cosa mi serve al brico"
  aggrega gli item per luogo/contesto entro la org dell'utente; dato
  un budget la selezione entro envelope e knapsack-corretta e
  spiegabile (must-have prima); stesso input -> stesso risultato.
- **F5**: account Gmail (OAuth2) configurato; email-to-task con
  tag/client/project e link sorgente; reply SMTP recapitata.
- **F5b**: a crediti zero un'operazione LLM/embedding e rifiutata con
  codice i18n; lettura/export/dati fiscali restano accessibili; debiti
  idempotenti (retry non raddoppia); nessuno scoperto sotto
  concorrenza; grant admin accreditato.
- **F6**: ricerca ibrida (RRF) entro (org, progetto) recupera un thread
  vecchio demosso a cold; query con token raro trovata dal ramo
  lessicale; ricerca senza filtro non perde dati cross-progetto/org;
  cancellazione di un messaggio propaga a embedding/summary/
  object-storage/blob consolidati; cambio modello -> re-embedding senza
  write-downtime.
- **F6b**: nota vocale registrata offline e in coda a crediti zero
  (cattura non bloccata); alla sync STT locale produce il transcript
  che entra in memoria entro (org, progetto); domanda posta offline ->
  risposta LLM differita accodata alla Note + notifica; erasure di una
  nota a cascata su audio S3 + transcript + blob memoria + task;
  online la risposta LLM e anche vocale (TTS).
- **F7a**: fattura B2B/B2C da time entry billable, XML valido a schema,
  export manuale scaricabile; immutabile dopo emissione; numerazione
  concorrente senza duplicati/buchi; TD04 collegata; stato adesione
  conservazione AdE tracciato e fatture fuori copertura marcate.
- **F7b**: notifica SdI in push sull'endpoint inbound, correlata per
  `IdentificativoSdI`; RC/MC/NS/AT parsate; conservazione AdE
  effettiva.
- **F7c**: canale di produzione accreditato; fattura reale consegnata
  (RC).
- **F8**: reminder ed esito SDI recapitati su Telegram ed email; task
  ricorrente materializzato dal worker.
- **Test automatici**: come da [requisiti non funzionali, sezione
  Testing](non-functional-requirements.md).
