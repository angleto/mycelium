# ADR-0011 SDI: modello intermediario/mandato, v1 B2B/B2C

Status: accettata. Corregge un errore legale di una bozza precedente.

## Contesto

Una bozza decideva "accreditamento diretto, nessun intermediario terzo"
come elemento distintivo. In un SaaS multi-tenant e sbagliato sul
diritto: nel momento in cui un unico canale accreditato trasmette
fatture il cui `CedentePrestatore` e di tenant diversi, l'operatore
**e** trasmittente per conto terzi, cioe intermediario. Il certificato
mutua-TLS del canale e legato a un solo codice fiscale (CA AdE) e non
porta identita per-tenant; l'accreditamento per-tenant e infattibile.

## Decisione

Canale **unico condiviso**; identita del tenant nel **payload
FatturaPA** (`CedentePrestatore`, `TerzoIntermediarioOSoggetto
Emittente`), mai nell'identita TLS. Modello esplicito `SdiMandate`
per-Org (scope, validita, revoca, audit): Flow trasmette per conto del
tenant sotto mandato e assume i doveri operativi dell'intermediario
(endpoint SOAP inbound sempre attivo, correlazione notifiche per
`IdentificativoSdI`, audit). v1 = **solo B2B/B2C**: nessuna firma (via
canale accreditato non richiesta), notifiche del ciclo attivo RC, MC,
NS, AT. **Post-v1**: PA/B2G (firma CAdES/XAdES + certificato
qualificato, NE/DT/EC/SE), ciclo passivo, reverse charge/autofattura,
esteri, bollo trimestrale. Introduzione per fasi (F7a manuale, F7b
SdICoop test, F7c produzione).

## Conseguenze

- `IdentificativoSdI` e colonna indicizzata di prima classe per
  correlare le notifiche in push al tenant giusto.
- F7c (Accordo di servizio + accreditamento + endpoint inbound mutua
  TLS sempre attivo) e l'item piu pesante, va risorsato come tale.
- Scope fiscale v1 minimo esplicito; il resto dichiarato come differito,
  non implicitamente "completo".

## Alternative scartate

- "Accreditamento diretto senza intermediario" in multi-tenant: errato
  sul diritto e tecnicamente infattibile (un canale = un codice
  fiscale).
- Intermediario terzo via API: valido ma scartato perche l'utente vuole
  niente terzo che veda le fatture; il modello a mandato con canale
  proprio soddisfa il vincolo restando legalmente corretto.
