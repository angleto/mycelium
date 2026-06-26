"""Set the per-org semantic-similarity floor (mycelio retrieval tuning).

The `mycelium` org shipped with ``retrieval_semantic_min_similarity = 0.8`` — above
bge-m3's achievable cosine band on the real corpus (max ~0.63, measured by
scripts/diag_retrieval.py), so ``SemanticDenseStage`` rejected 100% of dense
neighbours and retrieval silently degraded to lexical-only. 0.4 sits at the
noise/genuine boundary (relevant notes 0.43-0.63, noise tail <0.40), keeping
every relevant note while trimming the bottom third.

Mirrors the workspace settings router (merge bag + version-checked
optimistic_update), so audit/version invariants hold:

    kubectl -n mycelium-production exec -i deploy/mycelium-backend \
      -- python - < scripts/set_semantic_floor.py
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from mycelium_core.concurrency import optimistic_update
from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.membership import Membership, Role
from mycelium_core.models.memory_blob import MemoryBlob
from mycelium_core.models.organization import Organization
from mycelium_core.services import memory as M

MYCELIUM_PROJECT = "b5ec1167-11f0-47bf-ba11-f5c25885928c"
NEW_FLOOR = 0.4


async def main() -> None:
    async with admin_session() as s:
        orgs = (
            await s.execute(
                select(Membership.org_id, Membership.user_id).where(Membership.role == Role.owner)
            )
        ).all()

    org_id = user_id = None
    for cand_org, cand_user in orgs:
        async with tenant_session(str(cand_org), str(cand_user), project_id=MYCELIUM_PROJECT) as s:
            hit = (
                await s.execute(
                    select(MemoryBlob.id).where(MemoryBlob.project_id == MYCELIUM_PROJECT).limit(1)
                )
            ).first()
        if hit is not None:
            org_id, user_id = cand_org, cand_user
            break
    if org_id is None:
        print("mycelium project not found in any owned org; aborting")
        return

    async with tenant_session(str(org_id), str(user_id), project_id=MYCELIUM_PROJECT) as s:
        org = (await s.execute(select(Organization).where(Organization.id == org_id))).scalar_one()
        old = (org.settings or {}).get(M.SEMANTIC_MIN_SIM_KEY, "<unset>")
        if old != "<unset>" and float(old) == NEW_FLOOR:
            print(f"already {NEW_FLOOR}; no change")
            return
        merged = {**(org.settings or {}), M.SEMANTIC_MIN_SIM_KEY: NEW_FLOOR}
        new_version = await optimistic_update(
            s,
            Organization,
            pk=org_id,
            expected_version=org.version,
            values={"settings": merged},
        )
        print(f"{M.SEMANTIC_MIN_SIM_KEY}: {old} -> {NEW_FLOOR} (org={org_id} v->{new_version})")


if __name__ == "__main__":
    asyncio.run(main())
