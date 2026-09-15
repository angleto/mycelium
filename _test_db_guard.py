"""Refuse to run the suite against a database that is not for tests.

``make test`` is ``uv run pytest`` and the suite points at whatever
``MYCELIUM_DATABASE_URL*`` say. The fixtures are not read-only: they create
organizations, and the session-scoped autouse fixture in the root conftest
rewrites the function ACLs of the target. Against a database holding real
data those are not operations anybody undoes.

The configuration makes the mistake easy rather than hypothetical. The
default in ``config.py`` is ``localhost:5432``, where nothing listens, so a
first run fails with a connection error; the development Postgres is on
5433; and there is no ``.env`` in the repository. The natural sequence for
somebody meeting that failure is to look for the Postgres that IS running,
find it on 5433, export the URL and re-run. That reasonable gesture points
the suite at the development database, and nothing stopped it. It is not a
slip: it is what the system suggests.

WHY A MARKER TABLE and not the two cheaper candidates. A naming convention
(``mycelium_test``) needs an exception for CI, which uses ``mycelium``, and a
check with an exception is a check somebody eventually turns off. An
environment variable (``MYCELIUM_ALLOW_DESTRUCTIVE_TESTS=1``) ends up in the
shell profile of whoever is in a hurry on their first day, and from then on
it protects nobody. A table has to be created by a deliberate act against
that specific database, and it travels with the database rather than with
the machine or the shell.

WHAT IT DOES NOT COVER: a database somebody marked by hand and then filled
with real data. Nothing here can tell that apart from a test database, which
is why the marker row says in words what it grants.
"""

from __future__ import annotations

from urllib.parse import urlsplit

MARKER_TABLE = "_mycelium_test_database"

_HOW = """
Create a throwaway test database instead:

    make test-db-up          # container, roles, migrations, ACLs, marker
    make test                # about 31 minutes on a clean database
    make test-db-down

or mark an existing THROWAWAY database (never the development one):

    psql "$MYCELIUM_DATABASE_URL_SYNC" -f deploy/local/mark_test_database.sql
"""


class NotATestDatabase(RuntimeError):
    """The target is reachable and carries no marker."""


def _target(url: str) -> tuple[str, str | None, str]:
    """(host, port, database) as the URL names them, for comparison and for
    the message. Parsed rather than passed around whole so a password never
    reaches a traceback."""
    parts = urlsplit(url)
    return parts.hostname or "", str(parts.port) if parts.port else None, parts.path.lstrip("/")


def describe(url: str) -> str:
    host, port, database = _target(url)
    return f"{host}:{port or 'default'}/{database}"


def assert_test_database(*, sync_url: str, async_url: str) -> None:
    """Raise unless the target is reachable and declares itself disposable.

    UNREACHABLE IS SILENT, deliberately: there is nothing to destroy, and
    the offline CLI-only run is a real workflow (``make test-cli``). The
    DB-backed tests fail on their own connection a moment later, which is a
    clearer message than this one would be.

    A REFUSED AUTHENTICATION is an ``OperationalError`` too, so it takes the
    same silent branch and is indistinguishable from a server that is not
    there. That is safe in effect rather than by luck: a credential the
    guard cannot use is a credential the suite cannot use either, so the
    writes this exists to prevent cannot happen. It is worth knowing about
    because it is how this check can be defeated by accident and not by
    design, and a test that hands it a URL whose password has been masked
    will pass while measuring nothing.

    Both URLs are examined because they are separate settings and can name
    separate databases. The marker is read over the SYNC url, which is the
    owner role the destructive fixture itself uses; the async url is checked
    for AGREEMENT, since a marker on one database says nothing about writes
    going to another.
    """
    if _target(sync_url) != _target(async_url):
        raise NotATestDatabase(
            "MYCELIUM_DATABASE_URL and MYCELIUM_DATABASE_URL_SYNC name different "
            f"databases: {describe(async_url)} and {describe(sync_url)}. The suite "
            "writes through both, so a marker on one of them protects nothing.\n" + _HOW
        )

    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import OperationalError

    engine = create_engine(sync_url)
    try:
        with engine.connect() as conn:
            marked = conn.execute(
                text("SELECT to_regclass(:name) IS NOT NULL"), {"name": MARKER_TABLE}
            ).scalar_one()
    except OperationalError:
        return
    finally:
        engine.dispose()

    if not marked:
        raise NotATestDatabase(
            f"Refusing to run the test suite against {describe(sync_url)}: it has no "
            f"{MARKER_TABLE} table, so it has not been declared disposable.\n"
            "The suite creates organizations and REWRITES this database's function "
            "ACLs. Do not point it at the development database.\n" + _HOW
        )
