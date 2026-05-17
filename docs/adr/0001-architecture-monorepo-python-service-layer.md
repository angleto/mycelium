# ADR-0001 Architettura: monorepo Python, service layer unico

Status: accettata.

## Contesto

Servono GUI e MCP co-paritari (stessa logica, due client) piu worker e
un servizio SOAP inbound SdI. Il rischio e duplicare la logica di
dominio tra REST e MCP e farli divergere.

## Decisione

Monorepo Python. `core/` contiene dominio + service layer con TUTTA la
business logic, l'enforcement RBAC, la macchina a stati, lo scheduler,
il motore memoria, la generazione/validazione XML SDI. `api/`
(FastAPI REST + WebSocket) e `mcp/` (SDK Python) sono adapter sottili
sullo stesso service layer. Frontend React/TS con tipi generati da
OpenAPI. `worker/` per i job, `sdi-inbound/` per le notifiche SdI in
push.

## Conseguenze

- Un solo choke point per RBAC, isolamento, optimistic concurrency,
  macchina a stati: GUI, REST e MCP non possono divergere.
- Ogni fase di dominio espone subito REST + tool MCP.
- Disciplina necessaria: vietato mettere logica in `api/` o `mcp/`.

## Alternative scartate

- Servizi separati per REST e MCP: duplicazione di logica e drift,
  esattamente il rischio da evitare.
- Backend non-Python: l'SDK MCP di riferimento e Python e il pattern di
  astrazione LLM/Embedding riusato (ADR-0012) e Python.
