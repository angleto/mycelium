# Requisiti non funzionali

## Sicurezza

- Cifratura at-rest = cifratura del **volume** Postgres + object storage
  (LUKS / block storage cifrato / TDE managed): body, tsvector ed
  embedding restano indicizzabili. L'envelope app-level
  (libsodium/Fernet) e riservato ai **segreti opachi non indicizzati**:
  token OAuth, credenziali, materiale del canale SdI. Modello di
  minaccia dichiarato: protegge da disco/snapshot rubato, non da
  connessione DB viva. Vedi
  [ADR-0006](adr/0006-at-rest-volume-encryption.md).
- Certificato del canale SdI e (post-v1, se PA) certificato qualificato
  con custodia dedicata (HSM o firma remota).
- L'endpoint SOAP inbound SdI sempre attivo e con mutua TLS e una nuova
  superficie d'attacco e un impegno di disponibilita: va trattato come
  tale (non come un worker poll).
- RBAC nel service layer; rate limiting su SMTP e chiamate esterne;
  audit log append-only delle azioni sensibili (invio fattura, invio
  email, modifica workflow, cambio canale SdI, accesso cross-progetto
  alla memoria).

## Privacy e GDPR

- Isolamento per (org, progetto); provenienza esplicita e propagazione
  della cancellazione (erasure) a embedding, summary, object storage e
  blob consolidati; nessun merge cross-soggetto.
- Embedding locale: il corpo email non lascia il perimetro.
  Summarization/consolidamento via LLM esterno solo con opt-in per-Org
  esplicito e auditato; tracciamento di cio che esce dal perimetro.

## Isolamento multi-tenant

RLS obbligatoria su tutte le entita org-scoped. Memoria partizionata
per `org_id` con predicato (org, progetto) obbligatorio in ogni query.
Test esplicito: una ricerca senza filtro non deve mai restituire dati
di un altro tenant o di un altro progetto.

## Performance e nodo ARM

CPM deterministico O(V+E) (millisecondi su centinaia di task). HNSW
per-partizione con budget RAM dichiarato. Re-embedding pesante off-peak
o su nodo transitorio piu grande; `halfvec`/quantizzazione se serve.

## Affidabilita

Sync IMAP e callback SDI idempotenti, retry con backoff, isolamento dei
guasti per account/canale.

## Cloud, K8s-ready

Servizi stateless, immagini arm64, config/secret esternalizzati
(12-factor), object storage per allegati/blob, health/readiness probe,
worker scalabili. Deploy v1 a nodo singolo (Docker Compose) portabile a
K8s. Eccezioni stateful note: Postgres, sidecar Proton Bridge, endpoint
SOAP inbound SdI.

## Estensibilita predisposta

API-first per mobile futuro; astrazione canali di notifica; versioning
+ event log per collaborazione futura; `Embedder`, `LLMProvider`,
`SdiChannel`, `ConservationProvider` pluggable; namespace memoria
generico.

## Osservabilita

Logging strutturato, health endpoint, metriche sui job (sync email,
scheduler, memoria/re-embedding, ricevute SdI).

## Testing

Unit di dominio (4 disuguaglianze di dipendenza su calendario con
festivita, serializzazione per-persona, no-ubiquita, RBAC, macchina a
stati, RRF, erasure GDPR, numerazione concorrente,
fattibilita/ranking advisory e selezione knapsack entro budget,
determinismo dell'advisory a parita di input), integration API,
test dei tool MCP, fixture multi-tenant e multi-progetto, golden XML
FatturaPA per piu casi, contract test del canale SdICoop contro
l'ambiente di test SdI.
