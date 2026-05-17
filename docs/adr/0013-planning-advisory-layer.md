# ADR-0013 Layer di pianificazione advisory, core deterministico

Status: accettata. Scelta dell'utente (core v1).

## Contesto

La ragion d'essere del prodotto e aiutare nella pianificazione
efficace: rispondere a domande come "ho mezz'ora, cosa di necessario
posso fare?", "vado al brico, cosa mi serve?", "budget X per spese di
casa, quali priorita?". Sono query decisionali contestuali, non CRUD.

## Decisione

Layer advisory di prima classe in v1, nel service layer, esposto via
REST + tool MCP. Nucleo decisionale **deterministico e spiegabile**:
filtro di fattibilita + ranking + selezione vincolata (knapsack a
priorita). L'LLM/MCP e il **frontend in linguaggio naturale**: traduce
la richiesta in query strutturata, compone con la memoria e narra il
risultato; non e l'oracolo che decide. Tre archetipi: cosa-faccio-ora
(finestra libera), errand/contesto (luogo), prioritizzazione entro
budget. Determinismo verificabile (stesso input, stesso output).

## Conseguenze

- Coerente con ADR-0004 (core deterministico, LLM come interfaccia,
  niente magia opaca): spiegabilita e fiducia dell'utente.
- Costruito sopra scheduler (F3), time tracking (F4) e attributi
  personali/budget (ADR-0014); fase F4b.
- Opera sui task accessibili all'utente entro una org, anche
  multi-progetto: NON e una violazione dell'isolamento memoria
  (ADR-0007), che governa il contenuto RAG/email, non la lista task
  dell'utente. Distinzione da documentare e testare.

## Alternative scartate

- Layer successivo / post-core: e la ragion d'essere del prodotto,
  rimandarlo svuoterebbe il v1.
- Decisione guidata dall'LLM (ranking/selezione lasciati al modello):
  non spiegabile ne deterministico, incoerente con ADR-0004.
