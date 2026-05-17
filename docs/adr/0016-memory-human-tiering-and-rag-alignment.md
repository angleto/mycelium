# ADR-0016 Memoria: tiering tipo memoria umana e allineamento ai pattern RAG

Status: accettata. Raffina ADR-0005/0007. Origine: verifica contro
`docs/rag-architectures.txt` + richiesta utente di memoria gerarchica
come quella umana.

## Contesto

ADR-0005 definiva tiering hot/warm/cold per eta/dimensione. Due input:
(1) i 5 pattern RAG (hybrid, GraphRAG, agentic, corrective, multimodal);
(2) l'idea: la memoria deve essere gerarchica come quella umana, i
concetti che ricorrono spesso in un tier veloce, quelli rari in tier
meno performanti ma sempre recuperabili perche potrebbero essere
importanti.

## Decisione

### Tiering per frequenza/recency/importanza (memoria umana)

- Il tier (hot/warm/cold) e guidato da uno **score di accesso** con
  decay temporale (frequenza + recency) e da un segnale di importanza,
  non piu solo da eta/dimensione.
- **Invariante**: la frequenza determina solo il **tier di latenza**,
  mai la ritenzione ne la visibilita. Il cold resta sempre
  interrogabile; un concetto raro ma rilevante riemerge via retrieval
  ibrido + grader. Frequenza bassa != non importante.
- I **concetti ricorrenti** (cluster consolidati, provenienza
  preservata via `blob_sources`, sempre entro (org, progetto) e mai
  cross-soggetto) sono promossi a un tier compatto e pre-caldo;
  decadimento -> demozione, senza eliminazione.

### Allineamento ai 5 pattern RAG

- **Hybrid (01)**: gia baseline (ADR-0005). Invariato.
- **GraphRAG (02)**: si sfrutta il grafo **strutturale** gia presente
  (DAG dipendenze, gerarchia tag/client/project, link email-task e
  provenienza), non un knowledge graph estratto via LLM dal testo.
  GraphRAG testuale **differito** (costo alto, parzialmente ridondante
  col dominio tipizzato).
- **Agentic (03)**: il retrieval memoria e esposto come **un tool MCP**
  tra i tool deterministici; il planner LLM/MCP sceglie
  vector/SQL/strutturato ("retrieval come piano"). La decisione resta
  deterministica (ADR-0004/0013): l'LLM orchestra, non decide.
- **Corrective / CRAG (04)**: si aggiunge un **grader del retrieval**:
  soglia deterministica sullo score RRF fuso + grader LLM locale
  opzionale. Rami: ok -> usa; incerto -> riscrivi/espandi query;
  insufficiente -> allarga lo scope **entro il tenant** o rispondi
  "evidenza insufficiente". **Nessun ramo web**: la memoria e posta
  privata, il web violerebbe isolamento/GDPR. Deviazione consapevole
  dal pattern di riferimento.
- **Multimodal (05)**: v1 text-first, estrazione testo dagli allegati
  nella memoria testuale; multimodale vero (CLIP/ColPali, indice unico)
  **differito**.

## Conseguenze

- `memory_blobs` (F6) aggiunge: contatore/score di accesso con decay,
  segnale di importanza, riferimento al cluster-concetto; il job di
  promozione/demozione usa questi, non solo eta.
- Il grader e una nuova tappa del retrieval (F6); deterministico per
  default, LLM locale opzionale (coerente con la postura privacy).
- Nessun ramo di retrieval esce dal perimetro (no web sul privato).
- Invarianti ADR-0007 (isolamento per (org, progetto)) e ADR-0005
  (provenienza, no merge cross-soggetto) restano validi e vincolano la
  promozione dei concetti.

## Alternative scartate

- Eviction dei concetti rari per "fare spazio": viola l'invariante
  (raro != non importante); si demota, non si elimina.
- GraphRAG testuale in v1: costo sproporzionato e ridondante col grafo
  strutturale gia disponibile.
- Ramo CRAG "wrong -> web": incompatibile con memoria privata e GDPR.
- Tiering guidato dall'LLM: non deterministico/spiegabile (ADR-0004).
