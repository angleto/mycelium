# ADR-0002 Multi-tenant: optimistic concurrency, RLS obbligatoria

Status: accettata. Corregge una scelta sbagliata di una bozza
precedente.

## Contesto

Una bozza precedente prescriveva contemporaneamente "last-write-wins"
**e** "version (optimistic concurrency)". Sono politiche di conflitto
opposte: LWW accetta la scrittura stale sovrascrivendo (lost update
accettato); l'optimistic concurrency rifiuta la scrittura stale (lost
update prevenuto). Non possono coesistere sullo stesso path. Inoltre la
bozza marcava la RLS come "opzionale": per un sistema multi-tenant che
contiene email e dati fiscali, lasciare l'isolamento alla sola
diligenza delle query e una regressione di sicurezza.

## Decisione

Solo **optimistic concurrency**: `UPDATE ... WHERE id = ? AND
version = ?`; 0 righe -> `409 Conflict` propagato a GUI/REST/MCP
(enforce nel service layer). Activity log append-only. Invalidazione
realtime via WebSocket. Niente lost update silenzioso. **RLS
obbligatoria** su tutte le entita org-scoped come difesa primaria, non
opzionale.

## Conseguenze

- Il modello e idoneo a introdurre collaborazione in futuro senza
  riprogettare il core (LWW farebbe il contrario).
- Le righe derivate (es. `schedule`) non sono soggette a optimistic
  concurrency utente: vince la ricomputazione piu recente.

## Alternative scartate

- Last-write-wins: introduce lost update strutturali che qualunque
  feature collaborativa futura dovrebbe poi disfare.
- RLS opzionale: un singolo predicato dimenticato = leak cross-tenant.
