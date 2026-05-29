"""Chunked append to note parts (task 27f4d6c9): stream a large markdown
body past the MCP/JSON-RPC per-``tools/call`` payload cap. DB-backed,
exercises the same service the REST + MCP + CLI surfaces all wrap.

Properties under test:
- byte-for-byte reassembly (chunks concatenate raw, no separator);
- idempotent replay (same chunk at the same cursor is a no-op);
- optimistic concurrency (a stale cursor raises, no last-write-wins).
"""

from __future__ import annotations

import uuid

from flow_core.db import admin_session
from flow_core.errors import ConflictError
from flow_core.services.auth import signup
from flow_mcp.server import (
    append_note_part,
    create_note,
    get_note_part,
)


async def _tenant() -> tuple[str, str]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="ChunkedAppend",
        )
    assert r.token is not None
    return r.token, str(r.org_id)


def _payload(n_chunks: int, chunk_size: int) -> tuple[str, list[str]]:
    """Deterministic multi-paragraph markdown spanning ``n_chunks``
    chunks, with non-ASCII so the byte-vs-char accounting is exercised."""
    full = "".join(f"## Section {i}\n\nRiga {i} — corpo àèìòù ({i}).\n\n" for i in range(4000))
    full = full[: n_chunks * chunk_size - 7]  # land mid-chunk on the last one
    chunks = [full[i : i + chunk_size] for i in range(0, len(full), chunk_size)]
    return full, chunks


async def test_chunked_append_byte_integrity() -> None:
    token, org = await _tenant()
    note = await create_note(token=token, org_id=org, kind="text", text="seed")
    full, chunks = _payload(n_chunks=8, chunk_size=20_000)

    # First chunk CREATES the part (part_id omitted); the rest append.
    first = await append_note_part(token=token, org_id=org, note_id=note["id"], chunk=chunks[0])
    part_id = first["part_id"]
    version = first["version"]
    assert first["appended_chars"] == len(chunks[0])

    for idx in range(1, len(chunks)):
        res = await append_note_part(
            token=token,
            org_id=org,
            part_id=part_id,
            chunk=chunks[idx],
            expected_version=version,
            chunk_index=idx,
            is_last=idx == len(chunks) - 1,
            operation_id="op-1",
        )
        assert res["appended_chars"] == len(chunks[idx])
        version = res["version"]

    part = await get_note_part(token=token, org_id=org, part_id=part_id)
    assert part["body"] == full  # byte-for-byte reassembly, no separators


async def test_chunked_append_idempotent_replay() -> None:
    token, org = await _tenant()
    note = await create_note(token=token, org_id=org, kind="text", text="seed")
    first = await append_note_part(token=token, org_id=org, note_id=note["id"], chunk="alpha-")
    part_id, v0 = first["part_id"], first["version"]

    applied = await append_note_part(
        token=token, org_id=org, part_id=part_id, chunk="beta-", expected_version=v0
    )
    assert applied["appended_chars"] == len("beta-")
    v1 = applied["version"]
    assert v1 == v0 + 1

    # Replay of the SAME chunk at the SAME (now stale) cursor: no-op.
    replay = await append_note_part(
        token=token, org_id=org, part_id=part_id, chunk="beta-", expected_version=v0
    )
    assert replay["appended_chars"] == 0
    assert replay["version"] == v1

    part = await get_note_part(token=token, org_id=org, part_id=part_id)
    assert part["body"] == "alpha-beta-"  # replay did not double-append


async def test_chunked_append_stale_cursor_conflicts() -> None:
    token, org = await _tenant()
    note = await create_note(token=token, org_id=org, kind="text", text="seed")
    first = await append_note_part(token=token, org_id=org, note_id=note["id"], chunk="root-")
    part_id, v0 = first["part_id"], first["version"]

    await append_note_part(
        token=token, org_id=org, part_id=part_id, chunk="one-", expected_version=v0
    )

    # A second writer at the stale cursor with DIFFERENT content must lose
    # (not last-write-wins): version advanced and the tail is not "two-".
    try:
        await append_note_part(
            token=token, org_id=org, part_id=part_id, chunk="two-", expected_version=v0
        )
    except ConflictError:
        pass
    else:  # pragma: no cover - the assertion is the failure path
        raise AssertionError("stale cursor with new content should raise ConflictError")
