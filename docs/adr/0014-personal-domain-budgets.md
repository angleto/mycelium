# ADR-0014 Dominio personale e budget envelope

Status: accettata. Scelta dell'utente (modellati in v1).

## Contesto

Le query advisory (ADR-0013) richiedono che i task abbiano attributi di
vita reale: durata (gia presente), luogo, contesto/precondizioni,
necessita, e un costo monetario con budget per la prioritizzazione
delle spese (es. spese di casa). Il dominio v2 era orientato a
lavoro/clienti/fatturazione.

## Decisione

Modellare ora, riusando la tassonomia esistente (ADR-0003), senza
entita parallele:

- Attributi sul task: `monetary_cost?`, `location?`, `necessity`
  (must/should/nice); contesto/precondizioni via tag `generic` con
  convenzione di namespace (es. `ctx:richiede-computer`, `place:brico`).
- Un progetto puo essere personale (project non fatturabile): stesso
  modello tag/profilo, nessun dominio separato.
- `Budget`: envelope org-scoped per periodo (mese/trimestre/anno/
  custom) e categoria, con importo allocabile e valuta; i task vi si
  agganciano via `budget_id`; consumo/residuo calcolati dal service
  layer.
- La selezione entro budget e una **selezione vincolata deterministica**
  (knapsack a priorita/valore-densita, must-have prima), non un
  giudizio dell'LLM.

## Conseguenze

- Un solo modello concettuale (tag/progetto) copre lavoro e vita
  personale; nessuna duplicazione.
- I budget personali stanno comunque dentro la org dell'utente
  (un'org puo essere un workspace mono-persona): coerente con il
  multi-tenant e con RLS.

## Alternative scartate

- Dominio "personale" separato e parallelo: duplica tassonomia e
  isolamento senza beneficio.
- Costo/budget come metadati liberi non tipizzati: impedisce la
  selezione vincolata corretta e i controlli di valuta/periodo.
- Stratificare budget post-v1: l'utente ha chiesto esplicitamente di
  modellarli ora (terzo archetipo advisory).
