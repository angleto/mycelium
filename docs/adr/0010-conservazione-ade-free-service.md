# ADR-0010 Conservazione: servizio AdE gratuito

Status: accettata. Scelta dell'utente tra le opzioni presentate.

## Contesto

La conservazione sostitutiva a norma e un obbligo legale (art. 39 DPR
633/72, 10 anni; Linee Guida AgID) sui dati che il sistema memorizza, ed
era del tutto assente in una bozza. Opzioni: servizio AdE gratuito;
conservatore qualificato terzo via API; self-managed con Responsabile e
Manuale della conservazione.

## Decisione

Strategia = **servizio AdE gratuito**. `ConservationProvider =
AdeFreeConservation`: Flow non conserva in proprio, **traccia e guida
l'adesione per-tenant** nel cassetto fiscale Fatture e Corrispettivi
(stato adesione sul profilo Org) e si appoggia alla conservazione AdE
delle fatture transitate da SdI.

## Conseguenze

- Costo minimo, nessun onere di Responsabile/Manuale lato Flow.
- L'adesione e per soggetto IVA: Flow non puo aderire al posto del
  tenant, puo solo guidarla/verificarla.
- L'AdE conserva solo cio che passa da SdI: le fatture emesse via
  `ManualExportChannel` in F7a sono fuori copertura e vanno marcate "a
  carico del tenant". Copertura effettiva da F7b.
- Astrazione `ConservationProvider` mantenuta pluggable per poter
  passare a conservatore terzo in futuro.

## Alternative scartate

- Conservatore qualificato terzo: miglior trasferimento del rischio per
  un SaaS ma costo ricorrente e integrazione, non scelto ora.
- Self-managed: richiede Responsabile della conservazione e Manuale,
  pacchetti, hash, esibizione; onere continuo elevato.
