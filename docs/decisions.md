# Decisioni

Decisioni di prodotto e architettura, consolidate e bloccate. Il
*perche* e le alternative scartate sono negli [ADR](adr/README.md).

## Tabella decisioni

| # | Tema | Decisione |
|---|---|---|
| 1/A | Fatturazione SDI | v1 solo B2B/B2C (PA differita). Canale unico condiviso; Flow trasmittente/intermediario sotto mandato per-Org; identita tenant nel payload FatturaPA; conservazione = servizio AdE gratuito (adesione per-tenant); introdotta per fasi |
| 2/B | Auth email | Gmail OAuth2; Proton via sidecar Bridge; IMAP/SMTP generico |
| 3 | Scheduling | CPM logico deterministico + serializzazione per-persona dei task umani non delegati attorno agli appuntamenti; non RCPSP generico |
| 4 | Workflow stati | Configurabili per Org, override per progetto |
| 5 | Dipendenze | 4 tipi (FS/SS/FF/SF) + lag/lead in tempo-calendario |
| 6 | Concorrenza | Optimistic concurrency via `version` (conflitto = 409) + activity log append-only; niente last-write-wins |
| 7 | Notifiche | Telegram + email; canali aggiuntivi predisposti |
| 8 | Migrazione | Nessuna |
| 9 | Memoria | Gerarchica su pgvector; isolamento duro per (org, progetto) |
| 10 | Mobile | Web responsive ora; API-first per mobile futuro |
| 11 | Hosting | Cloud, PostgreSQL, nodo ARM; design K8s-ready |
| 12 | Nome | "Flow" |
| 13 | Tag | Un solo concetto `tag` con `kind`; client/project con profili tipizzati satellite (FK a `tag.id`), non JSONB libero |
| C | Embedding | Locale, astrazione `Embedder` pluggable, job di re-embedding |
| D | Retrieval | Hybrid lessicale + semantico baseline, fusione RRF (k circa 60), scope (org, progetto) |
| E | Astrazione LLM/Embedding | Riusa il pattern di bitvision_phoenix; Flow aggiunge `EmbedderProvider` |
| F | Isolamento memoria | Confine duro per (org, progetto): RLS obbligatoria + partizione + predicato; mai per sola rilevanza |
| G | No-ubiquita | Entita `events`; appuntamenti della stessa persona non si sovrappongono (rifiuto) |
| H | Esecutore | `task.executor` = utente umano (seriale) o agente LLM (parallelo, esente dalla timeline umana) |
| I | Assistente pianificazione | Layer advisory core v1 sopra lo scheduler: nucleo deterministico (fattibilita + ranking + selezione vincolata), LLM/MCP come frontend in linguaggio naturale |
| J | Dominio personale + budget | Modellati in v1: task con costo/luogo/contesto/necessita; budget envelope per periodo/categoria; selezione deterministica entro budget (knapsack a priorita) |
| K | Memoria avanzata | Tiering per frequenza/recency/importanza (cold sempre recuperabile, raro != non importante); grader correttivo senza ramo web; retrieval come tool MCP agentico; grafo strutturale non testuale; multimodale differito (ADR-0016) |

## Decisioni di scope risolte

1. SDI v1 = solo B2B/B2C. PA/B2G, firma, notifiche NE/DT/EC/SE e ciclo
   passivo sono differiti post-v1.
2. Conservazione = servizio AdE gratuito con adesione per-tenant. Flow
   traccia e guida l'adesione; copertura effettiva da quando le fatture
   transitano da SdI; le fatture da export manuale iniziale sono fuori
   copertura, a carico del tenant.
3. MVP stratificato accettato: tutto il resto completo da subito, SDI a
   profilo fiscale minimo ed estesa per fasi.
4. Assistente di pianificazione advisory = core v1 (nucleo
   deterministico, LLM/MCP frontend); dominio personale e budget
   modellati in v1.

Decisi su indicazione esplicita dell'utente: isolamento memoria duro
per (org, progetto); no-ubiquita; esecutore umano seriale vs LLM
parallelo.
