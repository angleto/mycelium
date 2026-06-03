"""REST wiring for opt-in narration (task T3).

POST /advisory/what-now with ``narrate`` true adds the advisor rationale
over the SAME ranked envelope, degrading to narrated=false when no
provider is configured. The deterministic envelope shape is unchanged.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from httpx import ASGITransport, AsyncClient

from flow_api.main import app
from flow_core.ai_providers import LLMResult, set_llm_override


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


class _Fake:
    model_id = "fake-narr"

    async def complete(
        self, *, system: str | None, messages: Sequence[tuple[str, str]]
    ) -> LLMResult:
        return LLMResult(
            text="Knock out alpha first; it fits the window.",
            tokens_in=1,
            tokens_out=1,
            model_id=self.model_id,
        )


async def test_rest_what_now_narrate_wiring() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123", "workspace_name": "NARR"},
            )
        ).json()
        h = {"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": a["workspace_id"]}
        me = a["user_id"]
        await c.post(
            "/tasks",
            headers=h,
            json={
                "title": "do alpha",
                "importance": 1,
                "urgency": 1,
                "estimate_effort_h": "0.5",
                "assignee_ids": [me],
            },
        )

        set_llm_override(_Fake)
        try:
            d = await c.post(
                "/advisory/what-now",
                headers=h,
                json={"duration_minutes": 60, "narrate": True},
            )
        finally:
            set_llm_override(None)
        assert d.status_code == 200, d.text
        body = d.json()
        assert body["ranked"], "expected one feasible task"
        assert body["narrated"] is True
        assert body["narration"]
        assert body["narration_model"] == "fake-narr"

        # No provider configured -> graceful narrated=false, ranking intact.
        d2 = await c.post(
            "/advisory/what-now",
            headers=h,
            json={"duration_minutes": 60, "narrate": True},
        )
        b2 = d2.json()
        assert b2["narrated"] is False and b2["narration"] is None
        assert b2["ranked"]
