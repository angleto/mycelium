"""Governance metric for the eval harness (Track B / B3).

Verified forgetting: erasing a subject's provenance must make its answers
actually UNRETRIEVABLE (GDPR right-to-erasure), not merely hidden -- the axis
hosted competitors never score. Self-contained: signup org, FakeEmbedder,
ingest two subjects with provenance, erase one, assert recall drops.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _fake_embedder import FakeEmbedder  # noqa: E402

from mycelium_core.db import admin_session, tenant_session  # noqa: E402
from mycelium_core.embedder import set_embedder_override  # noqa: E402
from mycelium_core.services import eval_offline, memory  # noqa: E402
from mycelium_core.services.auth import signup  # noqa: E402
from mycelium_core.services.eval_offline import GoldCase  # noqa: E402


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="GOV")
    return r.org_id, r.user_id


async def test_gdpr_forgetting_actually_drops_recall() -> None:
    org, user = await _org()
    set_embedder_override(FakeEmbedder)
    try:
        async with tenant_session(str(org), str(user)) as s:
            alice = await memory.write_blob(
                s,
                org_id=org,
                actor_id=user,
                project_id=None,
                text_body="kubernetes cluster autoscaling notes",
                operation_id="w-alice",
                sources=[("subject", "alice")],
            )
            bob = await memory.write_blob(
                s,
                org_id=org,
                actor_id=user,
                project_id=None,
                text_body="espresso machine descaling routine",
                operation_id="w-bob",
                sources=[("subject", "bob")],
            )
            alice_id, bob_id = alice.id, bob.id
        async with tenant_session(str(org), str(user)) as s:
            cases = [
                GoldCase(query="kubernetes cluster autoscaling", expected=frozenset({alice_id})),
                GoldCase(query="espresso machine descaling", expected=frozenset({bob_id})),
            ]
            report = await eval_offline.gdpr_forgetting(
                s,
                org_id=org,
                actor_id=user,
                source_kind="subject",
                source_id="alice",
                cases=cases,
            )
            assert report.recall_before == 1.0  # both subjects retrievable
            assert report.erased >= 1  # Alice's blob was actually deleted
            assert report.recall_after < report.recall_before  # her answer is gone
            assert report.forgotten is True
    finally:
        set_embedder_override(None)
