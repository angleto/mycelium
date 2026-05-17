# Flow, documentazione di progetto

Flow e un sistema multi-tenant di task e workflow management che unifica
task manager, time tracker, scheduler, email-to-task, fatturazione
elettronica italiana (SDI) e una memoria gerarchica con recupero
semantico, con un layer MCP co-paritario alla GUI.

Stato: requisiti e architettura **decisi** (post revisione critica).
Scope: **MVP stratificato** (tutto completo da subito tranne la
fatturazione SDI, introdotta per fasi). Ultimo aggiornamento:
2026-05-17.

Questi documenti sono la fonte di verita e sostituiscono ogni bozza di
pianificazione precedente.

## Indice

- [Contesto, scope, MVP](context.md)
- [Decisioni](decisions.md)
- [Modello di dominio](domain-model.md)
- [Modello dati](data-model.md)
- [Requisiti funzionali](functional-requirements.md)
- [Requisiti non funzionali](non-functional-requirements.md)
- [Architettura](architecture.md)
- [Roadmap a fasi e criteri di verifica](roadmap.md)
- [Riferimenti](references.md)
- [Architecture Decision Records](adr/README.md)

## Come leggere

Per capire **cosa** si costruisce: contesto, requisiti funzionali e non
funzionali. Per capire **come**: modello di dominio, modello dati,
architettura. Per capire **perche** una scelta non ovvia e stata fatta
(e quali alternative sono state scartate): gli ADR. Per capire
**quando**: roadmap.

## Principi non negoziabili

- Service layer unico in `core/`; `api/` (REST/WS) e `mcp/` sono
  adapter sottili senza logica di business.
- Multi-tenant con isolamento duro: RLS obbligatoria, e per la memoria
  isolamento per (org, progetto), mai per sola rilevanza.
- Optimistic concurrency, niente last-write-wins.
- Proporre la soluzione architetturale corretta, non la piu comoda; le
  scelte corrette gia prese non vanno regredite (vedi ADR).
