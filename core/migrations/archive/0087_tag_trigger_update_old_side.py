"""Structural tag guards: on an UPDATE of a junction row, re-check the
OLD entity as well as the NEW one (docs/adr/0050, migration 0086).

0086 put ``assert_task_structural_tags()`` /
``assert_note_structural_tags()`` on ``task_tags`` / ``note_tags`` for
INSERT OR UPDATE OR DELETE, but both bodies resolve ONE entity id, and
on an UPDATE that id is NEW's. Re-pointing a junction row at another
entity (``UPDATE task_tags SET task_id = ...``) therefore left the
SOURCE entity unchecked: it could be stripped of its client or of its
project without the guard noticing, which is exactly what the DELETE
path -- the other way to take a row away from an entity -- does catch.

Nothing in the tree issues such an UPDATE: ``services/tag_assignment``,
the only writer of structural junction rows, deletes and inserts. So
this is latent, not live. It is still a hole worth closing, because the
DB layer exists precisely for the writers that never go through the
service layer (psql, an importer, a future bulk re-parent); a guard
that only holds for the code that already holds it guards nothing.

NO TRIGGER IS RECREATED HERE. ``pg_trigger`` references the function by
oid and CREATE OR REPLACE FUNCTION keeps that oid (along with the owner
and the EXECUTE grant of 0059/0086), so the five constraint triggers
keep firing as they are, still DEFERRABLE INITIALLY DEFERRED.

Everything else is 0086 verbatim: the "parent row is gone" early return
(what lets purge_project / purge_client / delete_organization commit --
the triggers fire at COMMIT, long after the CASCADEs removed the
parent), SECURITY INVOKER, ERRCODE 23514, the note asymmetry (a
projectless note is a first-class perimeter, ``v_projects = 0`` is
legal, ADR-0021), and the fail-closed join through ``project_profile``
(a project with no profile row can satisfy no entity).

``assert_project_client_coherence()`` is untouched: it is not a
junction guard, it hangs off ``AFTER UPDATE OF client_tag_id ON
project_profile`` and re-checks the dependents of NEW.tag_id, which is
that table's primary key.

DOWNGRADE restores 0086's bodies verbatim.

Revision ID: 0087
Revises: 0086
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0087"
down_revision: str | None = "0086"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# The only change from 0086: ``v_ids`` instead of a scalar ``v_id``,
# holding BOTH ends of an UPDATE that re-points the row. The loop body
# is 0086's, with the early return turned into a CONTINUE so that a
# purged OLD parent does not stop the NEW one from being checked.
# ``IS DISTINCT FROM`` keeps the common UPDATE (one that leaves
# ``task_id`` alone) at exactly one pass, as before.
_TASK_FN = """
CREATE OR REPLACE FUNCTION assert_task_structural_tags() RETURNS trigger
    LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE
  v_ids uuid[];
  v_id uuid;
  v_clients int;
  v_projects int;
  v_coherent int;
BEGIN
  IF TG_TABLE_NAME = 'tasks' THEN
    v_ids := ARRAY[NEW.id];
  ELSIF TG_OP = 'DELETE' THEN
    v_ids := ARRAY[OLD.task_id];
  ELSIF TG_OP = 'UPDATE' AND NEW.task_id IS DISTINCT FROM OLD.task_id THEN
    v_ids := ARRAY[OLD.task_id, NEW.task_id];
  ELSE
    v_ids := ARRAY[NEW.task_id];
  END IF;
  FOREACH v_id IN ARRAY v_ids LOOP
    IF NOT EXISTS (SELECT 1 FROM tasks WHERE id = v_id) THEN
      CONTINUE;
    END IF;
    SELECT count(*) FILTER (WHERE t.kind = 'client'),
           count(*) FILTER (WHERE t.kind = 'project')
      INTO v_clients, v_projects
      FROM task_tags tt JOIN tags t ON t.id = tt.tag_id
     WHERE tt.task_id = v_id AND t.kind IN ('client', 'project');
    IF v_clients <> 1 OR v_projects <> 1 THEN
      RAISE EXCEPTION
        'tag.structural_invariant: task % carries % client tag(s) and % '
        'project tag(s); exactly one of each is required',
        v_id, v_clients, v_projects USING ERRCODE = '23514';
    END IF;
    SELECT count(*) INTO v_coherent
      FROM task_tags tp
      JOIN tags gp ON gp.id = tp.tag_id AND gp.kind = 'project'
      JOIN project_profile pp ON pp.tag_id = tp.tag_id
      JOIN task_tags tc ON tc.task_id = tp.task_id AND tc.tag_id = pp.client_tag_id
     WHERE tp.task_id = v_id;
    IF v_coherent <> 1 THEN
      RAISE EXCEPTION
        'tag.structural_invariant: task % carries a client tag that is not '
        'the owning client of its project tag',
        v_id USING ERRCODE = '23514';
    END IF;
  END LOOP;
  RETURN NULL;
END
$$;
"""

# (b) stays asymmetric ON PURPOSE, on the OLD side too: moving a note's
# project row away leaves that note projectless, which is a legal
# personal perimeter (memory_blobs.project_id NULL, ADR-0021), not a
# violation. Moving its CLIENT row away is a violation, and that is the
# case 0086 could not see.
_NOTE_FN = """
CREATE OR REPLACE FUNCTION assert_note_structural_tags() RETURNS trigger
    LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE
  v_ids uuid[];
  v_id uuid;
  v_clients int;
  v_projects int;
  v_coherent int;
BEGIN
  IF TG_TABLE_NAME = 'notes' THEN
    v_ids := ARRAY[NEW.id];
  ELSIF TG_OP = 'DELETE' THEN
    v_ids := ARRAY[OLD.note_id];
  ELSIF TG_OP = 'UPDATE' AND NEW.note_id IS DISTINCT FROM OLD.note_id THEN
    v_ids := ARRAY[OLD.note_id, NEW.note_id];
  ELSE
    v_ids := ARRAY[NEW.note_id];
  END IF;
  FOREACH v_id IN ARRAY v_ids LOOP
    IF NOT EXISTS (SELECT 1 FROM notes WHERE id = v_id) THEN
      CONTINUE;
    END IF;
    SELECT count(*) FILTER (WHERE t.kind = 'client'),
           count(*) FILTER (WHERE t.kind = 'project')
      INTO v_clients, v_projects
      FROM note_tags nt JOIN tags t ON t.id = nt.tag_id
     WHERE nt.note_id = v_id AND t.kind IN ('client', 'project');
    IF v_clients <> 1 OR v_projects > 1 THEN
      RAISE EXCEPTION
        'tag.structural_invariant: note % carries % client tag(s) and % '
        'project tag(s); exactly one client and at most one project are required',
        v_id, v_clients, v_projects USING ERRCODE = '23514';
    END IF;
    IF v_projects = 1 THEN
      SELECT count(*) INTO v_coherent
        FROM note_tags np
        JOIN tags gp ON gp.id = np.tag_id AND gp.kind = 'project'
        JOIN project_profile pp ON pp.tag_id = np.tag_id
        JOIN note_tags nc ON nc.note_id = np.note_id AND nc.tag_id = pp.client_tag_id
       WHERE np.note_id = v_id;
      IF v_coherent <> 1 THEN
        RAISE EXCEPTION
          'tag.structural_invariant: note % carries a client tag that is not '
          'the owning client of its project tag',
          v_id USING ERRCODE = '23514';
      END IF;
    END IF;
  END LOOP;
  RETURN NULL;
END
$$;
"""

# ------------------------------------------------------- 0086 verbatim
#
# Copied unchanged from 0086_tag_structural_invariant.py so that
# ``downgrade()`` puts back the exact bodies this revision replaced;
# core/tests/test_migrations.py runs ``downgrade -1`` + ``upgrade head``
# on every head, so a paraphrase here fails CI.

_TASK_FN_0086 = """
CREATE OR REPLACE FUNCTION assert_task_structural_tags() RETURNS trigger
    LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE
  v_id uuid;
  v_clients int;
  v_projects int;
  v_coherent int;
BEGIN
  IF TG_TABLE_NAME = 'tasks' THEN
    v_id := NEW.id;
  ELSIF TG_OP = 'DELETE' THEN
    v_id := OLD.task_id;
  ELSE
    v_id := NEW.task_id;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM tasks WHERE id = v_id) THEN
    RETURN NULL;
  END IF;
  SELECT count(*) FILTER (WHERE t.kind = 'client'),
         count(*) FILTER (WHERE t.kind = 'project')
    INTO v_clients, v_projects
    FROM task_tags tt JOIN tags t ON t.id = tt.tag_id
   WHERE tt.task_id = v_id AND t.kind IN ('client', 'project');
  IF v_clients <> 1 OR v_projects <> 1 THEN
    RAISE EXCEPTION
      'tag.structural_invariant: task % carries % client tag(s) and % '
      'project tag(s); exactly one of each is required',
      v_id, v_clients, v_projects USING ERRCODE = '23514';
  END IF;
  SELECT count(*) INTO v_coherent
    FROM task_tags tp
    JOIN tags gp ON gp.id = tp.tag_id AND gp.kind = 'project'
    JOIN project_profile pp ON pp.tag_id = tp.tag_id
    JOIN task_tags tc ON tc.task_id = tp.task_id AND tc.tag_id = pp.client_tag_id
   WHERE tp.task_id = v_id;
  IF v_coherent <> 1 THEN
    RAISE EXCEPTION
      'tag.structural_invariant: task % carries a client tag that is not '
      'the owning client of its project tag',
      v_id USING ERRCODE = '23514';
  END IF;
  RETURN NULL;
END
$$;
"""

_NOTE_FN_0086 = """
CREATE OR REPLACE FUNCTION assert_note_structural_tags() RETURNS trigger
    LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE
  v_id uuid;
  v_clients int;
  v_projects int;
  v_coherent int;
BEGIN
  IF TG_TABLE_NAME = 'notes' THEN
    v_id := NEW.id;
  ELSIF TG_OP = 'DELETE' THEN
    v_id := OLD.note_id;
  ELSE
    v_id := NEW.note_id;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM notes WHERE id = v_id) THEN
    RETURN NULL;
  END IF;
  SELECT count(*) FILTER (WHERE t.kind = 'client'),
         count(*) FILTER (WHERE t.kind = 'project')
    INTO v_clients, v_projects
    FROM note_tags nt JOIN tags t ON t.id = nt.tag_id
   WHERE nt.note_id = v_id AND t.kind IN ('client', 'project');
  IF v_clients <> 1 OR v_projects > 1 THEN
    RAISE EXCEPTION
      'tag.structural_invariant: note % carries % client tag(s) and % '
      'project tag(s); exactly one client and at most one project are required',
      v_id, v_clients, v_projects USING ERRCODE = '23514';
  END IF;
  IF v_projects = 1 THEN
    SELECT count(*) INTO v_coherent
      FROM note_tags np
      JOIN tags gp ON gp.id = np.tag_id AND gp.kind = 'project'
      JOIN project_profile pp ON pp.tag_id = np.tag_id
      JOIN note_tags nc ON nc.note_id = np.note_id AND nc.tag_id = pp.client_tag_id
     WHERE np.note_id = v_id;
    IF v_coherent <> 1 THEN
      RAISE EXCEPTION
        'tag.structural_invariant: note % carries a client tag that is not '
        'the owning client of its project tag',
        v_id USING ERRCODE = '23514';
    END IF;
  END IF;
  RETURN NULL;
END
$$;
"""


def upgrade() -> None:
    op.execute(_TASK_FN)
    op.execute(_NOTE_FN)


def downgrade() -> None:
    op.execute(_TASK_FN_0086)
    op.execute(_NOTE_FN_0086)
