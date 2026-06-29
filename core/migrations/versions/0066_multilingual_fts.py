"""Per-row language for the stemmed FTS column (task b1baaf52).

Migration 0007 added ``memory_blobs.fts_lang`` as a tsvector GENERATED
``to_tsvector('italian', text)`` -- a single hardcoded dictionary that
mis-stems every non-Italian row. This makes the dictionary per-row.

A generated-column expression must be IMMUTABLE, but ``text::regconfig``
(the cast needed to drive ``to_tsvector`` from a column) is only STABLE
(a catalog lookup), so ``to_tsvector(lang::regconfig, text)`` is rejected
directly ("generation expression is not immutable"). The standard fix is
a thin IMMUTABLE SQL wrapper: text-search configs are effectively static,
so labelling the wrapper immutable is sound (the same assumption every
``to_tsvector`` expression index relies on). The cast raises on an
unknown config name, but ``fts_language`` is a closed domain -- NOT NULL
DEFAULT 'simple', and every writer goes through
``services.fts_language`` which only ever emits a valid config or
'simple' -- so the cast is always well-defined.

ADR-0015: the app role evaluates the generated-column expression in its
OWN role on INSERT/UPDATE, so it needs EXECUTE on the wrapper (prod
revokes the default PUBLIC execute; without the grant a blob write 500s
with "permission denied for function").

memory_blobs is PARTITION BY HASH (org_id); PG 16 propagates ADD/DROP
COLUMN and the parent CREATE INDEX to every partition (same mechanics as
migration 0007).

Revision ID: 0066
Revises: 0065
Create Date: 2026-06-29
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0066"
down_revision: str | None = "0065"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_FN = (
    "CREATE OR REPLACE FUNCTION fts_to_tsvector(lang text, document text)\n"
    "RETURNS tsvector LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$\n"
    "  SELECT to_tsvector(lang::regconfig, COALESCE(document, ''))\n"
    "$$"
)


def upgrade() -> None:
    # Immutable wrapper that lets a per-row regconfig drive a STORED
    # generated tsvector (see module docstring).
    op.execute(_FN)
    op.execute("GRANT EXECUTE ON FUNCTION fts_to_tsvector(text, text) TO mycelium_app")

    # Per-row text-search config. Existing rows must keep their current
    # italian stemming so recall does not regress -- achieved WITHOUT a
    # table rewrite: ADD COLUMN with a constant default is metadata-only in
    # PG 11+ (existing rows read 'italian' via the stored missing-value),
    # then flip the default to 'simple' (the safe no-stemming fallback) for
    # any future non-app insert. New app writes set the detected language
    # explicitly (services.fts_language).
    op.execute("ALTER TABLE memory_blobs ADD COLUMN fts_language text NOT NULL DEFAULT 'italian'")
    op.execute("ALTER TABLE memory_blobs ALTER COLUMN fts_language SET DEFAULT 'simple'")

    # Swap the hardcoded-italian generated column for the per-row one.
    op.execute("DROP INDEX IF EXISTS ix_memory_blobs_fts_lang")
    op.execute("ALTER TABLE memory_blobs DROP COLUMN fts_lang")
    op.execute(
        "ALTER TABLE memory_blobs ADD COLUMN fts_lang tsvector "
        "GENERATED ALWAYS AS (fts_to_tsvector(fts_language, text)) STORED"
    )
    op.execute("CREATE INDEX ix_memory_blobs_fts_lang ON memory_blobs USING gin (fts_lang)")


def downgrade() -> None:
    # Restore the migration-0007 hardcoded-italian generated column.
    op.execute("DROP INDEX IF EXISTS ix_memory_blobs_fts_lang")
    op.execute("ALTER TABLE memory_blobs DROP COLUMN fts_lang")
    op.execute(
        "ALTER TABLE memory_blobs ADD COLUMN fts_lang tsvector "
        "GENERATED ALWAYS AS (to_tsvector('italian', COALESCE(text, ''))) STORED"
    )
    op.execute("CREATE INDEX ix_memory_blobs_fts_lang ON memory_blobs USING gin (fts_lang)")
    op.execute("ALTER TABLE memory_blobs DROP COLUMN fts_language")
    op.execute("DROP FUNCTION IF EXISTS fts_to_tsvector(text, text)")
