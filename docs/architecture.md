# Architettura

Monorepo Python + frontend TypeScript.

## Componenti

- `core/`: dominio + **service layer unico** (business logic, RBAC,
  macchina a stati, scheduler, motore di pianificazione advisory
  (fattibilita + ranking + selezione vincolata), budget, motore
  memoria, generazione/validazione XML SDI, mandato e conservazione).
  Unico punto di verita.
- `api/`: FastAPI, REST + WebSocket. Adapter sottile sul service layer.
- `mcp/`: MCP server (SDK Python). Adapter sottile, pari ad `api/`.
- `web/`: SPA React/TS (liste/board/calendario, grafo, Gantt, triage
  email, fatture, report). Tipi generati da OpenAPI.
- `worker/`: job (sync IMAP, scheduler, memoria/promozione/
  re-embedding, ricorrenze, reminder).
- `sdi-inbound/`: servizio SOAP sempre attivo con mutua TLS per le
  notifiche SdI in push (non e un worker poll).
- `db`: PostgreSQL con `pgvector`. SQLAlchemy + Alembic. `org_id`
  ovunque, RLS obbligatoria, memoria partizionata per `org_id`.
- `cache/broker`: Redis (coda dei job, pub/sub per WebSocket).
- connectors: Gmail OAuth2; sidecar Proton Bridge (arm64); IMAP
  generico; `SdiChannel`; `ConservationProvider`;
  `Embedder`/`LLMProvider` (pattern bitvision_phoenix).

## Diagramma

```
 Claude → mcp/ ─┐
                ├─► core/ (service, RBAC, scheduler, memoria, SDI) ─► PG+pgvector (part. per org, RLS)
Browser → web/ ─┤          ▲                                    ▲
      REST/WS   │          │                                    │
              api/ ────────┘                                    │
                │                                               │
            worker/ ── IMAP · scheduler · memoria/re-embed · ricorrenze · reminder
                │
   sdi-inbound/ (SOAP mutua-TLS, push notifiche SdI)
   connectors: Gmail · Proton Bridge · SdiChannel · Conservation · Embedder
```

## Principi architetturali

- `api/` e `mcp/` non contengono logica di business: sono due adapter
  sottili sullo stesso `core/`. GUI e MCP restano realmente
  co-paritari e non divergono.
- L'enforcement di RBAC, macchina a stati, isolamento (org, progetto) e
  optimistic concurrency e nel service layer: e l'unico choke point
  attraversato da GUI, REST e MCP.
- Astrazioni pluggable (`SdiChannel`, `ConservationProvider`,
  `Embedder`, `LLMProvider`) con factory DB-driven e DTO neutri,
  riusando il pattern di bitvision_phoenix (vedi
  [ADR-0012](adr/0012-llm-embedder-abstraction.md) e
  [references.md](references.md)).
- Deploy v1: Docker Compose su nodo ARM cloud; design K8s-ready.

## Layout monorepo (indicativo)

```
flow/
  core/        # dominio + service layer (pacchetto Python)
  api/         # FastAPI REST + WebSocket
  mcp/         # MCP server
  sdi-inbound/ # servizio SOAP inbound SdI
  worker/      # job in background
  web/         # SPA React/TS
  deploy/      # Docker Compose, config, migrazioni
  docs/        # questa documentazione
```
