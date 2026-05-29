"""One-shot, idempotent normalization of UUID-prefix references in note
bodies: wrap bare task/note codes in backticks so the SPA renders them
as clickable chips.

Roadmap / planning notes refer to tasks and notes by their 8-char UUID
prefix (e.g. ``66c5d15d``) or a full UUID. ADR-0038 chose the backtick
convention (`` `66c5d15d` ``) as the opt-in that the markdown renderer
and the in-editor decoration turn into clickable chips. Codes typed bare
in prose (``done/verify: 91cf6aaa, c8651969``) are inert. This pass
finds those bare codes and backticks the ones that actually resolve to
an existing task/note in the workspace.

Safety:

* **Resolver-gated.** A bare token is wrapped only if
  ``lookup.resolve_prefix`` returns at least one live match (same
  default scope the SPA chip uses: not archived, not deleted). Random
  hex that is not an entity -- commit SHAs, ``#ffc800`` colours, a
  password hash -- never resolves, so it is left untouched.
* **Markdown-aware.** Fenced code blocks are skipped entirely; inline
  code, links/images, autolinks and bare URLs are masked before
  matching, so a token already in backticks or inside a link target is
  never re-wrapped or broken.
* **Idempotent.** An already-backticked token is inside a masked inline
  code span, so a second run is a no-op. Safe to re-run.
* **Dry-run by default.** With no flag it only reports what it WOULD
  change (per part) and writes nothing. Pass ``--apply`` to persist via
  the canonical ``note_parts.update_part`` path (bumps version, records
  the note revision + audit entry).

Enumerates every workspace with the no-tenant admin session, then acts
inside each workspace's tenant session as its earliest owner, so all
writes satisfy RLS (ADR-0015) without bypassing it -- the same
cross-tenant pattern as ``migrate_attachments``.

Run (backend image, same DB URL as the app):

    python -m flow_core.normalize_note_uuids            # dry-run
    python -m flow_core.normalize_note_uuids --apply    # persist
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import uuid
from re import Match

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.db import admin_session, tenant_session
from flow_core.errors import DomainError
from flow_core.models.membership import Membership, Role
from flow_core.models.note import Note
from flow_core.models.organization import Organization
from flow_core.services import lookup as lookup_svc
from flow_core.services import note_parts as parts_svc

logger = logging.getLogger("flow.normalize_note_uuids")

# A fenced code block delimiter (``` or ~~~) at line start.
_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")

# Inline spans whose hex content must NOT be touched: inline code,
# links / images, autolinks, bare URLs. Masked before token matching.
_PROTECT_RE = re.compile(
    r"`[^`]*`"  # inline code
    r"|!?\[[^\]]*\]\([^)]*\)"  # [label](target) / ![alt](src)
    r"|<[^>\s]+>"  # <autolink>
    r"|https?://\S+"  # bare URL
)

# A bare entity code in prose: a full canonical UUID, or an 8-char hex
# run. Bounded so it never matches a sub-run of a longer hex/dashed
# token, a word, or something already backticked.
_TOKEN_RE = re.compile(
    r"(?<![0-9a-zA-Z`_-])"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{8})"
    r"(?![0-9a-zA-Z`_-])",
    re.IGNORECASE,
)

_MASK_RE = re.compile("\x00(\\d+)\x00")


def _mask_protected(line: str) -> tuple[str, list[str]]:
    """Replace protected inline spans with ``\\x00<i>\\x00`` placeholders
    (which can never match ``_TOKEN_RE``) and return the masked line plus
    the captured originals for later restoration."""
    saved: list[str] = []

    def repl(m: Match[str]) -> str:
        saved.append(m.group(0))
        return f"\x00{len(saved) - 1}\x00"

    return _PROTECT_RE.sub(repl, line), saved


def _unmask(line: str, saved: list[str]) -> str:
    return _MASK_RE.sub(lambda m: saved[int(m.group(1))], line)


def _collect_candidates(body: str) -> set[str]:
    """Every bare entity-code token (lowercased) in the prose of a part
    body, skipping fenced code and masked inline spans."""
    out: set[str] = set()
    in_fence = False
    for line in body.split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        masked, _ = _mask_protected(line)
        for m in _TOKEN_RE.finditer(masked):
            out.add(m.group(1).lower())
    return out


def _apply_backticks(body: str, allowed: set[str]) -> str:
    """Wrap every prose occurrence of a token in ``allowed`` in
    backticks, preserving fenced code and masked inline spans verbatim."""
    if not allowed:
        return body
    in_fence = False
    out: list[str] = []
    for line in body.split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        masked, saved = _mask_protected(line)

        def repl(m: Match[str]) -> str:
            tok = m.group(1)
            return f"`{tok}`" if tok.lower() in allowed else tok

        out.append(_unmask(_TOKEN_RE.sub(repl, masked), saved))
    return "\n".join(out)


async def _resolves(session: AsyncSession, token: str, cache: dict[str, bool]) -> bool:
    """True iff ``token`` is a prefix of at least one live task/note in
    the current tenant scope. Cached per run (the same code recurs across
    many parts)."""
    if token in cache:
        return cache[token]
    try:
        prefix = lookup_svc.normalise_prefix(token)
    except DomainError:
        cache[token] = False
        return False
    matches = await lookup_svc.resolve_prefix(session, prefix=prefix, kinds=("task", "note"))
    cache[token] = len(matches) > 0
    return cache[token]


async def _all_org_ids() -> list[uuid.UUID]:
    async with admin_session() as s:
        orgs = (await s.execute(select(Organization).order_by(Organization.id))).scalars().all()
    return [o.id for o in sorted(orgs, key=lambda o: str(o.id))]


async def _owner_of(org_id: uuid.UUID) -> uuid.UUID | None:
    """The earliest owner of the workspace, used as the actor for the
    tenant session so the writes satisfy RLS. ``None`` -> skip."""
    async with admin_session() as s:
        rows = (
            (
                await s.execute(
                    select(Membership)
                    .where(Membership.org_id == org_id, Membership.role == Role.owner)
                    .order_by(Membership.created_at, Membership.user_id)
                )
            )
            .scalars()
            .all()
        )
    if not rows:
        return None
    return sorted(rows, key=lambda m: (m.created_at, str(m.user_id)))[0].user_id


async def _normalize_org(org_id: uuid.UUID, actor_id: uuid.UUID, *, apply: bool) -> tuple[int, int]:
    """Normalize one workspace's note parts inside its tenant session.
    Returns ``(parts_changed, refs_wrapped)``."""
    parts_changed = 0
    refs_wrapped = 0
    cache: dict[str, bool] = {}
    async with tenant_session(str(org_id), str(actor_id)) as session:
        note_ids = (
            (await session.execute(select(Note.id).where(Note.org_id == org_id))).scalars().all()
        )
        for note_id in note_ids:
            parts = await parts_svc.list_parts(session, org_id=org_id, note_id=note_id)
            for part in parts:
                body = part.body or ""
                candidates = _collect_candidates(body)
                if not candidates:
                    continue
                allowed = {tok for tok in candidates if await _resolves(session, tok, cache)}
                if not allowed:
                    continue
                new_body = _apply_backticks(body, allowed)
                if new_body == body:
                    continue
                wrapped = (new_body.count("`") - body.count("`")) // 2
                parts_changed += 1
                refs_wrapped += wrapped
                logger.info(
                    "org %s note %s part %s: %d code ref(s)%s",
                    org_id,
                    note_id,
                    part.id,
                    wrapped,
                    "" if apply else " [dry-run]",
                )
                if apply:
                    await parts_svc.update_part(
                        session,
                        org_id=org_id,
                        actor_id=actor_id,
                        part_id=part.id,
                        expected_version=part.version,
                        body=new_body,
                        channel="system",
                    )
    return parts_changed, refs_wrapped


async def normalize_note_uuids(*, apply: bool) -> tuple[int, int]:
    """Backtick bare entity codes across every workspace. Returns
    ``(parts_changed, refs_wrapped)``."""
    parts_total = 0
    refs_total = 0
    for org_id in await _all_org_ids():
        owner = await _owner_of(org_id)
        if owner is None:
            logger.info("org %s has no owner; skipped", org_id)
            continue
        parts, refs = await _normalize_org(org_id, owner, apply=apply)
        parts_total += parts
        refs_total += refs
    logger.info(
        "note uuid normalization complete: %d ref(s) across %d part(s)%s",
        refs_total,
        parts_total,
        "" if apply else " [dry-run, nothing written]",
    )
    return parts_total, refs_total


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("FLOW_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    apply = "--apply" in sys.argv[1:]
    parts, refs = asyncio.run(normalize_note_uuids(apply=apply))
    mode = "applied" if apply else "DRY-RUN (no writes; pass --apply to persist)"
    sys.stdout.write(
        f"note uuid normalization [{mode}]: {refs} code ref(s) across {parts} part(s)\n"
    )


if __name__ == "__main__":
    main()
