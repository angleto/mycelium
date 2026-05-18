# References

## External (verified in review)

### Proton Mail Bridge headless / arm64

- https://github.com/shenxn/protonmail-bridge-docker
- https://hub.docker.com/r/shenxn/protonmail-bridge
- https://ndo.dev/posts/headless_protonbridge

### Sistema di Interscambio (SDI), accreditation, conservation

- Trial / test environment:
  https://www.fatturapa.gov.it/it/sistemainterscambio/sperimentazione/
- Intermediaries: https://www.fatturapa.gov.it/it/comefare/intermediari/
- Accreditation system:
  https://www.fatturapa.gov.it/it/SistemaAccreditamento/cose-il-sistema-di-accreditamento/
- Signing the FatturaPA (signature required only towards PA):
  https://www.fatturapa.gov.it/it/comefare/operatori-economici/firmare-la-fatturapa/
- Conservation (obligation art. 39 DPR 633/72):
  https://www.agenziaentrate.gov.it/portale/aree-tematiche/fatturazione-elettronica/guida-fatturazione-elettronica/come-predisporre-inviare-ricevere-fe/come-si-conservano-fe
- AdE conservation service (adhesion required):
  https://www.agenziaentrate.gov.it/portale/aree-tematiche/fatturazione-elettronica/guida-fatturazione-elettronica/i-servizi-dell-agenzia-fe/servizio-conservazione-elettronica
- FE and esterometro compilation guide:
  https://www.agenziaentrate.gov.it/portale/documents/d/guest/guida_compilazione-fe-esterometro-v1-10_aprile_2025
- AgID, Guidelines on the electronic document:
  https://www.agid.gov.it/sites/agid/files/2024-05/linee_guida_sul_documento_informatico.pdf

### pgvector, hybrid retrieval

- pgvector 0.8 (iterative scan, filtering, partitioning, CONCURRENTLY):
  https://github.com/pgvector/pgvector
- RRF, Azure AI Search:
  https://learn.microsoft.com/en-us/azure/search/hybrid-search-ranking
- RRF, OpenSearch:
  https://opensearch.org/blog/introducing-reciprocal-rank-fusion-hybrid-search/
- RAG patterns (hybrid/graph/agentic/corrective/multimodal),
  reference: https://github.com/honestsoul/rag_patterns
  (see ADR-0016 for our adoptions and deviations: no web branch in the
  grader because memory is private; structural, non-textual GraphRAG;
  multimodal deferred)
- Local STT for voice notes (ADR-0020): the Whisper /
  faster-whisper / whisper.cpp family (multilingual IT+EN, CPU/ARM
  small/distil -> GPU/large or API); concrete choice at implementation
- Local TTS for voice replies (ADR-0020, in v1): e.g. Piper /
  Coqui-XTTS or other open multilingual IT+EN, local default -> API;
  concrete choice at implementation

## Internal: LLM/Embedding abstraction pattern to reuse

The `bitvision_phoenix` project (local path
`/Users/angelo/data/WORK/bitvision/bitvision_phoenix`). Pattern: a
provider via `typing.Protocol` (not an ABC), a DB-driven factory,
neutral DTOs, pydantic settings with provider keys from env, a DB model
registry with `is_active`.

- `backend/src/bvphoenix/services/llm.py` (Protocol `LLMProvider` +
  impl + prompt caching)
- `backend/src/bvphoenix/services/llm_types.py` (neutral DTOs)
- `backend/src/bvphoenix/services/llm_openai.py` (SDK adapter template)
- `backend/src/bvphoenix/services/llm_factory.py` (DB-driven factory)
- `backend/src/bvphoenix/config.py` (provider settings + env keys)
- `backend/src/bvphoenix/db/models/llm_rate_cards.py` (model registry)
- `backend/src/bvphoenix/db/models/embeddings.py` +
  `workers/src/bvworkers/tasks/embed_series.py` (`model_id` versioning,
  lazy load)
- `backend/src/bvphoenix/services/billing.py`, `services/llm_cost.py`,
  `services/embedding_cost.py`, `services/ai_tiers.py`,
  `db/models/llm_rate_cards.py` (wallet/idempotent debits, rate cards,
  tiers; reused for ADR-0019)

Note: bitvision has NO embedding abstraction (direct calls). Flow adds
an `EmbedderProvider` mirroring `LLMProvider`. Do not copy: the
Anthropic-specific `ephemeral` cache, clinical templates, DICOM
handling, medical MCP token scoping.
