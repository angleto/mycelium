"""How the CLI renders a refusal: which status sends the user to log in.

A 403 used to carry the hint "Run: mycelium auth login". Logging in again
stores the same kind of token, which the server refuses the same act, so
the hint sent the user in a circle. Only a 401, a credential the server does
not accept at all, is a login problem.
"""

from __future__ import annotations

import httpx
import pytest

from mycelium_cli.http import AuthRequired, CLIError, raise_for_response


def _resp(status: int, code: str, message: str) -> httpx.Response:
    return httpx.Response(status, json={"code": code, "message": message})


def test_a_refused_act_names_the_reason_and_not_a_login() -> None:
    msg = "This request is reserved to a person. Make it from the web app."
    with pytest.raises(CLIError) as exc:
        raise_for_response(_resp(403, "agent.person_only", msg))
    assert not isinstance(exc.value, AuthRequired)
    assert exc.value.hint is None
    assert "agent.person_only" in str(exc.value)
    assert "web app" in str(exc.value)


def test_an_unaccepted_credential_still_sends_the_user_to_log_in() -> None:
    with pytest.raises(AuthRequired) as exc:
        raise_for_response(_resp(401, "auth.invalid_token", "Invalid token"))
    assert exc.value.hint == "Run: mycelium auth login"
