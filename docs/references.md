# Riferimenti

## Esterni (verificati in revisione)

### Proton Mail Bridge headless / arm64

- https://github.com/shenxn/protonmail-bridge-docker
- https://hub.docker.com/r/shenxn/protonmail-bridge
- https://ndo.dev/posts/headless_protonbridge

### Sistema di Interscambio (SDI), accreditamento, conservazione

- Sperimentazione / ambiente di test:
  https://www.fatturapa.gov.it/it/sistemainterscambio/sperimentazione/
- Intermediari: https://www.fatturapa.gov.it/it/comefare/intermediari/
- Sistema di accreditamento:
  https://www.fatturapa.gov.it/it/SistemaAccreditamento/cose-il-sistema-di-accreditamento/
- Firmare la FatturaPA (firma richiesta solo verso PA):
  https://www.fatturapa.gov.it/it/comefare/operatori-economici/firmare-la-fatturapa/
- Conservazione (obbligo art. 39 DPR 633/72):
  https://www.agenziaentrate.gov.it/portale/aree-tematiche/fatturazione-elettronica/guida-fatturazione-elettronica/come-predisporre-inviare-ricevere-fe/come-si-conservano-fe
- Servizio di conservazione AdE (adesione richiesta):
  https://www.agenziaentrate.gov.it/portale/aree-tematiche/fatturazione-elettronica/guida-fatturazione-elettronica/i-servizi-dell-agenzia-fe/servizio-conservazione-elettronica
- Guida compilazione FE ed esterometro:
  https://www.agenziaentrate.gov.it/portale/documents/d/guest/guida_compilazione-fe-esterometro-v1-10_aprile_2025
- AgID, Linee Guida sul documento informatico:
  https://www.agid.gov.it/sites/agid/files/2024-05/linee_guida_sul_documento_informatico.pdf

### pgvector, retrieval ibrido

- pgvector 0.8 (iterative scan, filtering, partizione, CONCURRENTLY):
  https://github.com/pgvector/pgvector
- RRF, Azure AI Search:
  https://learn.microsoft.com/en-us/azure/search/hybrid-search-ranking
- RRF, OpenSearch:
  https://opensearch.org/blog/introducing-reciprocal-rank-fusion-hybrid-search/
- Pattern RAG (hybrid/graph/agentic/corrective/multimodal),
  riferimento: https://github.com/honestsoul/rag_patterns
  (vedi ADR-0016 per le nostre adozioni e deviazioni: niente ramo web
  nel grader perche la memoria e privata; GraphRAG strutturale non
  testuale; multimodale differito)
- STT locale per note vocali (ADR-0020): famiglia Whisper /
  faster-whisper / whisper.cpp (multilingue IT+EN, CPU/ARM
  small/distil -> GPU/large o API); scelta concreta in implementazione
- TTS locale per risposte vocali (ADR-0020, in v1): es. Piper /
  Coqui-XTTS o altri open multilingue IT+EN, locale default -> API;
  scelta concreta in implementazione

## Interni: pattern di astrazione LLM/Embedding da riusare

Progetto `bitvision_phoenix` (path locale
`/Users/angelo/data/WORK/bitvision/bitvision_phoenix`). Pattern:
provider via `typing.Protocol` (non ABC), factory DB-driven, DTO
neutri, settings pydantic con chiavi provider via env, registry modelli
a DB con `is_active`.

- `backend/src/bvphoenix/services/llm.py` (Protocol `LLMProvider` +
  impl + prompt caching)
- `backend/src/bvphoenix/services/llm_types.py` (DTO neutri)
- `backend/src/bvphoenix/services/llm_openai.py` (adapter SDK template)
- `backend/src/bvphoenix/services/llm_factory.py` (factory DB-driven)
- `backend/src/bvphoenix/config.py` (settings provider + chiavi env)
- `backend/src/bvphoenix/db/models/llm_rate_cards.py` (registry modelli)
- `backend/src/bvphoenix/db/models/embeddings.py` +
  `workers/src/bvworkers/tasks/embed_series.py` (versioning `model_id`,
  load lazy)
- `backend/src/bvphoenix/services/billing.py`, `services/llm_cost.py`,
  `services/embedding_cost.py`, `services/ai_tiers.py`,
  `db/models/llm_rate_cards.py` (wallet/debiti idempotenti, rate card,
  tier; riuso per ADR-0019)

Nota: bitvision NON ha un'astrazione di embedding (chiamate dirette).
Flow aggiunge `EmbedderProvider` speculare a `LLMProvider`. Da non
copiare: cache `ephemeral` Anthropic-specifica, template clinici,
gestione DICOM, scoping token MCP medicale.
