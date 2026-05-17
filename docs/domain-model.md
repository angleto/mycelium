# Modello di dominio

Entita concettuali. Lo schema fisico e in [data-model.md](data-model.md).

- **Organization**: confine di tenancy. Possiede un **profilo fiscale
  emittente** (RegimeFiscale RF01.., P.IVA/CF, sede strutturata, REA,
  cassa) necessario alla fatturazione. Tutto e org-scoped.
- **User / Membership / Role**: utenti, appartenenza a una o piu Org,
  RBAC (owner, admin, member, guest read-only).
- **Tag** con `kind` in {generic, client, project}. Un solo concetto di
  etichetta. I dati legali/fiscali e di billing non stanno in JSONB
  libero ma in profili tipizzati satellite con FK a `tag.id`:
  - `client_profile`: ragione sociale, IdFiscaleIVA (paese + id),
    codice fiscale, sede strutturata, codice destinatario o PEC.
  - `project_profile`: riferimento al tag client (parent), tariffa,
    valuta, budget, eventuale workflow override.
  Associare cliente/progetto a un task = attaccare il tag relativo
  (stessa relazione many-to-many di ogni tag).
- **Task**: unita primaria. Stato (dalla workflow), priorita,
  `estimate_effort_h`, `remaining_effort_h`, `actual_start`,
  `is_milestone`, **`executor`** (utente umano oppure agente LLM),
  sottotask, tag, commenti, allegati. Attributi di pianificazione
  personale: `monetary_cost?`, `location?`, `necessity`
  (must/should/nice), contesto/precondizioni via tag generic (es.
  `ctx:richiede-computer`, `place:brico`), `budget_id?`. Un task puo
  appartenere a un progetto personale (project non fatturabile) oltre
  che a progetti cliente.
- **TaskDependency**: arco diretto tipizzato (FS/SS/FF/SF) con `lag`
  (minuti lavorativi con segno; calendario di riferimento =
  predecessore). L'insieme e un DAG; il rilevamento cicli e
  obbligatorio.
- **Event (appuntamento)**: org-scoped, tag client/project,
  partecipanti, intervallo time-pinned, luogo. Vincolo: nessuna
  sovrapposizione per partecipante (no-ubiquita).
- **WorkflowDefinition**: stati ordinati e transizioni; default per
  Org; override agganciabile a un `project_profile`.
- **WorkingCalendar**: ore settimanali, festivita, timezone; default
  Org + override per utente (capacita giornaliera).
- **TimeEntry**: da timer o manuale; billable; snapshot della tariffa;
  alimenta `remaining_effort` e le fatture.
- **EmailAccount / EmailMessage**: connector Gmail OAuth2, Proton
  Bridge, IMAP generico; messaggi per triage con link al Task creato.
- **MemoryBlob + BlobSource**: memoria gerarchica (tier hot/warm/cold)
  con provenienza N:1 esplicita per la cancellazione GDPR; scope
  (org, progetto).
- **Invoice**: macchina a stati (draft, issued/transmitted, terminale
  SdI); immutabile dopo emissione; correzione solo via nota di credito
  TD04.
- **SdiMandate**: autorizzazione per-Org a trasmettere per suo conto
  (scope, validita, revoca, audit).
- **ConservationRecord**: stato della conservazione a norma per fattura
  e per ricevuta SdI (modello "servizio AdE gratuito").
- **Budget**: envelope di spesa org-scoped per periodo e categoria
  (es. spese di casa) con importo allocabile; i task con
  `monetary_cost` lo consumano.
- **PlanningQuery (capacita advisory, non entita persistente)**:
  capacita del service layer = filtro di fattibilita + ranking +
  selezione vincolata (knapsack a priorita) su task accessibili
  all'utente entro una org. LLM/MCP come frontend in linguaggio
  naturale. Non viola l'isolamento memoria (ADR-0007), che governa il
  contenuto RAG/email, non la lista task dell'utente.
- **Notification / NotificationPref**: messaggio su canale
  (Telegram/email) e preferenze per utente ed evento.
- **Comment / Attachment / ActivityLog**: collaborazione e audit
  (log append-only).
