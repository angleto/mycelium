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

## LLM / Embedding provider abstraction (pattern adopted)

A pluggable AI-provider pattern Mycelium adopts (see ADR-0019):

- Provider as a `typing.Protocol` (structural typing, not an ABC), so
  adapters need no shared base class.
- DB-driven factory: the active provider/model is chosen from a
  database registry row (an `is_active` flag on a model registry), not
  hardcoded.
- Neutral DTOs for requests/responses, decoupled from any vendor SDK
  shape; one SDK adapter per provider implementing the Protocol.
- Pydantic settings carrying per-provider credentials from environment
  variables.
- A model / rate-card registry table driving cost: wallet-style
  idempotent debits, per-model rate cards, and usage tiers (reused for
  ADR-0019).
- Embedding models versioned by a stable `model_id` with lazy loading
  of the weights.

Mycelium adds an `EmbedderProvider` mirroring the `LLMProvider` Protocol:
an embedding abstraction is not implied by the LLM one and must be
designed explicitly (the source pattern called embedding models
directly). Deliberately out of scope: vendor-specific prompt-cache
mechanisms, any domain-specific prompt templates or data handling, and
bespoke MCP token scoping; design those for Mycelium's own needs rather
than porting them.
