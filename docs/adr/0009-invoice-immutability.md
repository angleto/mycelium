# ADR-0009 Immutabilita della fattura, carve-out soft-delete

Status: accettata. Corregge un conflitto della bozza generica.

## Contesto

FR-1 prevede soft-delete con ripristino su tutte le entita. Una fattura
emessa (trasmessa a SdI e non scartata) e un documento fiscale soggetto
a obbligo di conservazione decennale e integrita. Soft-delete/ripristino
generico romperebbe immutabilita e numerazione progressiva.

## Decisione

La fattura ha una macchina a stati: `draft` -> `transmitted` -> stati
terminali SdI. Solo `draft` e cancellabile. Dopo l'emissione il record
e append-only e immutabile; l'unica "rimozione" logica e una nota di
credito TD04 collegata via `parent_invoice_id`. Carve-out esplicito in
FR-1: la soft-delete non si applica a fatture emesse e documenti
conservati. La numerazione progressiva per (Org, serie, anno) e
allocata in modo concorrenza-safe solo alla transizione
draft -> transmitted, nella stessa transazione, e mai riusata (vedi
FR-9).

## Conseguenze

- Integrita e progressivita della numerazione preservate sotto
  concorrenza.
- Correzioni solo via TD04, come da prassi.

## Alternative scartate

- Soft-delete uniforme anche sulle fatture: viola immutabilita e
  progressivita, non conforme.
