# ADR-0003 Tag unificato con profili satellite tipizzati

Status: accettata.

## Contesto

Requisito dell'utente: client e project sono **tag speciali**, un solo
concetto di tag, nessuna entita aggiuntiva. Pero i dati di un client
sono legali/fiscali (P.IVA, codice destinatario, regime) e quelli di un
project sono di billing (tariffa, valuta, budget): metterli in JSONB
libero su una tabella tag generica sacrifica vincoli, tipi e validazione
che la fatturazione richiede per legge e che l'isolamento memoria per
progetto richiede per integrita.

## Decisione

Un solo concetto `tags(kind in {generic, client, project})` (rispetta
il requisito). I dati strutturati stanno in **profili satellite
tipizzati** con FK a `tags.id`: `client_profile(tag_id PK, ...)` e
`project_profile(tag_id PK, client_tag_id FK, ...)`. Associare
cliente/progetto a un task = attaccare il tag (relazione unica per ogni
kind).

## Conseguenze

- Modello concettuale unico come richiesto, ma con integrita
  referenziale e validazione sui dati fiscali e di billing.
- Le query di reporting e fatturazione aggregano via i tag
  client/project con join ai profili.

## Alternative scartate

- Attributi in JSONB libero sul tag generico: nessun vincolo,
  validazione fragile su dati legalmente sensibili.
- Entita Client/Project separate dai tag: viola il requisito esplicito
  dell'utente.
