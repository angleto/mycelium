"""The suite refuses a database that has not been declared disposable.

Task 7637f893. The fixtures create organizations and the session-scoped
autouse fixture in the root conftest rewrites the target's function ACLs;
against a database holding real data neither is undone. Until this guard
there was nothing between ``uv run pytest`` and whatever
``MYCELIUM_DATABASE_URL*`` happened to name, and the configuration makes
the wrong target the natural one to reach for: the default port has
nothing listening, the development Postgres is on another, and the obvious
recovery is to point the suite at the Postgres that IS running.

These exercise the check itself rather than a subprocess run of pytest.
The guard has to fire BEFORE the fixtures, so a test inside the session
cannot observe the session refusing to start; what it can do is point the
same function at a real database that carries no marker, which is the
whole of the behaviour. The markerless database used here is ``postgres``,
the maintenance database every server has: a real connection to a real
database that really has no marker, with nothing to set up.
"""

from __future__ import annotations

import pytest
from _test_db_guard import MARKER_TABLE, NotATestDatabase, assert_test_database
from sqlalchemy.engine import make_url

from mycelium_core.config import get_settings


def _urls() -> tuple[str, str]:
    s = get_settings()
    return s.database_url_sync, s.database_url


def _pointing_at(url: str, database: str) -> str:
    """The same target with another database name.

    ``render_as_string(hide_password=False)`` and not ``str()``: ``str()``
    masks the password as ``***``, and a URL carrying that reaches the
    server as a failed authentication, which the guard reads as
    unreachable and passes. The first version of this test did exactly
    that and was green against a guard that had never been asked a
    question."""
    return make_url(url).set(database=database).render_as_string(hide_password=False)


def test_the_database_the_suite_is_running_on_is_marked() -> None:
    """The positive case, and it is not a tautology: if the recipe in
    ``make test-db-up`` or the CI step stopped creating the marker, every
    run would refuse and this says which half broke."""
    sync_url, async_url = _urls()
    assert_test_database(sync_url=sync_url, async_url=async_url)


def test_an_unmarked_database_is_refused_with_a_message_that_teaches() -> None:
    """A refusal that only says no gets worked around rather than
    understood, so the message is part of the behaviour: it has to name the
    target, name the missing marker, and carry the command that makes a
    disposable database."""
    sync_url, async_url = _urls()
    other_sync = _pointing_at(sync_url, "postgres")
    other_async = _pointing_at(async_url, "postgres")
    with pytest.raises(NotATestDatabase) as excinfo:
        assert_test_database(sync_url=other_sync, async_url=other_async)
    message = str(excinfo.value)
    assert MARKER_TABLE in message
    assert "/postgres" in message
    assert "make test-db-up" in message
    assert "mark_test_database.sql" in message


def test_the_refusal_never_carries_the_password_it_was_handed() -> None:
    """The message names the target, and a URL is the wrong thing to print
    because it carries a credential.

    Two choices here are forced and both were found by the measurement
    failing. The password is one that cannot occur by accident: the fixture
    password is the literal string "mycelium", a substring of the marker
    table's own name, so asserting ITS absence fails against a message that
    leaks nothing. And the refusal exercised is the DISAGREEMENT branch,
    which answers before opening a connection: a made-up password on the
    marker branch is a refused authentication, which the guard reads as
    unreachable and passes, so that route cannot ask this question."""
    secret = "Pw-9d41c7a2-not-a-substring-of-anything"
    base = f"postgresql+psycopg://mycelium:{secret}@localhost:5439"
    with pytest.raises(NotATestDatabase) as excinfo:
        assert_test_database(sync_url=f"{base}/one", async_url=f"{base}/another")
    message = str(excinfo.value)
    assert "different" in message
    assert secret not in message


def test_two_urls_naming_different_databases_are_refused() -> None:
    """The two settings are independent and the suite writes through both,
    so a marker on one of them protects nothing. Without this term the
    guard reads the marked database and blesses writes going elsewhere."""
    sync_url, async_url = _urls()
    with pytest.raises(NotATestDatabase) as excinfo:
        assert_test_database(sync_url=sync_url, async_url=_pointing_at(async_url, "somewhere_else"))
    assert "different" in str(excinfo.value)


def test_an_unreachable_database_is_silent() -> None:
    """Nothing to destroy, nothing to say. The offline CLI-only run is a
    real workflow (``make test-cli``) and the DB-backed tests fail on their
    own connection a moment later with a clearer message than this one."""
    dead = "postgresql+psycopg://nobody:nobody@127.0.0.1:1/nothing"
    assert_test_database(sync_url=dead, async_url=dead)
