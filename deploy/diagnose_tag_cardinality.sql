-- Read-only diagnostic: how badly has the client/project tagging drifted?
--
-- Measures the four invariants we want to enforce (docs/adr/0003 unified tag):
--   (a) a task has AT MOST ONE tag of kind 'client' and AT MOST ONE of kind 'project'
--   (b) same for a note
--   (c) the project tag on a task/note belongs to the SAME client as that
--       entity's client tag (project_profile.client_tag_id == entity's client tag)
--   (d) every project has exactly one client (project_profile.client_tag_id NOT NULL)
--
-- None of these is enforced today: task_tags/note_tags have PK (entity_id, tag_id)
-- and no partial unique index on the tag kind, and project_profile.client_tag_id is
-- NULLABLE with FK ON DELETE SET NULL. This script quantifies the resulting drift
-- before any repair migration is written.
--
-- SAFETY: SELECT only, wrapped in a READ ONLY transaction. No INSERT/UPDATE/DELETE,
-- no DDL, no alembic. Safe to run against production while the app is live.
--
-- HOW TO RUN
--
--   1. Preferred (cross-org, complete numbers) -- as a SUPERUSER or a BYPASSRLS role:
--
--        psql "postgresql://<superuser>@<host>:5432/mycelium" \
--             -v ON_ERROR_STOP=1 -f deploy/diagnose_tag_cardinality.sql
--
--   2. Single tenant, as the owner/app role (subject to RLS): set the tenant GUC
--      first. Every table below is FORCE ROW LEVEL SECURITY, so even the table
--      owner `mycelium` is filtered -- without the GUC you get ZERO rows, which
--      looks exactly like "no corruption". Q0 below refuses to run in that state.
--
--        psql "postgresql://mycelium@<host>:5432/mycelium" -v ON_ERROR_STOP=1 \
--             -c "SET app.current_org = '<org-uuid>'" -f deploy/diagnose_tag_cardinality.sql
--
--      Caveat for mode 2: cross-org metrics (a tag borrowed from another org) are
--      structurally invisible under RLS and will read 0. Q1 flags that case.
--
-- OUTPUT: no names, no personal data -- ids and counts only, so the result can be
-- pasted into a ticket. Rows with a zero count are suppressed.

\set ON_ERROR_STOP on
\timing on
\pset null '(null)'

BEGIN;
SET TRANSACTION READ ONLY;
SET LOCAL statement_timeout = '10min';


-- ===========================================================================
-- Q0. Guard: refuse to produce misleadingly empty output.
-- ===========================================================================
DO $$
DECLARE
  v_bypass boolean;
  v_org    text := NULLIF(current_setting('app.current_org', true), '');
BEGIN
  SELECT rolsuper OR rolbypassrls INTO v_bypass
    FROM pg_roles WHERE rolname = current_user;

  IF NOT coalesce(v_bypass, false) AND v_org IS NULL THEN
    RAISE EXCEPTION
      'Role % is subject to RLS and app.current_org is unset: every query below '
      'would return 0 rows and look clean. Re-run as a superuser / BYPASSRLS role '
      'for cross-org numbers, or SET app.current_org = ''<org-uuid>'' first.',
      current_user;
  END IF;

  IF coalesce(v_bypass, false) THEN
    RAISE NOTICE 'Scope: ALL organizations (role % bypasses RLS).', current_user;
  ELSE
    RAISE NOTICE 'Scope: single org % (RLS active); cross-org metrics will read 0.', v_org;
  END IF;
END
$$;


-- ===========================================================================
-- Q1. RLS blindness self-check.
--
-- A junction row whose tag / entity is invisible is silently dropped by the
-- inner joins below, which UNDER-counts every cardinality metric. Under a
-- BYPASSRLS role these must all be 0 (the FKs guarantee it); anything non-zero
-- means the numbers in Q2..Q6 are partial.
-- ===========================================================================
SELECT 'task_tags -> tags not visible'   AS check_name, count(*) AS n
  FROM task_tags tt LEFT JOIN tags tg ON tg.id = tt.tag_id
 WHERE tg.id IS NULL
UNION ALL
SELECT 'task_tags -> tasks not visible', count(*)
  FROM task_tags tt LEFT JOIN tasks t ON t.id = tt.task_id
 WHERE t.id IS NULL
UNION ALL
SELECT 'note_tags -> tags not visible', count(*)
  FROM note_tags nt LEFT JOIN tags tg ON tg.id = nt.tag_id
 WHERE tg.id IS NULL
UNION ALL
SELECT 'note_tags -> notes not visible', count(*)
  FROM note_tags nt LEFT JOIN notes n ON n.id = nt.note_id
 WHERE n.id IS NULL
UNION ALL
SELECT 'project_profile -> tags not visible', count(*)
  FROM project_profile pp LEFT JOIN tags tg ON tg.id = pp.tag_id
 WHERE tg.id IS NULL;


-- ===========================================================================
-- Q2. THE HEADLINE TABLE: violations per org, per entity type, per scope.
--
-- Scopes (a violation is counted in each scope it belongs to):
--   n_all    = every row, including soft-deleted
--   n_live   = deleted_at IS NULL
--   n_active = live AND NOT is_archived (AND, for notes, review_state IS DISTINCT
--              FROM 'proposed') -- i.e. what the UI/API actually serves, so this
--              is the column that sizes the user-visible damage
--
-- The 'ALL ORGS' row is the cross-tenant rollup.
-- ===========================================================================
WITH ent AS (
    -- Every task and note with its lifecycle scope flags.
    SELECT 'task'::text AS etype, t.id, t.org_id,
           (t.deleted_at IS NULL)                       AS is_live,
           (t.deleted_at IS NULL AND t.is_archived = false) AS is_active
      FROM tasks t
    UNION ALL
    SELECT 'note'::text, n.id, n.org_id,
           (n.deleted_at IS NULL),
           (n.deleted_at IS NULL
            AND n.is_archived = false
            AND n.review_state IS DISTINCT FROM 'proposed')
      FROM notes n
),
struct_tag AS (
    -- One row per (entity, structural tag) pair. Structural = the two kinds the
    -- invariants are about; 'generic' and 'memory_channel' are unconstrained.
    -- project_profile is joined unconditionally so a satellite attached to the
    -- WRONG kind still shows up (measured in Q5).
    SELECT 'task'::text          AS etype,
           tt.task_id            AS entity_id,
           tt.org_id             AS link_org_id,
           tg.id                 AS tag_id,
           tg.org_id             AS tag_org_id,
           tg.kind::text         AS kind,
           tg.status             AS tag_status,
           (pp.tag_id IS NOT NULL) AS has_project_profile,
           pp.client_tag_id      AS project_client_tag_id
      FROM task_tags tt
      JOIN tags tg           ON tg.id = tt.tag_id
      LEFT JOIN project_profile pp ON pp.tag_id = tg.id
     WHERE tg.kind IN ('client', 'project')
    UNION ALL
    SELECT 'note'::text,
           nt.note_id,
           nt.org_id,
           tg.id,
           tg.org_id,
           tg.kind::text,
           tg.status,
           (pp.tag_id IS NOT NULL),
           pp.client_tag_id
      FROM note_tags nt
      JOIN tags tg           ON tg.id = nt.tag_id
      LEFT JOIN project_profile pp ON pp.tag_id = tg.id
     WHERE tg.kind IN ('client', 'project')
),
entity_clients AS (
    -- The set of client tags each entity carries. NULL (no row) = no client tag.
    SELECT etype, entity_id, array_agg(tag_id) AS client_tag_ids
      FROM struct_tag
     WHERE kind = 'client'
     GROUP BY 1, 2
),
facts AS (
    -- One row per entity that carries at least one structural tag.
    SELECT e.org_id,
           s.etype,
           s.entity_id,
           e.is_live,
           e.is_active,
           count(*) FILTER (WHERE s.kind = 'client')  AS n_client,
           count(*) FILTER (WHERE s.kind = 'project') AS n_project,
           -- (c) the project declares a client that is NOT among the entity's
           -- client tags. Only decidable when the entity has a client tag.
           count(*) FILTER (
             WHERE s.kind = 'project'
               AND s.project_client_tag_id IS NOT NULL
               AND ec.client_tag_ids IS NOT NULL
               AND NOT (s.project_client_tag_id = ANY (ec.client_tag_ids))
           ) AS n_project_client_mismatch,
           -- (d) reached from the entity: the project has no client at all, so
           -- (c) is unverifiable and billing falls back to "no rate".
           count(*) FILTER (
             WHERE s.kind = 'project'
               AND s.has_project_profile
               AND s.project_client_tag_id IS NULL
           ) AS n_project_clientless,
           -- project-kind tag with no satellite row at all (worse than (d)).
           count(*) FILTER (WHERE s.kind = 'project' AND NOT s.has_project_profile)
             AS n_project_no_profile,
           -- structural tag borrowed from another org (RLS/tenant leak).
           count(*) FILTER (WHERE s.tag_org_id <> e.org_id) AS n_cross_org_tag,
           -- junction row stamped with an org that is not the entity's org.
           count(*) FILTER (WHERE s.link_org_id <> e.org_id) AS n_link_org_mismatch,
           -- archived client/project tag still attached to the entity: decides
           -- which duplicate a repair should keep.
           count(*) FILTER (WHERE s.tag_status <> 'active') AS n_archived_struct_tag
      FROM struct_tag s
      JOIN ent e            ON e.etype = s.etype AND e.id = s.entity_id
      LEFT JOIN entity_clients ec
                            ON ec.etype = s.etype AND ec.entity_id = s.entity_id
     GROUP BY 1, 2, 3, 4, 5
),
metric_def AS (
    SELECT * FROM (VALUES
      ( 0, 'TOTAL entities carrying >=1 client/project tag'),
      ( 1, 'BASELINE clean: exactly 1 client + 1 project, consistent'),
      (10, '(a)/(b) VIOLATION: more than one CLIENT tag'),
      (11, '(a)/(b) VIOLATION: more than one PROJECT tag'),
      (20, '(c)   VIOLATION: project''s client not among entity''s client tags'),
      (21, '(c)   UNDECIDABLE: project tag whose project has NO client'),
      (22, '(d)   project-kind tag with NO project_profile row'),
      (30, 'SHAPE: project tag but NO client tag'),
      (31, 'SHAPE: client tag but NO project tag'),
      (40, 'TENANT: structural tag belongs to another org'),
      (41, 'TENANT: junction org_id <> entity org_id'),
      (50, 'HYGIENE: carries an ARCHIVED client/project tag')
    ) AS v(ord, label)
),
checks AS (
    SELECT f.org_id, f.etype, f.is_live, f.is_active, m.ord, m.label,
           CASE m.ord
             WHEN  0 THEN true
             WHEN  1 THEN f.n_client = 1 AND f.n_project = 1
                          AND f.n_project_client_mismatch = 0
                          AND f.n_project_clientless = 0
                          AND f.n_project_no_profile = 0
             WHEN 10 THEN f.n_client  > 1
             WHEN 11 THEN f.n_project > 1
             WHEN 20 THEN f.n_project_client_mismatch > 0
             WHEN 21 THEN f.n_project_clientless > 0
             WHEN 22 THEN f.n_project_no_profile > 0
             WHEN 30 THEN f.n_project > 0 AND f.n_client = 0
             WHEN 31 THEN f.n_client > 0 AND f.n_project = 0
             WHEN 40 THEN f.n_cross_org_tag > 0
             WHEN 41 THEN f.n_link_org_mismatch > 0
             WHEN 50 THEN f.n_archived_struct_tag > 0
             ELSE false
           END AS hit
      FROM facts f CROSS JOIN metric_def m
)
SELECT CASE WHEN grouping(org_id) = 1 THEN 'ALL ORGS' ELSE org_id::text END AS org,
       etype,
       label                                              AS metric,
       count(*) FILTER (WHERE hit)                        AS n_all,
       count(*) FILTER (WHERE hit AND is_live)            AS n_live,
       count(*) FILTER (WHERE hit AND is_active)          AS n_active
  FROM checks
 GROUP BY GROUPING SETS ((org_id, etype, ord, label), (etype, ord, label))
HAVING count(*) FILTER (WHERE hit) > 0
 ORDER BY grouping(org_id) DESC, org, etype, ord;


-- ===========================================================================
-- Q3. Worst-case cardinality per org: how deep does the duplication go?
-- (max tags on a single entity -- tells you whether a repair is "pick one of 2"
-- or "reconstruct intent from 7")
-- ===========================================================================
WITH ent AS (
    SELECT 'task'::text AS etype, t.id, t.org_id,
           (t.deleted_at IS NULL AND t.is_archived = false) AS is_active
      FROM tasks t
    UNION ALL
    SELECT 'note'::text, n.id, n.org_id,
           (n.deleted_at IS NULL AND n.is_archived = false
            AND n.review_state IS DISTINCT FROM 'proposed')
      FROM notes n
),
struct_tag AS (
    SELECT 'task'::text AS etype, tt.task_id AS entity_id, tg.kind::text AS kind
      FROM task_tags tt JOIN tags tg ON tg.id = tt.tag_id
     WHERE tg.kind IN ('client', 'project')
    UNION ALL
    SELECT 'note'::text, nt.note_id, tg.kind::text
      FROM note_tags nt JOIN tags tg ON tg.id = nt.tag_id
     WHERE tg.kind IN ('client', 'project')
),
per_entity AS (
    SELECT e.org_id, s.etype, e.is_active,
           count(*) FILTER (WHERE s.kind = 'client')  AS n_client,
           count(*) FILTER (WHERE s.kind = 'project') AS n_project
      FROM struct_tag s
      JOIN ent e ON e.etype = s.etype AND e.id = s.entity_id
     GROUP BY 1, 2, 3, s.entity_id
)
SELECT org_id::text AS org, etype,
       max(n_client)                            AS max_client_tags,
       max(n_project)                           AS max_project_tags,
       max(n_client) FILTER (WHERE is_active)   AS max_client_tags_active,
       max(n_project) FILTER (WHERE is_active)  AS max_project_tags_active
  FROM per_entity
 GROUP BY 1, 2
 ORDER BY 1, 2;


-- ===========================================================================
-- Q4. Invariant (d) at the source: project_profile rows with no client.
--
-- `n_referencing_active_entities` says whether the clientless project is dead
-- config or actively in use (the latter is what breaks billing: services/
-- time_tracking.py::_rate returns "no rate, EUR, billable" whenever
-- project_profile.client_tag_id IS NULL).
-- ===========================================================================
SELECT pp.org_id::text AS org,
       count(*)                                              AS projects_total,
       count(*) FILTER (WHERE pp.client_tag_id IS NULL)       AS projects_clientless,
       count(*) FILTER (WHERE pp.client_tag_id IS NULL
                          AND tg.status = 'active')           AS projects_clientless_active_tag,
       count(*) FILTER (WHERE pp.client_tag_id IS NOT NULL
                          AND ctg.id IS NULL)                 AS client_ptr_dangling,
       count(*) FILTER (WHERE pp.client_tag_id IS NOT NULL
                          AND ctg.kind::text <> 'client')     AS client_ptr_wrong_kind,
       count(*) FILTER (WHERE pp.client_tag_id IS NOT NULL
                          AND ctg.org_id <> pp.org_id)        AS client_ptr_cross_org
  FROM project_profile pp
  LEFT JOIN tags tg  ON tg.id  = pp.tag_id
  LEFT JOIN tags ctg ON ctg.id = pp.client_tag_id
 GROUP BY 1
 ORDER BY 1;

-- Which clientless projects are actually in use (ids only).
SELECT pp.org_id::text AS org,
       pp.tag_id       AS project_tag_id,
       (SELECT count(*) FROM task_tags tt JOIN tasks t ON t.id = tt.task_id
         WHERE tt.tag_id = pp.tag_id AND t.deleted_at IS NULL) AS live_tasks,
       (SELECT count(*) FROM note_tags nt JOIN notes n ON n.id = nt.note_id
         WHERE nt.tag_id = pp.tag_id AND n.deleted_at IS NULL) AS live_notes
  FROM project_profile pp
 WHERE pp.client_tag_id IS NULL
 ORDER BY 3 DESC, 4 DESC, 1, 2
 LIMIT 50;


-- ===========================================================================
-- Q5. Satellite integrity: does every client/project tag have the right
-- satellite row, and only that one?
-- ===========================================================================
SELECT tg.org_id::text AS org,
       count(*) FILTER (WHERE tg.kind::text = 'client')  AS client_tags,
       count(*) FILTER (WHERE tg.kind::text = 'project') AS project_tags,
       count(*) FILTER (WHERE tg.kind::text = 'client'  AND cp.tag_id IS NULL)
         AS client_tag_missing_profile,
       count(*) FILTER (WHERE tg.kind::text = 'project' AND pp.tag_id IS NULL)
         AS project_tag_missing_profile,
       count(*) FILTER (WHERE tg.kind::text <> 'client'  AND cp.tag_id IS NOT NULL)
         AS client_profile_on_wrong_kind,
       count(*) FILTER (WHERE tg.kind::text <> 'project' AND pp.tag_id IS NOT NULL)
         AS project_profile_on_wrong_kind,
       count(*) FILTER (WHERE cp.tag_id IS NOT NULL AND pp.tag_id IS NOT NULL)
         AS both_satellites,
       count(*) FILTER (WHERE cp.tag_id IS NOT NULL AND cp.org_id <> tg.org_id)
         AS client_profile_org_mismatch,
       count(*) FILTER (WHERE pp.tag_id IS NOT NULL AND pp.org_id <> tg.org_id)
         AS project_profile_org_mismatch
  FROM tags tg
  LEFT JOIN client_profile  cp ON cp.tag_id = tg.id
  LEFT JOIN project_profile pp ON pp.tag_id = tg.id
 GROUP BY 1
 ORDER BY 1;


-- ===========================================================================
-- Q6. EXAMPLES -- ids only, no titles, no client names. The worst offenders
-- first, so a human can eyeball a handful before deciding the repair rule.
--
-- `winner_by_id` is the tag services/time_tracking.py::_rate would pick today
-- (ORDER BY Tag.id LIMIT 1); services/notes.py::project_tag_for_note has NO
-- ORDER BY at all, so the note side is plan-dependent and can flip between
-- runs. Worth knowing before repairing: the "current" project is not stable.
-- ===========================================================================
WITH ent AS (
    SELECT 'task'::text AS etype, t.id, t.org_id,
           (t.deleted_at IS NULL) AS is_live,
           (t.deleted_at IS NULL AND t.is_archived = false) AS is_active
      FROM tasks t
    UNION ALL
    SELECT 'note'::text, n.id, n.org_id,
           (n.deleted_at IS NULL),
           (n.deleted_at IS NULL AND n.is_archived = false
            AND n.review_state IS DISTINCT FROM 'proposed')
      FROM notes n
),
struct_tag AS (
    SELECT 'task'::text AS etype, tt.task_id AS entity_id, tg.id AS tag_id,
           tg.kind::text AS kind, pp.client_tag_id AS project_client_tag_id,
           (pp.tag_id IS NOT NULL) AS has_project_profile
      FROM task_tags tt
      JOIN tags tg ON tg.id = tt.tag_id
      LEFT JOIN project_profile pp ON pp.tag_id = tg.id
     WHERE tg.kind IN ('client', 'project')
    UNION ALL
    SELECT 'note'::text, nt.note_id, tg.id, tg.kind::text, pp.client_tag_id,
           (pp.tag_id IS NOT NULL)
      FROM note_tags nt
      JOIN tags tg ON tg.id = nt.tag_id
      LEFT JOIN project_profile pp ON pp.tag_id = tg.id
     WHERE tg.kind IN ('client', 'project')
),
entity_clients AS (
    SELECT etype, entity_id, array_agg(tag_id) AS client_tag_ids
      FROM struct_tag WHERE kind = 'client' GROUP BY 1, 2
),
offenders AS (
    SELECT e.org_id, s.etype, s.entity_id, e.is_live, e.is_active,
           count(*) FILTER (WHERE s.kind = 'client')  AS n_client,
           count(*) FILTER (WHERE s.kind = 'project') AS n_project,
           array_agg(s.tag_id ORDER BY s.tag_id) FILTER (WHERE s.kind = 'client')
             AS client_tag_ids,
           array_agg(s.tag_id ORDER BY s.tag_id) FILTER (WHERE s.kind = 'project')
             AS project_tag_ids,
           min(s.tag_id) FILTER (WHERE s.kind = 'project') AS winner_by_id,
           count(*) FILTER (
             WHERE s.kind = 'project'
               AND s.project_client_tag_id IS NOT NULL
               AND ec.client_tag_ids IS NOT NULL
               AND NOT (s.project_client_tag_id = ANY (ec.client_tag_ids))
           ) AS n_project_client_mismatch,
           count(*) FILTER (WHERE s.kind = 'project' AND NOT s.has_project_profile)
             AS n_project_no_profile,
           count(*) FILTER (WHERE s.kind = 'project' AND s.has_project_profile
                              AND s.project_client_tag_id IS NULL)
             AS n_project_clientless
      FROM struct_tag s
      JOIN ent e ON e.etype = s.etype AND e.id = s.entity_id
      LEFT JOIN entity_clients ec ON ec.etype = s.etype AND ec.entity_id = s.entity_id
     GROUP BY 1, 2, 3, 4, 5
)
SELECT org_id::text AS org, etype, entity_id, is_live, is_active,
       n_client, n_project,
       n_project_client_mismatch AS mismatch,
       n_project_clientless      AS proj_no_client,
       n_project_no_profile      AS proj_no_profile,
       client_tag_ids, project_tag_ids, winner_by_id
  FROM offenders
 WHERE n_client > 1
    OR n_project > 1
    OR n_project_client_mismatch > 0
    OR n_project_no_profile > 0
 ORDER BY is_active DESC,
          (n_client + n_project) DESC,
          n_project_client_mismatch DESC,
          org, etype, entity_id
 LIMIT 40;


-- ===========================================================================
-- Q7. MONEY EXPOSURE: time booked on tasks that violate (a) or (c).
--
-- Why this is not cosmetic. Two DIFFERENT client-resolution paths coexist:
--
--   * services/time_tracking.py::time_report, the group_by='client' branch
--     (the ``for tag_id, name in tags`` loop) fans each time entry out over
--     EVERY client-kind tag on the task -> a task with two client tags adds
--     its full duration to BOTH clients. `client_axis_excess_seconds` below
--     is exactly the inflation that report currently shows.
--   * services/time_tracking.py::_rate and ::resolve_task_contexts resolve
--     the client as project -> project_profile.client_tag_id, IGNORING the
--     task's own client tag. So for a (c)-violating task the rate/invoice
--     path and the client report attribute the same seconds to two
--     different clients.
-- ===========================================================================
WITH struct_tag AS (
    SELECT tt.task_id, tg.id AS tag_id, tg.kind::text AS kind, pp.client_tag_id
      FROM task_tags tt
      JOIN tags tg ON tg.id = tt.tag_id
      LEFT JOIN project_profile pp ON pp.tag_id = tg.id
     WHERE tg.kind IN ('client', 'project')
),
task_clients AS (
    SELECT task_id, array_agg(tag_id) AS client_tag_ids
      FROM struct_tag WHERE kind = 'client' GROUP BY 1
),
facts AS (
    SELECT t.org_id, t.id AS task_id,
           count(*) FILTER (WHERE s.kind = 'client')  AS n_client,
           count(*) FILTER (WHERE s.kind = 'project') AS n_project,
           count(*) FILTER (
             WHERE s.kind = 'project'
               AND s.client_tag_id IS NOT NULL
               AND tc.client_tag_ids IS NOT NULL
               AND NOT (s.client_tag_id = ANY (tc.client_tag_ids))
           ) AS n_mismatch
      FROM struct_tag s
      JOIN tasks t ON t.id = s.task_id
      LEFT JOIN task_clients tc ON tc.task_id = s.task_id
     WHERE t.deleted_at IS NULL
     GROUP BY 1, 2
),
booked AS (
    SELECT f.org_id, f.task_id, f.n_client, f.n_project, f.n_mismatch,
           coalesce(sum(te.duration_seconds), 0) AS secs,
           coalesce(sum(te.duration_seconds) FILTER (WHERE te.billable), 0) AS billable_secs,
           count(te.id) AS entries
      FROM facts f
      LEFT JOIN time_entries te ON te.task_id = f.task_id
     GROUP BY 1, 2, 3, 4, 5
)
SELECT org_id::text AS org,
       sum(billable_secs)                                          AS billable_secs_total,
       sum(billable_secs) FILTER (WHERE n_client > 1)               AS on_multi_client_tasks,
       sum(billable_secs * (n_client - 1)) FILTER (WHERE n_client > 1)
         AS client_axis_excess_seconds,
       sum(billable_secs) FILTER (WHERE n_project > 1)              AS on_multi_project_tasks,
       sum(billable_secs * (n_project - 1)) FILTER (WHERE n_project > 1)
         AS project_axis_excess_seconds,
       sum(billable_secs) FILTER (WHERE n_mismatch > 0)             AS on_mismatched_tasks,
       count(*) FILTER (WHERE n_client > 1 AND entries > 0)         AS multi_client_tasks_with_time,
       count(*) FILTER (WHERE n_mismatch > 0 AND entries > 0)       AS mismatched_tasks_with_time
  FROM booked
 GROUP BY 1
 ORDER BY 1;

COMMIT;
