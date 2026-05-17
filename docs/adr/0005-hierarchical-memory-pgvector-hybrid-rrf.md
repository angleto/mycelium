# ADR-0005 Memoria gerarchica su pgvector, retrieval ibrido RRF

Status: accettata.

## Contesto

Serve una memoria multi-livello che riassuma il vecchio, lo mandi in
recupero semantico e lo ritrovi, su DB (non store numpy lato app).
L'utente ha richiesto esplicitamente il ramo lessicale nella ricerca
ibrida (era stato proposto opzionale; ora e baseline).

## Decisione

Tier hot/warm/cold. Cold = embedding in `pgvector` con indice HNSW.
Retrieval ibrido baseline: ramo semantico (HNSW) + ramo lessicale
(`tsvector`/`ts_rank`, `pg_trgm` con indice trigram dedicato), fusione
**RRF** (rank-based, k circa 60, niente normalizzazione di score
incommensurabili). K sovracampionato per ramo (circa 100) prima della
fusione; tiebreak deterministico; fusione entro (org, progetto). Per
filtri molto selettivi (message-id, numero fattura) path esatto, non
HNSW. `hnsw.iterative_scan = relaxed_order` tarato. Embedding pluggable
(ADR-0012) con `model_id`+`dim` per blob e job di re-embedding
(nuova colonna/tabella, dual-write, `CREATE INDEX CONCURRENTLY`,
cutover atomico): garanzia onesta = nessun write-downtime, possibile
degrado di latenza in lettura su nodo singolo durante il backfill.
Provenienza N:1 esplicita (`blob_sources`) per la cancellazione GDPR;
consolidamento mai cross-soggetto. Isolamento: vedi ADR-0007.

## Conseguenze

- Recupero robusto su match esatti e semantici; nessun iperparametro
  di score da tarare (solo `k` di RRF).
- "Niente numpy" significa: nessuno store di similarita numpy lato app;
  l'`Embedder` locale puo dipendere da numpy transitivamente.

## Alternative scartate

- Solo semantico: perde i match esatti su token rari in testo libero
  (l'utente ha richiesto esplicitamente il lessicale).
- Fusione con pesi su score grezzi: score di `ts_rank` e cosine
  incommensurabili; RRF rank-based evita il problema.
- Claim "nessun read-downtime" sul re-embedding: non sostenibile su
  nodo ARM singolo; sostituito con una garanzia onesta.
