"""Sync identities.handle to its source row and prettify legacy
UUID-derived assistant handles.

What 0099 left undone:

- 0099 picked a UUID-derived fallback (``_a_<short-uuid>``) for every
  ai_assistant whose handle was the empty-string sentinel. Safe but
  unfriendly: the picker chip renders the raw handle, so Claude
  surfaces as ``@_a_e593a0bb`` instead of ``@claude``. This migration
  re-slugifies those rows from their label, with per-org dedupe, and
  propagates the new handle to the matching identity row.
- 0099 also re-asserted identities for users via the 0084 INSERT
  pattern. That pattern uses ``ON CONFLICT (org_id, handle) DO
  NOTHING``, so a pre-existing identity row whose handle has drifted
  away from ``users.handle`` (e.g. user was renamed without an
  UPDATE trigger to keep identities in sync — the 0085 triggers fire
  on INSERT only) is silently left stale. The downstream resolver
  (``identities_svc.lookup_by_handle``) then misses the lookup and
  the PATCH /tasks call raises DomainError on what should be a
  trivial self-assign.

Fix here:

1. Re-slugify legacy ai_assistant handles (``_a_<8hex>``) from the
   label, per-org-deduped via a window function. Identity rows are
   updated together (the ``identities.handle`` UNIQUE(org_id, handle)
   constraint is satisfied because we only rename rows whose handle
   currently starts with ``_a_``).
2. For every (org x user) where identities.handle != users.handle,
   align identities.handle to users.handle (and drop any stranded
   row that loses uniqueness from the swap).
3. Same for ai_assistants → identities.handle alignment after step 1.
4. Add UPDATE triggers on ``users.handle`` and
   ``ai_assistants.handle`` so future renames stay in sync without a
   service-layer hook.

Revision: 0100
Down revision: 0099
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0100"
down_revision: str | None = "0099"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Step 1: pick a label-derived slug for each legacy assistant with a
# UUID handle. CTE shape: produce a candidate per row, dedupe within
# (org_id, candidate) via row_number(), suffix collisions with -N.
# Falls back to the existing UUID handle if the label is unusable
# (all-symbol, blank) — that's still better than failing the row.
_RESLUG_ASSISTANTS = """
WITH base AS (
  SELECT
    id,
    org_id,
    handle AS old_handle,
    NULLIF(
      btrim(
        substr(
          regexp_replace(lower(label), '[^a-z0-9]+', '-', 'g'),
          1,
          36
        ),
        '-'
      ),
      ''
    ) AS slug
  FROM ai_assistants
  WHERE handle LIKE '\\_a\\_%' ESCAPE '\\'
),
candidates AS (
  SELECT
    id,
    org_id,
    old_handle,
    slug,
    row_number() OVER (PARTITION BY org_id, slug ORDER BY id) AS n
  FROM base
  WHERE slug IS NOT NULL
),
final AS (
  SELECT
    id,
    org_id,
    old_handle,
    CASE
      WHEN n = 1 THEN slug
      ELSE substr(slug, 1, 36 - length(n::text) - 1) || '-' || n::text
    END AS new_handle
  FROM candidates
),
collision_free AS (
  SELECT f.*
  FROM final f
  WHERE NOT EXISTS (
    SELECT 1
    FROM ai_assistants a2
    WHERE a2.org_id = f.org_id
      AND a2.handle = f.new_handle
      AND a2.id <> f.id
  )
),
upd_assistant AS (
  UPDATE ai_assistants a
     SET handle = c.new_handle
    FROM collision_free c
   WHERE a.id = c.id
  RETURNING a.id, a.org_id, c.old_handle, c.new_handle
)
UPDATE identities i
   SET handle = u.new_handle
  FROM upd_assistant u
 WHERE i.ai_assistant_id = u.id
   AND i.org_id = u.org_id
   AND i.handle = u.old_handle;
"""


# Step 2: align identities.handle to users.handle for every
# (org x user) where they have drifted. Dedupe: if another identity
# in the same org already holds the target handle (rare, but the
# UNIQUE constraint would reject the UPDATE), delete the drifted
# row first — the next assignment will re-materialise via
# identities_svc.lookup_by_handle's self-heal.
_DROP_STRANDED_USER_IDENTITIES = """
DELETE FROM identities i
 WHERE i.kind = 'user'
   AND EXISTS (
     SELECT 1
       FROM users u
      WHERE u.id = i.user_id
        AND u.handle <> i.handle
   )
   AND EXISTS (
     SELECT 1
       FROM identities other
      WHERE other.org_id = i.org_id
        AND other.id <> i.id
        AND other.handle = (
          SELECT u.handle FROM users u WHERE u.id = i.user_id
        )
   );
"""

_ALIGN_USER_IDENTITIES = """
UPDATE identities i
   SET handle = u.handle
  FROM users u
 WHERE i.user_id = u.id
   AND i.kind = 'user'
   AND u.handle <> ''
   AND i.handle <> u.handle;
"""

# Step 2b: any (membership x user) with no identity at all gets one.
# Mirrors the 0084 backfill, idempotent.
_ENSURE_USER_IDENTITIES = """
INSERT INTO identities (org_id, kind, handle, user_id)
SELECT m.org_id, 'user', u.handle, u.id
FROM memberships m
JOIN users u ON u.id = m.user_id
WHERE u.handle <> ''
  AND NOT EXISTS (
    SELECT 1 FROM identities i
     WHERE i.org_id = m.org_id
       AND i.user_id = u.id
  )
ON CONFLICT (org_id, handle) DO NOTHING;
"""

# Step 3: same alignment for ai_assistants → identities.
_DROP_STRANDED_ASSISTANT_IDENTITIES = """
DELETE FROM identities i
 WHERE i.kind = 'ai_assistant'
   AND EXISTS (
     SELECT 1
       FROM ai_assistants a
      WHERE a.id = i.ai_assistant_id
        AND a.handle <> i.handle
   )
   AND EXISTS (
     SELECT 1
       FROM identities other
      WHERE other.org_id = i.org_id
        AND other.id <> i.id
        AND other.handle = (
          SELECT a.handle FROM ai_assistants a WHERE a.id = i.ai_assistant_id
        )
   );
"""

_ALIGN_ASSISTANT_IDENTITIES = """
UPDATE identities i
   SET handle = a.handle
  FROM ai_assistants a
 WHERE i.ai_assistant_id = a.id
   AND i.kind = 'ai_assistant'
   AND a.handle <> ''
   AND i.handle <> a.handle;
"""

_ENSURE_ASSISTANT_IDENTITIES = """
INSERT INTO identities (org_id, kind, handle, ai_assistant_id)
SELECT a.org_id, 'ai_assistant', a.handle, a.id
FROM ai_assistants a
WHERE a.handle <> ''
  AND NOT EXISTS (
    SELECT 1 FROM identities i
     WHERE i.org_id = a.org_id
       AND i.ai_assistant_id = a.id
  )
ON CONFLICT (org_id, handle) DO NOTHING;
"""


# Step 4: UPDATE triggers so renames propagate forward. SECURITY
# DEFINER mirrors the 0085 INSERT triggers — they fire from the
# admin_session provisioning paths and need to bypass the identities
# RLS policy.
_USER_UPDATE_TRIGGER = """
CREATE OR REPLACE FUNCTION sync_identity_on_user_handle_update()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $fn$
BEGIN
  IF NEW.handle IS NULL OR NEW.handle = '' THEN
    RETURN NEW;
  END IF;
  IF OLD.handle IS NOT DISTINCT FROM NEW.handle THEN
    RETURN NEW;
  END IF;
  UPDATE identities
     SET handle = NEW.handle
   WHERE user_id = NEW.id
     AND kind = 'user'
     AND NOT EXISTS (
       SELECT 1 FROM identities other
        WHERE other.org_id = identities.org_id
          AND other.id <> identities.id
          AND other.handle = NEW.handle
     );
  RETURN NEW;
END
$fn$
"""

_USER_UPDATE_TRIGGER_BIND = """
DROP TRIGGER IF EXISTS trg_sync_identity_on_user_handle_update ON users;
CREATE TRIGGER trg_sync_identity_on_user_handle_update
AFTER UPDATE OF handle ON users
FOR EACH ROW EXECUTE FUNCTION sync_identity_on_user_handle_update();
"""

_ASSISTANT_UPDATE_TRIGGER = """
CREATE OR REPLACE FUNCTION sync_identity_on_ai_assistant_handle_update()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $fn$
BEGIN
  IF NEW.handle IS NULL OR NEW.handle = '' THEN
    RETURN NEW;
  END IF;
  IF OLD.handle IS NOT DISTINCT FROM NEW.handle THEN
    RETURN NEW;
  END IF;
  UPDATE identities
     SET handle = NEW.handle
   WHERE ai_assistant_id = NEW.id
     AND kind = 'ai_assistant'
     AND NOT EXISTS (
       SELECT 1 FROM identities other
        WHERE other.org_id = identities.org_id
          AND other.id <> identities.id
          AND other.handle = NEW.handle
     );
  RETURN NEW;
END
$fn$
"""

_ASSISTANT_UPDATE_TRIGGER_BIND = """
DROP TRIGGER IF EXISTS trg_sync_identity_on_ai_assistant_handle_update ON ai_assistants;
CREATE TRIGGER trg_sync_identity_on_ai_assistant_handle_update
AFTER UPDATE OF handle ON ai_assistants
FOR EACH ROW EXECUTE FUNCTION sync_identity_on_ai_assistant_handle_update();
"""


UPGRADE: tuple[str, ...] = (
    _RESLUG_ASSISTANTS,
    _DROP_STRANDED_USER_IDENTITIES,
    _ALIGN_USER_IDENTITIES,
    _ENSURE_USER_IDENTITIES,
    _DROP_STRANDED_ASSISTANT_IDENTITIES,
    _ALIGN_ASSISTANT_IDENTITIES,
    _ENSURE_ASSISTANT_IDENTITIES,
    _USER_UPDATE_TRIGGER,
    _USER_UPDATE_TRIGGER_BIND,
    _ASSISTANT_UPDATE_TRIGGER,
    _ASSISTANT_UPDATE_TRIGGER_BIND,
)


DOWNGRADE: tuple[str, ...] = (
    "DROP TRIGGER IF EXISTS trg_sync_identity_on_ai_assistant_handle_update ON ai_assistants",
    "DROP FUNCTION IF EXISTS sync_identity_on_ai_assistant_handle_update()",
    "DROP TRIGGER IF EXISTS trg_sync_identity_on_user_handle_update ON users",
    "DROP FUNCTION IF EXISTS sync_identity_on_user_handle_update()",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
