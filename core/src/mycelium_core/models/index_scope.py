"""How a row relates to the automatic search index.

A row's title and body reach ``memory_blobs`` through the search
indexer without the writer ever asking for it, and a blob is org-wide
retrievable from ``memory_search`` and the unified ``/search``. This
enum is the writer's opt-out from that path, and nothing more.

``none`` is NOT a read boundary. The row stays readable org-wide
(``get_task`` selects on the primary key with no actor predicate; the
RLS policies carry ``org_id`` as their only term) and stays matchable
by the server-side ILIKE of ``list_tasks(q=...)`` / ``list_notes(q=...)``,
which never touch ``memory_blobs``. What it closes is unrequested
recall -- automatic indexing, semantic search, ``memory_search`` -- not
the deliberate query of an in-org actor who can already read the row.
Governed material therefore does not belong in a title or a body even
at ``none``.

Lives in its own module because both ``tasks`` and ``notes`` carry the
column: importing it from either model file would make one table's
module a dependency of the other's for no reason.
"""

from __future__ import annotations

import enum


class IndexScope(enum.StrEnum):
    org = "org"
    none = "none"
