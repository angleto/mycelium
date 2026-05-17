# ADR-0006 Cifratura at-rest a livello di volume

Status: accettata. Risolve una contraddizione interna di una bozza
precedente.

## Contesto

Una bozza richiedeva "corpo email cifrato at-rest in Postgres" con
primitive app-level (libsodium/Fernet) e, contemporaneamente, full-text
`tsvector` ed embedding locale del corpo. Sono incompatibili: un indice
GIN/tsvector estrae lessemi dal testo in chiaro e un embedding del
ciphertext e rumore. Con cifratura app-level del corpo, FTS ed embedding
sul corpo non funzionano.

## Decisione

Cifratura at-rest = cifratura del **volume** Postgres + object storage
(LUKS / block storage cifrato / TDE managed): corpo, `tsvector` ed
`embedding` restano in chiaro dentro il DB ma il disco e cifrato, quindi
indicizzabili e ricercabili. L'envelope app-level (libsodium/Fernet) e
riservato ai **segreti opachi non indicizzati**: token OAuth,
credenziali, materiale del canale SdI. Modello di minaccia dichiarato:
protegge da disco/snapshot rubato, non da connessione DB viva o app/DBA
compromessi.

## Conseguenze

- FTS + embedding del corpo funzionano (requisito di retrieval ibrido).
- La confidenzialita verso un attaccante con DB vivo non e coperta da
  questa misura: e una scelta dichiarata, non implicita; si compensa
  con RLS obbligatoria (ADR-0002) e isolamento (ADR-0007).

## Alternative scartate

- Envelope app-level del corpo: rompe FTS ed embedding (la
  contraddizione originale).
- Searchable encryption (EQL/indici cifrati): nessun ANN cosine su
  vettori cifrati; complessita sproporzionata per un nodo ARM singolo.
