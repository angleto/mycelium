# ADR-0004 Scheduler deterministico, non RCPSP

Status: accettata. Corregge un'incoerenza algoritmica di una bozza
precedente.

## Contesto

Una bozza prescriveva un "motore CPM che rispetta calendari e
capacita". E una contraddizione: il CPM assume risorse illimitate e
durate fisse; con capacita per-utente, assegnatari multipli e durate
derivate dall'effort diventa RCPSP, NP-hard, senza una passata
avanti/indietro ne un singolo percorso critico ben definito. Lo schema
`schedule` (uno slack, un flag percorso critico) era vincolato
all'oggetto sbagliato. L'utente ha poi chiarito il modello reale: i
task che deve svolgere di persona non sono concorrenti; quelli delegati
a un LLM possono esserlo; e non ha il dono dell'ubiquita (vedi
ADR-0008).

## Decisione

Niente RCPSP generico. Motore = **CPM logico deterministico** su
calendari lavorativi (ES/EF/LS/LF, slack, percorso critico logico,
onesti perche senza contesa) + **piazzamento seriale deterministico
per-persona** dei task `executor=human` non delegati, attorno agli
appuntamenti fissi, con regola a priorita stabile. I task
`executor=llm_agent` sono fuori dalla timeline umana (paralleli, solo
precedenza). Necessari: piano vs consuntivo (`remaining_effort_h`,
`actual_start`, stato terminale), `schedule_mode`/constraint con drag
write-back che sopravvive al ricalcolo, determinismo verificabile,
rollup sommario. Le 4 disuguaglianze di dipendenza sono in tempo
lavorativo (vedi functional-requirements FR-4).

## Conseguenze

- Deterministico, O(V+E), millisecondi su centinaia di task su nodo
  ARM; slack e percorso critico ben definiti perche logici.
- Il sovraccarico tra task e gestito da serializzazione per-persona
  (cio che l'utente vuole) piu un indicatore di sovraccarico, non da
  un solver opaco.

## Alternative scartate

- RCPSP / leveling euristico stile MS Project: euristico, instabile e
  inspiegabile; pessimo trade-off per "pronto all'uso da subito".
- CP-SAT (OR-Tools): leveling ottimizzante, valido ma post-v1 opzionale
  (non interattivo-istantaneo, output meno spiegabile).
