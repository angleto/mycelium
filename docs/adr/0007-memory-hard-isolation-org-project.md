# ADR-0007 Isolamento memoria duro per (org, progetto)

Status: accettata. Requisito esplicito dell'utente.

## Contesto

Il sistema gestira molta informazione organizzata per conto
dell'utente. Requisito: non mescolare la memoria di un progetto con
quella di un altro, nessun data leak, ne tra tenant ne tra progetti.
Un filtro di sola rilevanza non e una garanzia di isolamento (vedi la
trappola filtered-ANN: un `WHERE` selettivo su HNSW puo degradare a
post-filtro inaffidabile).

## Decisione

Confine **duro**, non per rilevanza. `memory_blobs` partizionata per
`org_id`, indici HNSW/GIN/trigram per partizione. Predicato
(org_id, project_tag_id) **obbligatorio** in ogni query di memoria.
RLS obbligatoria. Default = progetto corrente; accesso cross-progetto
solo con autorizzazione esplicita e auditata. Nessun
retrieval/summarization/consolidamento attraversa progetti o tenant; il
consolidamento e limitato a stesso (org, progetto, thread/account) e
mai cross-soggetto. La regola vale identica via MCP (FR-10).

## Conseguenze

- Isolamento e sicurezza dati garantiti da RLS + partizione +
  predicato; il pre-filtro metadati resta solo rilevanza.
- Test obbligatorio: una ricerca senza filtro non restituisce mai dati
  di un altro progetto/tenant.

## Alternative scartate

- Isolamento solo a livello org con progetto come rilevanza: viola il
  requisito (mescolerebbe progetti).
- Affidare l'isolamento alla sola query application-level: un predicato
  dimenticato = leak; serve la difesa in profondita (RLS + partizione).
