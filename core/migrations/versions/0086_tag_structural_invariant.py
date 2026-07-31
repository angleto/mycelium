"""Structural tag invariant: repair the violating rows, then guard them
in the database (ADR-0003 / ADR-0007 / ADR-0021).

The invariant, over the ONE ``tags`` table and its ``kind``:

  (a) a TASK carries exactly one ``client`` tag and exactly one
      ``project`` tag;
  (b) a NOTE carries exactly one ``client`` tag and AT MOST one
      ``project`` tag -- a projectless note is a first-class retrieval
      perimeter (``memory_blobs.project_id`` NULL, ADR-0021), not an
      asymmetry waiting to be "fixed";
  (c) when an entity carries a project tag, its client tag MUST be that
      project's ``project_profile.client_tag_id``;
  (d) every project has exactly one client (``client_tag_id`` NOT NULL).

``generic`` / ``memory_channel`` tags stay unconstrained many-to-many,
and ``memory_blob_tags`` is out of scope (blob consolidation unions
member tags on purpose; the authoritative perimeter is the scalar
``memory_blobs.project_id``).

REPAIR RULE -- THE PROJECT IS TRUTH. Per entity the surviving project is
the more specific one (the workspace default ``General`` loses whenever
another project is also attached), ties broken by ``tags.created_at``
then ``tags.id`` (``task_tags`` / ``note_tags`` carry neither an id nor
a timestamp, so the survivor can only be picked by joining ``tags``).
The client is then DERIVED from that project, and every other structural
junction row of the entity is dropped. Soft-deleted rows are repaired
too: ``restore_task`` can walk a hidden violation back into the live set.

ATTACHMENT KEYS: ``services/attachments.py`` embeds the resolved client
id in the storage key (``org/<org>/client/<client>/...``). Every entity
whose client changed here is printed at the end of ``upgrade()``; those
attachments must be re-keyed afterwards with
``core/src/mycelium_core/rekey_attachments.py``, which compares every
row against the key ``attachments._build_storage_key`` would give it
today and therefore does move a hierarchical key filed under the
PREVIOUS client, not only a flat legacy one.

KNOWN FAIL-OPEN: the guards are SECURITY INVOKER constraint triggers,
so they fire at COMMIT under the caller's RLS. A transaction that
switches ``app.current_org`` between orgs and commits once leaves the
earlier orgs' entities invisible to the check, which then returns early
exactly as it does for an already-purged parent. Every request-scoped
path pins one org for the whole transaction, so this only affects
multi-tenant batch jobs; the choke point
(``services/tag_assignment``) remains the primary enforcement.

DOWNGRADE: the schema change is fully reversed; the DATA repair is NOT.
Dropped junction rows, re-pointed clients and re-scoped blobs are lost
for good, exactly like 0022's fold.

Revision ID: 0086
Revises: 0085
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0086"
down_revision: str | None = "0085"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The migration role owns these tables and FORCE RLS applies to the
# owner too, with no ``app.current_org`` GUC during a migration: a
# cross-org statement would silently match ZERO rows. 0011 shipped
# exactly that bug and 0013 had to recover the data. ``ONLY`` mirrors
# the baseline literals (``memory_blobs`` is a hash-partitioned parent
# whose partitions carry no RLS of their own).
_RLS_TABLES = (
    "tags",
    "tasks",
    "notes",
    "task_tags",
    "note_tags",
    "project_profile",
    "client_profile",
    # Not in the invariant, but the repair re-derives the note
    # perimeter (memory_blobs.project_id) and prunes stale structural
    # blob tags, so both must be visible.
    "memory_blobs",
    "memory_blob_tags",
)

# The default tags are identified by their NATURAL key
# (``uq_tags(org_id, kind, name)``), the same key
# ``taxonomy._ensure_default_tag`` is idempotent on. The
# ``organizations.settings`` pointer is only a cache and can be absent
# or stale, so it is deliberately not consulted here.
_DEFAULT_CLIENT_NAME = "Personal"
_DEFAULT_PROJECT_NAME = "General"

# A full id dump would drown the migration log on a large tenant; the
# count is always exact, the listing is capped.
_MAX_PRINTED_IDS = 200


def _print_ids(label: str, rows: Sequence[sa.Row[Any]]) -> None:
    print(f"0086: {label}: {len(rows)}")
    for row in rows[:_MAX_PRINTED_IDS]:
        # An "X -> X" arrow reads as a change that never happened; the
        # ambiguous bucket below is printed with the client stated once.
        where = (
            f"client {row.new_client_tag_id}"
            if row.old_client_tag_id == row.new_client_tag_id
            else f"client {row.old_client_tag_id} -> {row.new_client_tag_id}"
        )
        print(f"0086:   {row.entity} {row.entity_id} {where}")
    if len(rows) > _MAX_PRINTED_IDS:
        print(f"0086:   ... and {len(rows) - _MAX_PRINTED_IDS} more")


def upgrade() -> None:
    conn = op.get_bind()
    for table in _RLS_TABLES:
        op.execute(f"ALTER TABLE ONLY {table} NO FORCE ROW LEVEL SECURITY")
    try:
        _repair(conn)
    finally:
        for table in _RLS_TABLES:
            op.execute(f"ALTER TABLE ONLY {table} FORCE ROW LEVEL SECURITY")
    _constrain()
    _guards()


# ------------------------------------------------------------- repair


def _repair(conn: sa.Connection) -> None:
    # A satellite profile that points at a tag of the wrong kind cannot
    # be expressed once the composite FKs below exist. Fail loudly and
    # legibly here rather than through an opaque FK violation on the
    # ALTER: deleting the profile would silently destroy billing config.
    for table, want in (("project_profile", "project"), ("client_profile", "client")):
        bad = (
            conn.execute(
                sa.text(
                    f"SELECT s.tag_id FROM {table} s JOIN tags t ON t.id = s.tag_id "
                    "WHERE t.kind <> CAST(:want AS tag_kind) LIMIT 20"
                ),
                {"want": want},
            )
            .scalars()
            .all()
        )
        if bad:
            raise RuntimeError(
                f"0086: {table} rows point at tags that are not kind={want}: "
                f"{[str(b) for b in bad]}. Re-point or remove them, then re-run."
            )

    # 1. The org default ``General`` project, for every org holding a
    #    task with no project tag at all (invariant (a) has no "none"
    #    case; taxonomy.ensure_default_project is the runtime analogue).
    n_project_tags = conn.execute(
        sa.text(
            """
            INSERT INTO tags (org_id, kind, name)
            SELECT DISTINCT tk.org_id, 'project'::tag_kind, CAST(:pname AS varchar)
              FROM tasks tk
             WHERE NOT EXISTS (
                   SELECT 1 FROM task_tags tt JOIN tags g ON g.id = tt.tag_id
                    WHERE tt.task_id = tk.id AND g.kind = 'project')
            ON CONFLICT ON CONSTRAINT uq_tags_org_id DO NOTHING
            """
        ),
        {"pname": _DEFAULT_PROJECT_NAME},
    ).rowcount

    # 2. (d) preparation: a client pointer aimed at a non-client tag is
    #    no pointer at all -- reset it and let the default fill it in.
    n_kind_reset = conn.execute(
        sa.text(
            """
            UPDATE project_profile pp
               SET client_tag_id = NULL, updated_at = now()
             WHERE pp.client_tag_id IS NOT NULL
               AND NOT EXISTS (SELECT 1 FROM tags t
                                WHERE t.id = pp.client_tag_id AND t.kind = 'client')
            """
        )
    ).rowcount

    # 3. A project tag with no satellite row is itself a (d) violation
    #    and would make the fail-closed trigger reject every entity
    #    carrying it. Materialise the row now; step 5 gives it a client.
    n_profiles = conn.execute(
        sa.text(
            """
            INSERT INTO project_profile (tag_id, org_id)
            SELECT t.id, t.org_id FROM tags t
             WHERE t.kind = 'project'
               AND NOT EXISTS (SELECT 1 FROM project_profile pp WHERE pp.tag_id = t.id)
            """
        )
    ).rowcount

    # 4. Orgs that genuinely need the default ``Personal`` client: one
    #    with a clientless project, or with a note carrying no
    #    structural tag at all and no Personal tag to fall back on. An
    #    org that needs neither is left untouched (no gratuitous tag).
    conn.execute(
        sa.text("CREATE TEMP TABLE tmp_0086_need_client (org_id uuid PRIMARY KEY) ON COMMIT DROP")
    )
    conn.execute(
        sa.text(
            """
            INSERT INTO tmp_0086_need_client (org_id)
            SELECT DISTINCT org_id FROM (
                SELECT pp.org_id FROM project_profile pp WHERE pp.client_tag_id IS NULL
                UNION
                SELECT n.org_id FROM notes n
                 WHERE NOT EXISTS (
                       SELECT 1 FROM note_tags nt JOIN tags g ON g.id = nt.tag_id
                        WHERE nt.note_id = n.id AND g.kind IN ('client', 'project'))
                   AND NOT EXISTS (
                       SELECT 1 FROM tags g
                        WHERE g.org_id = n.org_id AND g.kind = 'client' AND g.name = :cname)
            ) s
            """
        ),
        {"cname": _DEFAULT_CLIENT_NAME},
    )
    n_client_tags = conn.execute(
        sa.text(
            """
            INSERT INTO tags (org_id, kind, name)
            SELECT o.org_id, 'client'::tag_kind, CAST(:cname AS varchar)
              FROM tmp_0086_need_client o
            ON CONFLICT ON CONSTRAINT uq_tags_org_id DO NOTHING
            """
        ),
        {"cname": _DEFAULT_CLIENT_NAME},
    ).rowcount
    n_client_profiles = conn.execute(
        sa.text(
            """
            INSERT INTO client_profile (tag_id, org_id, legal_name)
            SELECT t.id, t.org_id, CAST(:cname AS varchar)
              FROM tags t JOIN tmp_0086_need_client o ON o.org_id = t.org_id
             WHERE t.kind = 'client' AND t.name = :cname
               AND NOT EXISTS (SELECT 1 FROM client_profile cp WHERE cp.tag_id = t.id)
            """
        ),
        {"cname": _DEFAULT_CLIENT_NAME},
    ).rowcount

    # 5. (d): every remaining clientless project belongs to Personal.
    n_defaulted = conn.execute(
        sa.text(
            """
            UPDATE project_profile pp
               SET client_tag_id = c.id, updated_at = now()
              FROM tags c
             WHERE pp.client_tag_id IS NULL
               AND c.org_id = pp.org_id AND c.kind = 'client' AND c.name = :cname
            """
        ),
        {"cname": _DEFAULT_CLIENT_NAME},
    ).rowcount
    orphan = conn.execute(
        sa.text("SELECT count(*) FROM project_profile WHERE client_tag_id IS NULL")
    ).scalar_one()
    if orphan:
        raise RuntimeError(
            f"0086: {orphan} project_profile rows still have no client after the "
            "default backfill; SET NOT NULL would fail. Investigate before re-running."
        )

    conn.execute(
        sa.text(
            """
            CREATE TEMP TABLE tmp_0086_client_moved (
                entity text NOT NULL,
                entity_id uuid NOT NULL,
                org_id uuid NOT NULL,
                old_client_tag_id uuid,
                new_client_tag_id uuid NOT NULL
            ) ON COMMIT DROP
            """
        )
    )
    _repair_tasks(conn)
    _repair_notes(conn)
    n_blobs, n_blob_tags = _repair_memory(conn)

    # ``tmp_0086_client_moved`` records every entity the repair TOUCHED
    # on the client axis. An entity that carried SEVERAL client tags,
    # one of them already the winner, lands there with old = new: its
    # client did not change, and sending it to the re-key pass costs a
    # full S3 copy for nothing. The operator's work list is therefore
    # the rows where the client GENUINELY differs, count and ids taken
    # from the same filtered read so they cannot disagree.
    moved = conn.execute(
        sa.text(
            "SELECT entity, entity_id, old_client_tag_id, new_client_tag_id "
            "  FROM tmp_0086_client_moved "
            " WHERE old_client_tag_id IS DISTINCT FROM new_client_tag_id "
            " ORDER BY entity, entity_id"
        )
    ).fetchall()
    # The residue is not provably a no-op either. The OLD storage key
    # was built by ``attachments._resolve_client_tag_id``, which is
    # LIMIT 1 with NO ORDER BY: for a multi-client entity it embedded
    # whichever client row the plan returned, not necessarily the one
    # reconstructed here. Printed apart because the REASON differs, not
    # because the remedy does: ``rekey_attachments`` compares each row
    # against the key it should have, so one plain run settles both
    # buckets and leaves whatever was already right untouched.
    ambiguous = conn.execute(
        sa.text(
            "SELECT entity, entity_id, old_client_tag_id, new_client_tag_id "
            "  FROM tmp_0086_client_moved "
            " WHERE old_client_tag_id IS NOT DISTINCT FROM new_client_tag_id "
            " ORDER BY entity, entity_id"
        )
    ).fetchall()
    print(
        f"0086: default project tags created={n_project_tags}, "
        f"default client tags created={n_client_tags}, "
        f"client profiles created={n_client_profiles}, "
        f"project profiles created={n_profiles}, "
        f"bogus client pointers reset={n_kind_reset}, "
        f"projects defaulted to {_DEFAULT_CLIENT_NAME}={n_defaulted}"
    )
    print(
        f"0086: memory blobs re-scoped={n_blobs}, stale structural blob tags pruned={n_blob_tags}"
    )
    _print_ids(
        "entities whose CLIENT changed -- re-key their attachments "
        "(mycelium_core.rekey_attachments)",
        moved,
    )
    _print_ids(
        "entities that carried SEVERAL client tags and KEPT this one -- "
        "client unchanged, old attachment key plan-dependent: the same "
        "re-key run settles these",
        ambiguous,
    )


def _repair_tasks(conn: sa.Connection) -> None:
    conn.execute(
        sa.text(
            """
            CREATE TEMP TABLE tmp_0086_task (
                task_id uuid PRIMARY KEY,
                org_id uuid NOT NULL,
                project_tag_id uuid NOT NULL,
                client_tag_id uuid NOT NULL
            ) ON COMMIT DROP
            """
        )
    )
    # The project is truth. ``(t.name = :pname)`` sorts FALSE first, so
    # the workspace default loses to any more specific project; the
    # single-project case is unaffected (one row is always rank 1).
    conn.execute(
        sa.text(
            """
            INSERT INTO tmp_0086_task (task_id, org_id, project_tag_id, client_tag_id)
            SELECT r.task_id, tk.org_id, r.tag_id, pp.client_tag_id
              FROM (
                    SELECT tt.task_id, tt.tag_id,
                           row_number() OVER (
                               PARTITION BY tt.task_id
                               ORDER BY (t.name = :pname), t.created_at, tt.tag_id
                           ) AS rn
                      FROM task_tags tt JOIN tags t ON t.id = tt.tag_id
                     WHERE t.kind = 'project'
                   ) r
              JOIN tasks tk ON tk.id = r.task_id
              JOIN project_profile pp ON pp.tag_id = r.tag_id
             WHERE r.rn = 1
            """
        ),
        {"pname": _DEFAULT_PROJECT_NAME},
    )
    conn.execute(
        sa.text(
            """
            INSERT INTO tmp_0086_task (task_id, org_id, project_tag_id, client_tag_id)
            SELECT tk.id, tk.org_id, p.id, pp.client_tag_id
              FROM tasks tk
              JOIN tags p ON p.org_id = tk.org_id AND p.kind = 'project' AND p.name = :pname
              JOIN project_profile pp ON pp.tag_id = p.id
             WHERE NOT EXISTS (
                   SELECT 1 FROM task_tags tt JOIN tags g ON g.id = tt.tag_id
                    WHERE tt.task_id = tk.id AND g.kind = 'project')
            """
        ),
        {"pname": _DEFAULT_PROJECT_NAME},
    )
    missing = conn.execute(
        sa.text(
            "SELECT count(*) FROM tasks tk "
            "WHERE NOT EXISTS (SELECT 1 FROM tmp_0086_task x WHERE x.task_id = tk.id)"
        )
    ).scalar_one()
    if missing:
        raise RuntimeError(
            f"0086: {missing} tasks resolved to no project; the AFTER INSERT guard "
            "would reject them. Investigate before re-running."
        )

    _record_moved(conn, entity="task")
    n_del = conn.execute(
        sa.text(
            """
            DELETE FROM task_tags tt
             USING tags g, tmp_0086_task x
             WHERE g.id = tt.tag_id
               AND tt.task_id = x.task_id
               AND g.kind IN ('client', 'project')
               AND tt.tag_id NOT IN (x.project_tag_id, x.client_tag_id)
            """
        )
    ).rowcount
    n_ins = conn.execute(
        sa.text(
            """
            INSERT INTO task_tags (task_id, tag_id, org_id)
            SELECT x.task_id, x.project_tag_id, x.org_id FROM tmp_0086_task x
            ON CONFLICT ON CONSTRAINT pk_task_tags DO NOTHING
            """
        )
    ).rowcount
    n_ins += conn.execute(
        sa.text(
            """
            INSERT INTO task_tags (task_id, tag_id, org_id)
            SELECT x.task_id, x.client_tag_id, x.org_id FROM tmp_0086_task x
            ON CONFLICT ON CONSTRAINT pk_task_tags DO NOTHING
            """
        )
    ).rowcount
    print(f"0086: task_tags structural rows deleted={n_del}, inserted={n_ins}")


def _repair_notes(conn: sa.Connection) -> None:
    # ``project_tag_id`` is nullable here and stays NULL for a note that
    # carries no project: (b) is AT MOST one, and a projectless note is
    # a deliberate perimeter (ADR-0021, core/tests/test_f6b_notes.py).
    conn.execute(
        sa.text(
            """
            CREATE TEMP TABLE tmp_0086_note (
                note_id uuid PRIMARY KEY,
                org_id uuid NOT NULL,
                project_tag_id uuid,
                client_tag_id uuid NOT NULL
            ) ON COMMIT DROP
            """
        )
    )
    conn.execute(
        sa.text(
            """
            INSERT INTO tmp_0086_note (note_id, org_id, project_tag_id, client_tag_id)
            SELECT r.note_id, n.org_id, r.tag_id, pp.client_tag_id
              FROM (
                    SELECT nt.note_id, nt.tag_id,
                           row_number() OVER (
                               PARTITION BY nt.note_id
                               ORDER BY (t.name = :pname), t.created_at, nt.tag_id
                           ) AS rn
                      FROM note_tags nt JOIN tags t ON t.id = nt.tag_id
                     WHERE t.kind = 'project'
                   ) r
              JOIN notes n ON n.id = r.note_id
              JOIN project_profile pp ON pp.tag_id = r.tag_id
             WHERE r.rn = 1
            """
        ),
        {"pname": _DEFAULT_PROJECT_NAME},
    )
    # No project: keep the oldest attached client, else the org default.
    # A note is NEVER given a project it did not have.
    conn.execute(
        sa.text(
            """
            INSERT INTO tmp_0086_note (note_id, org_id, project_tag_id, client_tag_id)
            SELECT n.id, n.org_id, NULL::uuid, COALESCE(oc.tag_id, dc.id)
              FROM notes n
              LEFT JOIN LATERAL (
                    SELECT nt.tag_id FROM note_tags nt JOIN tags g ON g.id = nt.tag_id
                     WHERE nt.note_id = n.id AND g.kind = 'client'
                     ORDER BY g.created_at, nt.tag_id LIMIT 1
                   ) oc ON true
              LEFT JOIN tags dc
                     ON dc.org_id = n.org_id AND dc.kind = 'client' AND dc.name = :cname
             WHERE NOT EXISTS (
                   SELECT 1 FROM note_tags nt JOIN tags g ON g.id = nt.tag_id
                    WHERE nt.note_id = n.id AND g.kind = 'project')
            """
        ),
        {"cname": _DEFAULT_CLIENT_NAME},
    )
    missing = conn.execute(
        sa.text(
            "SELECT count(*) FROM notes n "
            "WHERE NOT EXISTS (SELECT 1 FROM tmp_0086_note x WHERE x.note_id = n.id)"
        )
    ).scalar_one()
    if missing:
        raise RuntimeError(
            f"0086: {missing} notes resolved to no structural pair; the AFTER INSERT "
            "guard would reject them. Investigate before re-running."
        )

    _record_moved(conn, entity="note")
    n_del = conn.execute(
        sa.text(
            """
            DELETE FROM note_tags nt
             USING tags g, tmp_0086_note x
             WHERE g.id = nt.tag_id
               AND nt.note_id = x.note_id
               AND g.kind IN ('client', 'project')
               AND nt.tag_id <> x.client_tag_id
               AND (x.project_tag_id IS NULL OR nt.tag_id <> x.project_tag_id)
            """
        )
    ).rowcount
    n_ins = conn.execute(
        sa.text(
            """
            INSERT INTO note_tags (org_id, note_id, tag_id)
            SELECT x.org_id, x.note_id, x.client_tag_id FROM tmp_0086_note x
            ON CONFLICT ON CONSTRAINT pk_note_tags DO NOTHING
            """
        )
    ).rowcount
    n_ins += conn.execute(
        sa.text(
            """
            INSERT INTO note_tags (org_id, note_id, tag_id)
            SELECT x.org_id, x.note_id, x.project_tag_id FROM tmp_0086_note x
             WHERE x.project_tag_id IS NOT NULL
            ON CONFLICT ON CONSTRAINT pk_note_tags DO NOTHING
            """
        )
    ).rowcount
    print(f"0086: note_tags structural rows deleted={n_del}, inserted={n_ins}")


def _record_moved(conn: sa.Connection, *, entity: str) -> None:
    """Snapshot the entities whose client tagging is about to be
    rewritten, BEFORE the junction rewrite. ``old_client_tag_id`` is a
    RECONSTRUCTION: ``attachments._resolve_client_tag_id`` takes the
    first client tag it finds with no ORDER BY, so for an entity that
    carried several the key it produced is only guessed at here. The
    snapshot is therefore deliberately wide (anything but "exactly the
    target client, alone"); ``_repair`` splits it at print time into
    the genuine moves and the ambiguous residue."""
    if entity == "task":
        conn.execute(
            sa.text(
                """
                INSERT INTO tmp_0086_client_moved
                    (entity, entity_id, org_id, old_client_tag_id, new_client_tag_id)
                SELECT 'task'::text, x.task_id, x.org_id, cur.first_client, x.client_tag_id
                  FROM tmp_0086_task x
                  LEFT JOIN LATERAL (
                        SELECT count(*) AS n,
                               (array_agg(tt.tag_id ORDER BY g.created_at, tt.tag_id))[1]
                                   AS first_client
                          FROM task_tags tt JOIN tags g ON g.id = tt.tag_id
                         WHERE tt.task_id = x.task_id AND g.kind = 'client'
                       ) cur ON true
                 WHERE cur.n <> 1 OR cur.first_client IS DISTINCT FROM x.client_tag_id
                """
            )
        )
        return
    conn.execute(
        sa.text(
            """
            INSERT INTO tmp_0086_client_moved
                (entity, entity_id, org_id, old_client_tag_id, new_client_tag_id)
            SELECT 'note'::text, x.note_id, x.org_id, cur.first_client, x.client_tag_id
              FROM tmp_0086_note x
              LEFT JOIN LATERAL (
                    SELECT count(*) AS n,
                           (array_agg(nt.tag_id ORDER BY g.created_at, nt.tag_id))[1]
                               AS first_client
                      FROM note_tags nt JOIN tags g ON g.id = nt.tag_id
                     WHERE nt.note_id = x.note_id AND g.kind = 'client'
                   ) cur ON true
             WHERE cur.n <> 1 OR cur.first_client IS DISTINCT FROM x.client_tag_id
            """
        )
    )


def _repair_memory(conn: sa.Connection) -> tuple[int, int]:
    """SQL equivalent of ``note_search.rescope_note_blobs`` for every
    repaired note, plus the prune no code path performs.

    ``memory_blobs.project_id`` is the ADR-0007 RLS isolation predicate,
    so deciding which project wins MOVES content between perimeters --
    the blobs must follow the junction rewrite in the same transaction
    or a peer's search would keep seeing the old perimeter. Task blobs
    are always written with ``project_id=NULL`` (task_search: they live
    in the org-wide channel), so only note blobs are re-scoped.

    The two pointer tables are ENABLE-only RLS (0004 / 0040), never
    FORCE, so the owner role reads them across orgs without a bracket."""
    n_blobs = conn.execute(
        sa.text(
            """
            UPDATE memory_blobs b
               SET project_id = x.project_tag_id
              FROM note_part_index_pointer p
              JOIN tmp_0086_note x ON x.note_id = p.note_id
             WHERE b.id = p.blob_id AND b.org_id = p.org_id
               AND b.project_id IS DISTINCT FROM x.project_tag_id
            """
        )
    ).rowcount
    # Blob tags are copied from the parent at index time
    # (note_search._attach_inherited_tags / task_search's twin) and no
    # code path ever removes a stale one. Only pointer-backed blobs are
    # touched: a consolidated blob deliberately unions its members'
    # tags and is out of scope.
    n_tags = conn.execute(
        sa.text(
            """
            DELETE FROM memory_blob_tags mbt
             USING tags g, note_part_index_pointer p, tmp_0086_note x
             WHERE g.id = mbt.tag_id
               AND p.blob_id = mbt.blob_id AND p.org_id = mbt.org_id
               AND x.note_id = p.note_id
               AND g.kind IN ('client', 'project')
               AND mbt.tag_id <> x.client_tag_id
               AND (x.project_tag_id IS NULL OR mbt.tag_id <> x.project_tag_id)
            """
        )
    ).rowcount
    n_tags += conn.execute(
        sa.text(
            """
            DELETE FROM memory_blob_tags mbt
             USING tags g, task_index_pointer p, tmp_0086_task x
             WHERE g.id = mbt.tag_id
               AND p.blob_id = mbt.blob_id AND p.org_id = mbt.org_id
               AND x.task_id = p.task_id
               AND g.kind IN ('client', 'project')
               AND mbt.tag_id NOT IN (x.project_tag_id, x.client_tag_id)
            """
        )
    ).rowcount
    return n_blobs, n_tags


# --------------------------------------------------------- constraints


def _constrain() -> None:
    # The composite FKs below need a UNIQUE key on the referenced
    # (id, kind) pair. ``tags_pkey`` alone is not enough for a
    # multi-column reference.
    op.execute("ALTER TABLE tags ADD CONSTRAINT uq_tags_id_kind UNIQUE (id, kind)")

    # "The satellite points at a tag of the right kind" becomes
    # declarative: a constant, CHECKed kind column joined into a
    # composite FK. No trigger, no drift. ``tags.kind`` is immutable at
    # the service layer (taxonomy.update_tag writes only name / color /
    # status) so ON UPDATE CASCADE would be dead weight.
    op.execute(
        """
        ALTER TABLE project_profile
          ADD COLUMN tag_kind tag_kind NOT NULL DEFAULT 'project'
              CONSTRAINT ck_project_profile_tag_kind CHECK (tag_kind = 'project'),
          ADD COLUMN client_kind tag_kind NOT NULL DEFAULT 'client'
              CONSTRAINT ck_project_profile_client_kind CHECK (client_kind = 'client')
        """
    )
    op.execute(
        """
        ALTER TABLE project_profile
          ADD CONSTRAINT fk_project_profile_tag_kind
              FOREIGN KEY (tag_id, tag_kind) REFERENCES tags (id, kind) ON DELETE CASCADE,
          ADD CONSTRAINT fk_project_profile_client_kind
              FOREIGN KEY (client_tag_id, client_kind) REFERENCES tags (id, kind)
              ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED
        """
    )
    op.execute(
        """
        ALTER TABLE client_profile
          ADD COLUMN tag_kind tag_kind NOT NULL DEFAULT 'client'
              CONSTRAINT ck_client_profile_tag_kind CHECK (tag_kind = 'client')
        """
    )
    op.execute(
        """
        ALTER TABLE client_profile
          ADD CONSTRAINT fk_client_profile_tag_kind
              FOREIGN KEY (tag_id, tag_kind) REFERENCES tags (id, kind) ON DELETE CASCADE
        """
    )

    # The single-column pointer FK was ON DELETE SET NULL, which is
    # mutually exclusive with the NOT NULL that (d) demands. It is
    # re-added (rather than dropped in favour of the composite one)
    # because the model still declares ForeignKey("tags.id") and
    # autogenerate would keep proposing it back.
    #
    # NO ACTION, not RESTRICT: delete_organization
    # (0001_baseline.sql:620-658) is a single DELETE FROM organizations
    # that relies on CASCADE reaching both ``tags`` and
    # ``project_profile``. RESTRICT is checked BEFORE sibling cascade
    # actions run, so it would break workspace teardown; NO ACTION
    # DEFERRABLE is checked at COMMIT, by which time the cascade has
    # removed the referencing row.
    op.execute("ALTER TABLE project_profile DROP CONSTRAINT project_profile_client_tag_id_fkey")
    op.execute(
        """
        ALTER TABLE project_profile
          ADD CONSTRAINT project_profile_client_tag_id_fkey
              FOREIGN KEY (client_tag_id) REFERENCES tags (id)
              ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED
        """
    )
    op.execute("ALTER TABLE project_profile ALTER COLUMN client_tag_id SET NOT NULL")


# -------------------------------------------------------------- guards


# SECURITY INVOKER, never DEFINER: delete_organization's own comment
# (baseline:628-631) proves the owner role is FORCE-RLS-filtered on
# managed Postgres, so DEFINER would buy nothing and only hide the
# failure mode. The functions therefore see exactly what the caller
# sees, and the "parent is gone" early return is what keeps
# purge_project / purge_client / delete_organization working: the
# triggers are DEFERRED, they fire at COMMIT, long after the parent row
# was cascaded away.
_TASK_FN = """
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

# (b) is asymmetric ON PURPOSE: a projectless note is a first-class
# retrieval perimeter (memory_blobs.project_id NULL, ADR-0021, guarded
# by core/tests/test_f6b_notes.py:213-250). v_projects = 0 is legal.
_NOTE_FN = """
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

# taxonomy.update_project can break (c) for every dependent task and
# note WITHOUT touching a single junction row, so neither junction
# trigger would fire. This is the (c) guard for that path.
_PROJECT_FN = """
CREATE OR REPLACE FUNCTION assert_project_client_coherence() RETURNS trigger
    LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE
  v_bad uuid;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM tags WHERE id = NEW.tag_id) THEN
    RETURN NULL;
  END IF;
  SELECT tt.task_id INTO v_bad
    FROM task_tags tt
   WHERE tt.tag_id = NEW.tag_id
     AND NOT EXISTS (SELECT 1 FROM task_tags c
                      WHERE c.task_id = tt.task_id AND c.tag_id = NEW.client_tag_id)
   LIMIT 1;
  IF v_bad IS NOT NULL THEN
    RAISE EXCEPTION
      'tag.structural_invariant: task % still carries the previous client of project %',
      v_bad, NEW.tag_id USING ERRCODE = '23514';
  END IF;
  SELECT nt.note_id INTO v_bad
    FROM note_tags nt
   WHERE nt.tag_id = NEW.tag_id
     AND NOT EXISTS (SELECT 1 FROM note_tags c
                      WHERE c.note_id = nt.note_id AND c.tag_id = NEW.client_tag_id)
   LIMIT 1;
  IF v_bad IS NOT NULL THEN
    RAISE EXCEPTION
      'tag.structural_invariant: note % still carries the previous client of project %',
      v_bad, NEW.tag_id USING ERRCODE = '23514';
  END IF;
  RETURN NULL;
END
$$;
"""

_FUNCTIONS = (
    "assert_task_structural_tags",
    "assert_note_structural_tags",
    "assert_project_client_coherence",
)


def _guards() -> None:
    for body in (_TASK_FN, _NOTE_FN, _PROJECT_FN):
        op.execute(body)
    # Prod has PUBLIC EXECUTE revoked on public functions (see 0059);
    # the privilege is checked when the trigger is created, but an
    # explicit grant costs nothing and removes the doubt.
    for fn in _FUNCTIONS:
        op.execute(f"GRANT EXECUTE ON FUNCTION public.{fn}() TO mycelium_app")
    # DEFERRABLE INITIALLY DEFERRED throughout: the choke point
    # (services/tag_assignment.set_structural) legitimately passes
    # through an intermediate state while it swaps the pair, and an
    # entity is only ever consistent at the end of the transaction.
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_task_tags_structural
          AFTER INSERT OR UPDATE OR DELETE ON task_tags
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION assert_task_structural_tags()
        """
    )
    # A task inserted with zero junction rows fires no junction trigger
    # at all, so EXACTLY-ONE needs the parent-side trigger as well.
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_tasks_structural
          AFTER INSERT ON tasks
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION assert_task_structural_tags()
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_note_tags_structural
          AFTER INSERT OR UPDATE OR DELETE ON note_tags
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION assert_note_structural_tags()
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_notes_structural
          AFTER INSERT ON notes
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION assert_note_structural_tags()
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_project_profile_client_coherence
          AFTER UPDATE OF client_tag_id ON project_profile
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION assert_project_client_coherence()
        """
    )


def downgrade() -> None:
    """Full schema inverse. The DATA repair is NOT undone: dropped
    structural junction rows, re-derived clients and re-scoped memory
    blobs are gone for good (same posture as 0022's lossy fold)."""
    op.execute("DROP TRIGGER IF EXISTS trg_project_profile_client_coherence ON project_profile")
    op.execute("DROP TRIGGER IF EXISTS trg_notes_structural ON notes")
    op.execute("DROP TRIGGER IF EXISTS trg_note_tags_structural ON note_tags")
    op.execute("DROP TRIGGER IF EXISTS trg_tasks_structural ON tasks")
    op.execute("DROP TRIGGER IF EXISTS trg_task_tags_structural ON task_tags")
    for fn in _FUNCTIONS:
        op.execute(f"DROP FUNCTION IF EXISTS public.{fn}()")

    op.execute("ALTER TABLE project_profile ALTER COLUMN client_tag_id DROP NOT NULL")
    op.execute(
        "ALTER TABLE project_profile DROP CONSTRAINT IF EXISTS project_profile_client_tag_id_fkey"
    )
    op.execute(
        """
        ALTER TABLE project_profile
          ADD CONSTRAINT project_profile_client_tag_id_fkey
              FOREIGN KEY (client_tag_id) REFERENCES tags (id) ON DELETE SET NULL
        """
    )
    op.execute("ALTER TABLE client_profile DROP CONSTRAINT IF EXISTS fk_client_profile_tag_kind")
    op.execute("ALTER TABLE client_profile DROP COLUMN IF EXISTS tag_kind")
    op.execute(
        "ALTER TABLE project_profile DROP CONSTRAINT IF EXISTS fk_project_profile_client_kind"
    )
    op.execute("ALTER TABLE project_profile DROP CONSTRAINT IF EXISTS fk_project_profile_tag_kind")
    op.execute("ALTER TABLE project_profile DROP COLUMN IF EXISTS client_kind")
    op.execute("ALTER TABLE project_profile DROP COLUMN IF EXISTS tag_kind")
    op.execute("ALTER TABLE tags DROP CONSTRAINT IF EXISTS uq_tags_id_kind")
