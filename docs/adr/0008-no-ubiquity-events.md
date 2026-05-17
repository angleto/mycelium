# ADR-0008 No-ubiquita: entita events

Status: accettata. Requisito esplicito dell'utente.

## Contesto

Il sistema organizza appuntamenti per conto dell'utente. L'utente non
ha il dono dell'ubiquita: non puo avere due impegni con clienti diversi
nello stesso momento. La sola WorkingCalendar non modella appuntamenti
a orario fisso.

## Decisione

Nuova entita `events` (appuntamento): org-scoped, tag client/project,
partecipanti, intervallo time-pinned, luogo. Vincolo: nessuna
sovrapposizione di intervalli per lo stesso partecipante; creare o
spostare un appuntamento che si sovrappone per la stessa persona viene
**rifiutato**. Gli appuntamenti sono prenotazioni fisse esclusive sulla
timeline della persona e lo scheduler (ADR-0004) vi colloca attorno i
task umani non delegati.

## Conseguenze

- Lo scheduler tratta gli eventi come vincoli duri; i task umani della
  stessa persona non si sovrappongono ne tra loro ne agli appuntamenti.
- Notifica su tentativo di doppia prenotazione.

## Alternative scartate

- Modellare gli appuntamenti come task: un task e flessibile e
  schedulabile, un appuntamento e a orario fisso ed esclusivo; semantica
  diversa.
- Solo avviso non bloccante sulla sovrapposizione: il requisito chiede
  che il sistema non fissi impegni contemporanei, quindi rifiuto.
