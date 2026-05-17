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
- **F4 Time tracking**: timer realtime, voci manuali, report, export;
  alimenta F3.
- **F4b Dominio personale, budget, assistente advisory**: attributi
  task (costo, luogo, contesto, necessita), `budgets` envelope,
  capacita advisory (cosa-faccio-ora / errand bundling /
  prioritizzazione entro budget) nel service layer esposte via
  REST + MCP; nucleo deterministico, LLM/MCP frontend.
- **F5 Email**: connector Gmail OAuth2 + IMAP generico, sync, triage,
  email-to-task, invio SMTP (Proton Bridge a seguire).
- **F6 Memoria**: tiering, summarization, `Embedder` pluggable +
  pgvector, retrieval ibrido RRF entro (org, progetto), consolidamento
  con provenienza, job di re-embedding, erasure GDPR.
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
  ottimizzante CP-SAT.
- **F8 Notifiche + ricorrenze + rifiniture**: Telegram + email,
  reminder, task ricorrenti, hardening sicurezza/privacy, audit.

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
- **F6**: ricerca ibrida (RRF) entro (org, progetto) recupera un thread
  vecchio demosso a cold; query con token raro trovata dal ramo
  lessicale; ricerca senza filtro non perde dati cross-progetto/org;
  cancellazione di un messaggio propaga a embedding/summary/
  object-storage/blob consolidati; cambio modello -> re-embedding senza
  write-downtime.
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
