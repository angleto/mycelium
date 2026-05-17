# Contesto, scope, MVP

## Cosa e Flow

Sistema multi-tenant/team che unifica cinque capacita oggi separate:

1. Task manager in stile Todoist (il task come unita primaria).
2. Time tracker in stile Toggl (timer + voci manuali, report).
3. Dipendenze tra task con grafo di workflow e scheduling/Gantt.
4. Email multi-account (lettura + invio) con "mail to task".
5. Fatturazione elettronica italiana (SDI) end-to-end.

Layer MCP co-paritario alla GUI: stessa logica di dominio, due client.

Terminologia: l'entita primaria si chiama sempre **Task** (in
discussione informale a volte detta "card").

## Obiettivi

- Essere un copilota di pianificazione: rispondere a query advisory
  contestuali (cosa fare in una finestra libera, cosa serve per un
  errand/luogo, priorita entro un budget di spesa), con nucleo
  decisionale deterministico e LLM/MCP come interfaccia in linguaggio
  naturale.
- Organizzare l'informazione per conto dell'utente senza mescolare
  contesti: nessun data leak tra tenant ne tra progetti.
- Pianificazione realistica del tempo dell'utente: niente ubiquita
  (non due impegni contemporanei per la stessa persona); i task che
  l'utente deve svolgere di persona non sono concorrenti, salvo delega
  a un agente LLM.
- Fatturare in modo semplice il tempo tracciato, senza passare dal
  portale dell'Agenzia delle Entrate.

## Realismo di scope (esplicito)

"Tutto perfetto dal primo giorno" non e un v1: sono di fatto piu
prodotti. La stratificazione e dettata da realta legale e algoritmica,
non da comodita.

### Completo da subito (fattibile e corretto)

Task, tassonomia, workflow configurabili, dipendenze e grafo, scheduler
deterministico, time tracking, eventi/no-ubiquita, esecutore
umano/LLM, email Gmail + IMAP generico, memoria con isolamento per
progetto, dominio personale + budget, assistente di pianificazione
advisory, MCP, multi-tenant con RLS e optimistic concurrency.

### Stratificato (MVP stratificato, scelta confermata)

Fatturazione SDI:

- v1 **solo B2B/B2C** a profilo fiscale minimo esplicito (TD01/TD04,
  aliquote standard + insieme Natura ridotto, ritenuta opzionale, bollo
  come flag con export trimestrale manuale).
- Poi canale SdICoop in ambiente di test, quindi in produzione.
- **Post-v1**: PA/B2G (firma CAdES/XAdES + certificato qualificato,
  notifiche NE/DT/EC/SE), ciclo passivo, reverse charge/autofattura
  TD16-TD19, clienti esteri, liquidazione trimestrale del bollo,
  leveling ottimizzante CP-SAT.

Proton Mail (via Bridge) dopo Gmail.

Conseguenza operativa da possedere consapevolmente: la conservazione a
norma scelta e il servizio gratuito dell'AdE, che richiede l'adesione
del singolo tenant nel proprio cassetto fiscale e conserva solo cio che
transita da SdI. Le fatture emesse via export manuale in fase iniziale
non sono coperte dall'AdE e restano a carico del tenant finche il
canale SdICoop non e attivo. Vedi
[ADR-0010](adr/0010-conservazione-ade-free-service.md).
