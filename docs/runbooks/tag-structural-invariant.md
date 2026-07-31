# Runbook: structural tag invariant (client / project)

`client` and `project` are structural tags: a TASK carries exactly one of
each, a NOTE carries exactly one client and AT MOST one project, an
entity's client IS its project's `project_profile.client_tag_id`, and
`project_profile.client_tag_id` is NOT NULL
([ADR-0050](../adr/0050-structural-tag-cardinality.md)).
The asymmetry on notes is deliberate: a projectless note is the personal
retrieval perimeter (`memory_blobs.project_id` NULL, ADR-0021), not a
note missing a field. `services/tag_assignment.py` is the only writer of
client/project junction rows; migration `0086` repaired the pre-existing
drift and put the rule in the database (composite FKs plus five
`DEFERRABLE INITIALLY DEFERRED` constraint triggers).

Because the triggers are deferred they fire at COMMIT, under the
caller's RLS, with `SQLSTATE 23514`:

```
ERROR:  tag.structural_invariant: task <id> carries 2 client tag(s) and 1 project tag(s); exactly one of each is required
```

A `tag.structural_invariant` in the backend log is never a user error.
The user-facing rejections are service-layer `MessageCode`s
(`TAG_CLIENT_PROJECT_MISMATCH`, `TAG_STRUCTURAL_REQUIRED`) raised before
the write. The database message means either a write path bypassed
`tag_assignment`, or the rows predate the repair (see "Restoring a
pre-0086 dump" below).

| trigger | on | catches |
|---|---|---|
| `trg_task_tags_structural` | `task_tags` I/U/D | (a), (c) for tasks |
| `trg_tasks_structural` | `tasks` INSERT | a task inserted with zero junction rows |
| `trg_note_tags_structural` | `note_tags` I/U/D | (b), (c) for notes |
| `trg_notes_structural` | `notes` INSERT | a note inserted with zero junction rows |
| `trg_project_profile_client_coherence` | `project_profile` UPDATE OF `client_tag_id` | (c) broken wholesale by re-pointing a project |

## Checking a live database

`deploy/diagnose_tag_cardinality.sql` is SELECT-only, inside a
`READ ONLY` transaction, and safe against a live production. It emits
ids and counts only (no titles, no client names), so its output can be
pasted into a ticket.

Every table it reads is `FORCE ROW LEVEL SECURITY`, which applies to the
table owner too: as `mycelium` (the owner, the role in
`MYCELIUM_DATABASE_URL_SYNC`) with no `app.current_org` set, every query
would return zero rows and look perfectly clean. `Q0` refuses to run in
that state and RAISES instead:

```
ERROR:  Role mycelium is subject to RLS and app.current_org is unset: every query below
        would return 0 rows and look clean. Re-run as a superuser / BYPASSRLS role for
        cross-org numbers, or SET app.current_org = '<org-uuid>' first.
```

A failed run with that message means "wrong role", not "database
broken". Two ways through:

1. Cross-org, complete numbers: a superuser or a `BYPASSRLS` role.

   ```bash
   psql "postgresql://<superuser>@<host>:5432/mycelium" \
        -v ON_ERROR_STOP=1 -f deploy/diagnose_tag_cardinality.sql
   ```

   On managed PostgreSQL the provider's admin role is often neither
   `rolsuper` nor `rolbypassrls`, in which case Q0 rejects it as well.
   If a superuser is reachable, `ALTER ROLE mycelium BYPASSRLS` for the
   duration and REVOKE right after: leaving `BYPASSRLS` on the owner is
   a standing hole in the tenant isolation of ADR-0007. If no superuser
   is reachable at all, loop mode 2 over the org ids.

2. Single tenant, as the owner, subject to RLS. `psql` executes
   intermixed `-c` and `-f` in one session, so the GUC survives into the
   script:

   ```bash
   psql "postgresql://mycelium@<host>:5432/mycelium" -v ON_ERROR_STOP=1 \
        -c "SET app.current_org = '<org-uuid>'" \
        -f deploy/diagnose_tag_cardinality.sql
   ```

   Cross-org metrics (a tag borrowed from another org, metrics 40/41)
   are structurally invisible in this mode and read 0. Q1 flags it.

Reading the output, post-0086:

- The script's header prose predates the enforcement and states the
  invariant as "AT MOST ONE". Read the metric rows, not the prose: for
  tasks the rule is EXACTLY one, so metric `30` (project tag but NO
  client tag) is a violation now.
- Metric `31` (client tag but NO project tag) on `note` rows is LEGAL
  and expected on a healthy database: that is the personal perimeter.
  Only `31` on `task` rows is a violation. Metric `1` ("BASELINE clean")
  counts 1 client + 1 project, so a healthy tenant with personal notes
  does NOT show 100% under it.
- Metric `50` (an archived client/project tag still attached) is
  hygiene, not a violation: the invariant does not look at `tags.status`.
- Anything under `10`, `11`, `20`, `21`, `22`, `40`, `41`, and `30` /
  `31`-on-tasks, is real drift on a post-0086 database and should be
  zero. If it is not, something wrote junction rows without going
  through `tag_assignment`, or the data was restored past the guards.

## Running the check against production without extracting the secret

The point of the pattern below is that the DSN never lands on the
operator's laptop or in shell history: the SQL goes IN via a ConfigMap
(it is public, read-only, and lives in git), the credential is injected
by reference with `secretKeyRef`, and only ids and counts come OUT via
`kubectl logs`. `kubectl get secret -o jsonpath=...` would have put the
production password in the terminal scrollback.

The image is `postgres:17-alpine` purely for the `psql` binary: the
backend image installs `libpq5` (the psycopg runtime for the Alembic
sync path) and no client tools, so `kubectl exec deploy/mycelium-backend
-- psql` cannot work. A 17 client against the 16 server is fine.

```bash
kubectl -n mycelium-production create configmap tag-diagnose-sql \
  --from-file=diagnose.sql=deploy/diagnose_tag_cardinality.sql
```

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: tag-diagnose
  namespace: mycelium-production
spec:
  backoffLimit: 0              # a retry would just re-run the query
  activeDeadlineSeconds: 1800  # the script sets statement_timeout = 10min
  # No ttlSecondsAfterFinished ON PURPOSE: the Job (and its logs) must
  # survive until they have been read. mycelium-migrate sets 600.
  template:
    spec:
      restartPolicy: Never
      nodeSelector:
        pool: mycelium
      tolerations:
      - key: dedicated
        operator: Equal
        value: mycelium
        effect: NoSchedule
      containers:
      - name: psql
        image: postgres:17-alpine
        command: ["sh", "-c"]
        # The secret holds a SQLAlchemy URL; libpq rejects the driver
        # suffix, hence the strip. Never `set -x` / `echo` here: the
        # DSN carries the password and pod logs are readable by anyone
        # with `logs` on the namespace.
        args:
        - |
          DSN=$(printf '%s' "$MYCELIUM_DATABASE_URL_SYNC" | sed 's/+psycopg//')
          exec psql "$DSN" -v ON_ERROR_STOP=1 -f /sql/diagnose.sql
        env:
        - name: MYCELIUM_DATABASE_URL_SYNC
          valueFrom:
            secretKeyRef:
              name: mycelium-db
              key: MYCELIUM_DATABASE_URL_SYNC
        volumeMounts:
        - name: sql
          mountPath: /sql
          readOnly: true
      volumes:
      - name: sql
        configMap:
          name: tag-diagnose-sql
```

```bash
kubectl -n mycelium-production wait --for=condition=complete \
        job/tag-diagnose --timeout=30m
kubectl -n mycelium-production logs job/tag-diagnose \
  | tee tag-diagnose-$(date +%F).txt
kubectl -n mycelium-production delete job/tag-diagnose cm/tag-diagnose-sql
```

`mycelium-db/MYCELIUM_DATABASE_URL_SYNC` is the OWNER role, so the Job
as written hits the Q0 guard and fails: that is mode 1 above without the
role it needs. For a single-tenant answer, add the GUC to the same
psql invocation:

```
          exec psql "$DSN" -v ON_ERROR_STOP=1 \
               -c "SET app.current_org = '<org-uuid>'" -f /sql/diagnose.sql
```

For cross-org numbers, put a superuser/`BYPASSRLS` DSN in a one-off
Secret, reference it here instead, and delete the Secret with the Job.

## The client-changed list is not persisted: capture it at upgrade time

`0086` prints, at the end of `upgrade()`, the entities whose client the
repair changed:

```
0086: entities whose CLIENT changed -- re-key their attachments (mycelium_core.rekey_attachments): 10
0086:   task <id> client <old> -> <new>
...
0086: entities that carried SEVERAL client tags and KEPT this one -- client unchanged, old attachment key plan-dependent: the same re-key run settles these: 1
```

That list exists ONLY in the Job's stdout. It is accumulated in
`tmp_0086_client_moved`, a temp table declared `ON COMMIT DROP`: nothing
is written to a real table, no `activity_log` row, no file. And
`mycelium-migrate` sets `ttlSecondsAfterFinished: 600`, so the Job, its
pod and `kubectl logs` are garbage-collected ten minutes after it
completes. Capture during the upgrade, not after:

```bash
kubectl -n mycelium-production logs -f job/mycelium-migrate \
  | tee 0086-upgrade-$(date +%F).log
```

Paste both lists into the release ticket. Why they matter:
`services/attachments.py::_build_storage_key` embeds the resolved client
id in the object key (`org/<org>/client/<client>/tasks/<task>/...`), so
an entity that moved to another client has every attachment filed under
the old client's prefix. Re-key them:

```bash
kubectl -n mycelium-production exec deploy/mycelium-backend -- \
    env MYCELIUM_DATABASE_URL='postgresql+asyncpg://<bypassrls-role>:<pw>@<host>/mycelium' \
    python -m mycelium_core.rekey_attachments --dry-run
```

(drop `--dry-run` to apply; the env wiring for a standalone Job is the
same as `attachment-migrate-job.yaml`, which needs the config ConfigMap
plus the DB, JWT, secret-key and S3 secrets, because it goes through app
`Settings`).

READ THE ROLE PARAGRAPH BEFORE RUNNING IT. The scan is one cross-tenant
`admin_session` over `attachments`, and that session never sets
`app.current_org`, so `p_attachments` (`org_id = current_setting(...)`)
matches NOTHING for any role subject to RLS -- the same fail-closed
posture `Q0` of the diagnose script refuses to run under, for the same
reason. The pod's own `MYCELIUM_DATABASE_URL` is `mycelium_app`, which
is subject to RLS: run with it and the tool prints `Inspecting 0
object-store-backed attachments`, `inspected=0`, and EXITS 0. It looks
like a clean run and is a no-op. It needs a superuser or `BYPASSRLS`
DSN, exactly like mode 1 of "Checking a live database" -- and, as noted
there, the owner role is not necessarily `BYPASSRLS` on managed
PostgreSQL. Sanity-check the `Inspecting N` line against the number of
S3-backed rows you expect before trusting any run.

Both printed lists are work for that one run, and neither is a manual
job: `rekey_attachments` compares each row against the key
`_build_storage_key` would give it today, so it moves a hierarchical
key filed under the previous client just as it moves a flat legacy one,
and it leaves a row already on its expected key alone (that is also its
idempotence: a second run is a no-op). The second, "several client
tags" list is printed apart only because its REASON differs: those
entities kept the client they already had, but
`attachments._resolve_client_tag_id` picks with `LIMIT 1` and no
`ORDER BY`, so which of their several clients ended up in the old key
is plan-dependent -- which the expected-key comparison resolves without
anyone having to guess.

## Restoring a pre-0086 dump: THE dangerous case

A dump taken before `0086` carries the old drift: entities with two
clients, projects with no client, clients contradicting their project.
Restoring it does not resurrect a merely untidy database, it resurrects
a database the running code assumes cannot exist.

**Safe path: restore the FULL dump (schema + data) into an EMPTY
database.** You get the pre-0086 schema, no triggers, no NOT NULL, and
`alembic_version` at the dump's revision. Then `alembic -c
core/alembic.ini upgrade head` re-runs `0086`, repair included, and
prints the client-changed list again (capture it, per the section
above).

**Dangerous path: a data-only restore into a database already at
`0086`.**

- Plain `pg_restore --data-only`: the triggers and the composite FKs are
  live, so the restore fails part-way and leaves a half-loaded database.
  Loud, but not clean.
- `pg_restore --disable-triggers`, or `SET session_replication_role =
  'replica'`: this is the one that hurts. As a superuser it issues
  `ALTER TABLE ... DISABLE TRIGGER ALL`, which turns off the five
  constraint triggers AND the FK triggers, so every drifted row loads
  without a murmur and the database looks healthy. `NOT NULL` and
  `CHECK` constraints are not triggers and still apply, so
  `project_profile.client_tag_id` and the `ck_*_kind` checks may reject
  a table or two while everything else lands: a MIXED state is the
  likely outcome, not a clean failure.
- Either way the guards say nothing until the first transaction that
  touches an offending entity commits, i.e. in a user request, hours or
  weeks later, as a 500 on an entity nobody edited today.

A restored pre-0086 database is not trustworthy until the repair DML has
been re-run. Procedure:

1. Stop the writers: scale `mycelium-backend`, `mycelium-worker` and
   `mycelium-sdi-inbound` to 0. The steps below leave the database
   unguarded for a while.
2. Reconcile `alembic_version` BEFORE anything else. A data-only restore
   may have overwritten it with the dump's revision while the schema is
   still at `0086`. Compare the row against the actual schema:

   ```sql
   SELECT version_num FROM alembic_version;
   SELECT tgname FROM pg_trigger WHERE tgname LIKE 'trg_%structural%';
   ```

   Triggers present but the row says `0085` means `alembic -c
   core/alembic.ini stamp 0086` first, otherwise the next upgrade tries
   to re-add existing DDL and dies on "constraint already exists".
3. Replay the repair through the migration, which is its only
   maintained copy:

   ```bash
   alembic -c core/alembic.ini downgrade 0085
   alembic -c core/alembic.ini upgrade 0086
   ```

   `downgrade()` reverses the schema in full (triggers, functions,
   composite FKs, added columns, the NOT NULL) and deliberately does NOT
   undo data; `upgrade()` then re-runs the repair, which is written
   idempotent, and re-creates the guards. Capture stdout. Finish with
   `upgrade head` if the environment is meant to stand past `0086`.
4. If later revisions are already applied on top and downgrading through
   them is not acceptable, the fallback is to replay `_repair`'s
   statements by hand from
   `core/migrations/versions/0086_tag_structural_invariant.py`, in ONE
   transaction, inside the same `NO FORCE ROW LEVEL SECURITY` / `try` /
   restore bracket. It works with the guards in place precisely because
   they are `DEFERRABLE INITIALLY DEFERRED`: the intermediate states are
   never checked, only the final COMMIT. It is the awkward path; prefer
   step 3.
5. Re-run `deploy/diagnose_tag_cardinality.sql` and read it. This step
   is mandatory, not a formality: `CREATE CONSTRAINT TRIGGER` does not
   validate existing rows, so a guard re-created over data the repair
   somehow missed is silent until it bites. (The composite FKs and the
   NOT NULL, by contrast, ARE validated when added: they fail loudly in
   step 3.)
6. Re-key the attachments of everything on the client-changed list.
   One plain `rekey_attachments` run covers both printed lists; mind
   the role requirement in the section above, or it will report zero
   rows and exit 0.
7. Scale the writers back up.

The same applies, in miniature, to any partial restore: a single table
copied back from a pre-0086 dump into `task_tags` / `note_tags` /
`project_profile` reintroduces the drift for those rows only, and the
`--disable-triggers` bypass is just as total.
