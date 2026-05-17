# ADR-0012 Astrazione LLM/Embedder, riuso pattern bitvision

Status: accettata.

## Contesto

Servono summarization e embedding pluggable: modello locale di default
(privacy, il corpo non lascia il perimetro) ma sostituibile (es. modello
SOTA su cluster GPU in futuro). Esiste gia un pattern collaudato
dell'utente in `bitvision_phoenix`.

## Decisione

Riusare il pattern di `bitvision_phoenix`: provider via
`typing.Protocol` (non ABC), factory DB-driven, DTO neutri, settings
pydantic con chiavi provider via env, registry modelli a DB con
`is_active`. File di riferimento in
[references.md](../references.md). bitvision NON ha un'astrazione di
embedding (chiamate dirette): Flow aggiunge `EmbedderProvider`
speculare a `LLMProvider` (`embed_text`, `dim`, `model_id`). Default:
modello multilingue piccolo CPU/ARM; scelta concreta in implementazione
tra candidati open source forti (BGE-M3, multilingual-E5,
GTE-multilingual, Qwen3-Embedding) su MTEB multilingue corrente, non un
"migliore" fissato a priori. `model_id`+`dim` per blob abilitano il
re-embedding (ADR-0005).

## Conseguenze

- Coerenza con un pattern gia validato; sostituzione del modello senza
  riprogettare il core.
- Da non copiare da bitvision: cache `ephemeral` Anthropic-specifica,
  template clinici, gestione DICOM, scoping token MCP medicale.

## Alternative scartate

- Astrazione nuova ad hoc: reinventa un pattern gia funzionante
  dell'utente.
- Modello cloud-only: il corpo email lascerebbe il perimetro,
  incompatibile con la postura privacy (default locale).
