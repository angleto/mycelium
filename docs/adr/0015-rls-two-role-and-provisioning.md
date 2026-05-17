# ADR-0015 RLS: due ruoli Postgres e provisioning SECURITY DEFINER

Status: accettata. Emersa implementando F0.

## Contesto

ADR-0002/0007 impongono RLS come difesa primaria. Due fatti di
PostgreSQL la rendono non banale:

1. Un **superuser bypassa sempre la RLS** (anche con FORCE). Se l'app si
   connette come superuser (il default dell'immagine `postgres`),
   l'isolamento RLS e un no-op.
2. RLS + `FORCE` rende la creazione di una nuova organizzazione un
   problema uovo-gallina: l'INSERT in `organizations` non puo
   soddisfare una policy che richiede `app.current_org` per una org che
   non esiste ancora.

## Decisione

- **Due ruoli**: `flow` (owner/superuser: DDL, migrazioni) e **`flow_app`**
  (runtime: LOGIN, NOSUPERUSER, non proprietario, soggetto a RLS+FORCE).
  L'app si connette come `flow_app`; le migrazioni come `flow`.
- **RLS + FORCE** su tutte le entita org-scoped; policy su
  `nullif(current_setting('app.current_*', true),'')::uuid`
  (fail-closed: GUC assente -> nessuna riga).
- **Provisioning tenant** via funzione `SECURITY DEFINER`
  `provision_organization(name, user_id)` di proprieta di `flow`
  (esegue come owner, quindi puo creare org+membership), con
  `search_path` fisso, `EXECUTE` concesso solo a `flow_app`. Unico punto
  che crea una org; nessun bypass RLS sparso nel codice.
- Ruolo creato (senza password) dalla migrazione baseline per
  idempotenza dello schema; la password di `flow_app` e impostata da un
  bootstrap separato (`deploy/bootstrap_roles.sql`) a partire da una
  variabile d'ambiente: nessun segreto nel git.

## Conseguenze

- L'isolamento RLS e realmente applicato (l'app non e superuser): i test
  di verifica F0 connettono come `flow_app`.
- Ordine operativo: bootstrap ruolo+password, poi `alembic upgrade`.
- `memory_blobs` e `PARTITION BY HASH (org_id)` (PK composta
  `(id, org_id)`, vincolo del partitioning); la RLS sulla tabella padre
  si applica alle partizioni.
- `activity_log` e append-only via trigger che vieta UPDATE/DELETE.

## Alternative scartate

- App come superuser/owner: la RLS non verrebbe applicata (bypass).
- Bypass RLS ad-hoc nel codice per il provisioning: sparpaglia il
  privilegio; la funzione SECURITY DEFINER lo confina a un punto solo.
- Password del ruolo nella migrazione: segreto nel version control.
