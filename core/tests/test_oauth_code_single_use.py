"""An assistant-secret authorization code is redeemed at most once, also when
the exchanges race (mycelium-cc review, 2026-11-13).

``oauth_codes.consume`` used to SELECT the row and then DELETE it without
looking at what the DELETE removed: two exchanges that both read the row
before either deleted it both got it back, and both minted a token. Observed
red on 2fcb7cd2 before the fix: several of the concurrent consumers returned
the row."""

from __future__ import annotations

import asyncio

from mycelium_core.db import admin_session
from mycelium_core.services import oauth_codes as codes_svc


async def _mint() -> str:
    async with admin_session() as s:
        return await codes_svc.mint(
            s,
            client_id="00000000-0000-0000-0000-000000000001",
            redirect_uri="https://host.example/cb",
            code_challenge="x" * 43,
            code_challenge_method="S256",
        )


async def _consume(code: str) -> object | None:
    async with admin_session() as s:
        return await codes_svc.consume(s, code=code)


async def test_concurrent_consumes_of_one_code_return_it_once() -> None:
    for _ in range(5):
        code = await _mint()
        got = await asyncio.gather(*(_consume(code) for _ in range(8)))
        winners = [g for g in got if g is not None]
        assert len(winners) == 1, f"{len(winners)} consumers got the same code"


async def test_a_consumed_code_carries_what_was_minted() -> None:
    code = await _mint()
    row = await _consume(code)
    assert row is not None
    assert row.client_id == "00000000-0000-0000-0000-000000000001"  # type: ignore[attr-defined]
    assert row.redirect_uri == "https://host.example/cb"  # type: ignore[attr-defined]
    assert row.code_challenge_method == "S256"  # type: ignore[attr-defined]
    assert await _consume(code) is None
