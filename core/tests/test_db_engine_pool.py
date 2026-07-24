"""Async engine pooling + bounded connect retry (``mycelium_core.db``).

Two independent resilience properties of the DB layer, and their limits:

- the pool is sized EXPLICITLY from settings, so this app's connection
  footprint on the shared managed instance is a number that can be added up
  per deployment -- and is SMALLER than SQLAlchemy's implicit 5 + 10;
- ``do_connect`` retries a genuinely transient connect failure with bounded,
  jittered backoff, and retries nothing else: an auth failure, an admission
  refusal (53300 too_many_connections) or a protocol violation must surface on
  the first attempt.

Neither property cures a database that stops admitting new connections (an
ACL/firewall change, a hard outage): every attempt fails, the bound is
exhausted, the driver error surfaces. These tests pin resilience behaviour,
not an incident fix.

The retry tests drive the REAL engine through the REAL greenlet bridge (the
hook only works inside it) by swapping the dialect's ``connect``. The reuse /
recycle / "fails twice then succeeds" cases need the test DB to be reachable.
"""

from __future__ import annotations

import asyncio
import socket
import ssl
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

import mycelium_core.db as db
from mycelium_core.config import Settings, get_settings


@contextmanager
def _engine_with(monkeypatch: pytest.MonkeyPatch, **env: str | None) -> Iterator[AsyncEngine]:
    """A freshly built engine with ``env`` applied to the settings.

    A ``None`` value REMOVES the variable, so a test can pin the behaviour of
    a shipped default even on a machine that exports an override.

    ``get_settings`` is an lru_cache singleton and the engine is a module
    global, so both must be dropped for the env tweak to take effect -- and
    dropped again on the way out, or the flipped value leaks into every later
    test. Disposal is the root conftest's autouse ``_dispose_engine`` job (it
    runs after every test), so this only has to hand the engine over.
    """
    for name, value in env.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    db._engine = None
    db._sessionmaker = None
    try:
        yield db.get_engine()
    finally:
        get_settings.cache_clear()


def _shipped_int(name: str) -> int:
    """The default DECLARED on a Settings field, environment excluded.

    ``Settings()`` reads os.environ and .env, so it describes the machine it
    runs on; these tests are about what we SHIP. Reading the field default
    keeps an operator who exports MYCELIUM_DB_POOL_SIZE from failing an
    unrelated test run.
    """
    value = Settings.model_fields[name].default
    assert isinstance(value, int), f"{name} default is not an int: {value!r}"
    return value


def _shipped_float(name: str) -> float:
    value = Settings.model_fields[name].default
    assert isinstance(value, float | int), f"{name} default is not numeric: {value!r}"
    return float(value)


async def _concurrent_burst(engine: AsyncEngine, n: int) -> None:
    """Hold ``n`` connections open AT THE SAME TIME, then release them all.

    The barrier is what makes this a deterministic burst: every task stays
    inside its ``engine.connect()`` block until all ``n`` have arrived, so the
    pool must supply ``n`` DISTINCT connections instead of however many the
    scheduler happens to overlap.
    """
    barrier = asyncio.Barrier(n)

    async def one() -> None:
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT 1"))).scalar_one() == 1
            await barrier.wait()

    await asyncio.gather(*(one() for _ in range(n)))


def _count_connects(monkeypatch: pytest.MonkeyPatch, engine: AsyncEngine) -> list[int]:
    """Wrap the dialect's ``connect`` with a counter; returns a 1-cell list.

    Counts connection CREATIONS only: ``pool_pre_ping`` validates an existing
    connection without going through ``dialect.connect``, so a checkout that
    reuses a live pooled connection does not move this number.
    """
    counter = [0]
    dialect = engine.sync_engine.dialect
    original = dialect.connect

    def counting(*cargs: Any, **cparams: Any) -> Any:
        counter[0] += 1
        return original(*cargs, **cparams)

    monkeypatch.setattr(dialect, "connect", counting)
    return counter


async def test_pool_is_sized_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every pooling parameter comes from a MYCELIUM_* knob, none is inherited."""
    with _engine_with(
        monkeypatch,
        MYCELIUM_DB_POOL_SIZE="7",
        MYCELIUM_DB_MAX_OVERFLOW="3",
        MYCELIUM_DB_POOL_TIMEOUT_SECONDS="4.5",
        MYCELIUM_DB_POOL_RECYCLE_SECONDS="111",
    ) as engine:
        pool = engine.sync_engine.pool
        assert pool.size() == 7
        assert pool._max_overflow == 3
        assert pool._timeout == 4.5
        assert pool._recycle == 111
        # pool_pre_ping stays on: it is what replaces a connection that died
        # while idle, one checkout at a time (see the recycle tests below).
        assert pool._pre_ping is True
        # LIFO keeps reuse on a small hot set instead of round-robining over
        # the whole pool.
        assert pool._pool.use_lifo is True


# The deployment premise the shipped defaults are sized against; the same
# numbers are spelled out in ``mycelium_core.db.get_engine``'s docstring. If
# any of them changes (a replica, a uvicorn --workers, a bigger instance), the
# defaults must be re-derived and this is where that gets noticed.
_MAX_CONNECTIONS = 100  # Scaleway Managed Postgres db-dev-s
_SUPERUSER_RESERVED = 3  # Postgres superuser_reserved_connections
_POOL_HOLDING_PROCESSES = 3  # backend, worker, sdi-inbound: 1 replica x 1 process each
_ROLLING_UPDATE_FACTOR = 2  # old + new pod of each deployment overlap during a deploy
# The MINIMUM we insist on leaving on the instance for the co-tenant project,
# an ``alembic upgrade`` and a human ``psql``. The shipped defaults leave more
# than this (43 of 97); the constant is the line we refuse to cross.
_CO_TENANT_RESERVE = 25
# The worker runs ~12 sweep loops whose ticks overlap (dispatch, reminders,
# webhooks, the search/embedding backfills, ...), each opening its own session.
# A ceiling under this turns ordinary operation into checkout timeouts, so it
# is a FLOOR on the sizing, not an aspiration.
_WORKER_CONCURRENT_LOOPS = 12


def test_shipped_pool_defaults_fit_the_connection_budget() -> None:
    """The defaults are a deployment contract, so assert the arithmetic.

    Read from the FIELD DEFAULTS, not from ``Settings()``: the latter merges
    os.environ and .env, i.e. it would assert the configuration of whatever
    machine runs the suite instead of the configuration we ship.

    Two ceilings, because they are not the same claim. STEADY STATE is what
    the deployment actually holds; the ROLLING window is bounded on the
    PERSISTENT half only, because overflow connections are opened on demand
    and discarded on return, so an old pod that is draining and a new pod that
    is warming do not both sit at their overflow ceiling. Bounding the roll on
    the full ceiling instead is what produced a sizing below the worker's own
    concurrency, i.e. a self-inflicted checkout timeout.
    """
    per_process = _shipped_int("db_pool_size") + _shipped_int("db_max_overflow")
    available = _MAX_CONNECTIONS - _SUPERUSER_RESERVED - _CO_TENANT_RESERVE

    steady = per_process * _POOL_HOLDING_PROCESSES
    assert steady <= available, (
        f"steady-state {steady} connections exceeds the {available} this app "
        f"may claim on the shared instance"
    )

    rolling_persistent = (
        _shipped_int("db_pool_size") * _POOL_HOLDING_PROCESSES * _ROLLING_UPDATE_FACTOR
    )
    assert rolling_persistent <= available, (
        f"mid-deploy {rolling_persistent} persistent connections exceeds {available}"
    )

    # The total must not exceed what SQLAlchemy would have given us implicitly:
    # the fix is the SPLIT (more persistent, less overflow), not a bigger pool.
    assert per_process <= 5 + 10
    # ... and it must not fall below what the worker structurally needs.
    assert per_process >= _WORKER_CONCURRENT_LOOPS
    # Overflow connections are opened on the request's critical path, so they
    # stay a thin margin, never the main supply.
    assert _shipped_int("db_max_overflow") < _shipped_int("db_pool_size")
    # No age-based recycling by default: see the two recycle tests below.
    assert _shipped_int("db_pool_recycle_seconds") == -1
    # A request stuck at the connect boundary is bounded by attempts x the
    # per-attempt connect timeout, plus the full-jitter backoff ceiling
    # (base x (2^(attempts-1) - 1)). 15.3 s as shipped.
    attempts = _shipped_int("db_connect_max_attempts")
    connect_bound = attempts * _shipped_float("db_connect_timeout_seconds")
    backoff_bound = _shipped_float("db_connect_retry_base_seconds") * (2 ** (attempts - 1) - 1)
    assert connect_bound + backoff_bound <= 20.0


async def test_shipped_config_reuses_pooled_connections_across_bursts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the shipped config a second burst opens NOTHING.

    This is the property ``db_pool_recycle_seconds = -1`` buys: connections
    are replaced when they are found dead (pool_pre_ping, one checkout at a
    time), never on a timer. The env var is removed rather than set, so this
    exercises the default even where an operator exports an override.
    """
    with _engine_with(monkeypatch, MYCELIUM_DB_POOL_RECYCLE_SECONDS=None) as engine:
        assert engine.sync_engine.pool._recycle == -1
        connects = _count_connects(monkeypatch, engine)

        await _concurrent_burst(engine, 4)
        assert connects[0] == 4, "a cold pool must open one connection per concurrent checkout"

        await _concurrent_burst(engine, 4)
        assert connects[0] == 4, "the second burst must reuse the pool, not re-open it"


async def test_a_recycle_window_re_opens_the_whole_pool_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Why the shipped ``db_pool_recycle_seconds`` is -1, made executable.

    SQLAlchemy evaluates the recycle age per connection AT CHECKOUT, and a
    pool's connections are opened together, so their ages are correlated: once
    the window elapses, the next burst of N concurrent checkouts re-opens N
    connections in the same instant. Synchronized, not staggered -- a connect
    storm on a schedule, and one that discards live connections.

    The window is scaled to 1 s here; the shape is what matters.
    """
    with _engine_with(monkeypatch, MYCELIUM_DB_POOL_RECYCLE_SECONDS="1") as engine:
        connects = _count_connects(monkeypatch, engine)

        await _concurrent_burst(engine, 3)
        assert connects[0] == 3

        # Idle for longer than the window: every pooled connection is now
        # "expired", although all three are perfectly alive.
        await asyncio.sleep(1.2)

        await _concurrent_burst(engine, 3)
        assert connects[0] == 6, "the whole pool was re-opened simultaneously"


async def test_transient_connect_error_is_retried_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two resets then a success: the session works, the caller sees nothing.

    Also pins the reason ``do_connect`` was chosen over ``async_creator=``:
    the retry re-uses the ``cargs``/``cparams`` SQLAlchemy derived from the
    URL, byte-identical across attempts, so credentials / ssl / the connect
    timeout cannot be dropped on the retry path (no DSN is re-parsed).
    """
    with _engine_with(
        monkeypatch,
        MYCELIUM_DB_CONNECT_MAX_ATTEMPTS="3",
        MYCELIUM_DB_CONNECT_RETRY_BASE_SECONDS="0.01",
        MYCELIUM_DB_CONNECT_TIMEOUT_SECONDS="7.5",
    ) as engine:
        dialect = engine.sync_engine.dialect
        original = dialect.connect
        seen: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

        def flaky(*cargs: Any, **cparams: Any) -> Any:
            seen.append((cargs, dict(cparams)))
            if len(seen) <= 2:
                raise ConnectionResetError(104, "Connection reset by peer")
            return original(*cargs, **cparams)

        monkeypatch.setattr(dialect, "connect", flaky)
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT 1"))).scalar_one() == 1

        assert len(seen) == 3, "two retries, then the real connect"
        assert seen[0] == seen[1] == seen[2], "retry must not re-derive the connect args"
        # The URL's own material survives, and the wall-clock bound is armed.
        assert seen[0][1]["user"]
        assert seen[0][1]["database"]
        assert seen[0][1]["timeout"] == 7.5


async def test_permanent_connect_failure_raises_after_the_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DB that is really down fails fast: bounded attempts, no hang.

    The original driver exception is what surfaces -- the retry hides a
    single-event blip, it must never disguise a server that is not accepting
    connections.
    """
    with _engine_with(
        monkeypatch,
        MYCELIUM_DB_CONNECT_MAX_ATTEMPTS="4",
        MYCELIUM_DB_CONNECT_RETRY_BASE_SECONDS="0.01",
    ) as engine:
        calls = 0

        def always_reset(*cargs: Any, **cparams: Any) -> Any:
            nonlocal calls
            calls += 1
            raise ConnectionResetError(104, "Connection reset by peer")

        monkeypatch.setattr(engine.sync_engine.dialect, "connect", always_reset)
        started = time.monotonic()
        with pytest.raises(ConnectionResetError):
            async with engine.connect():
                pass
        elapsed = time.monotonic() - started

        assert calls == 4, "exactly db_connect_max_attempts, no unbounded loop"
        # Backoff ceiling is 0.01 + 0.02 + 0.04 s; a generous bound still
        # proves it terminates instead of retrying forever.
        assert elapsed < 5.0


async def test_auth_failure_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bad password surfaces on the FIRST attempt.

    Retrying it cannot help and can hurt: repeated failures are exactly what
    trips account lockout / fail2ban on the DB side.
    """
    with _engine_with(
        monkeypatch,
        MYCELIUM_DB_CONNECT_MAX_ATTEMPTS="5",
        MYCELIUM_DB_CONNECT_RETRY_BASE_SECONDS="0.01",
    ) as engine:
        calls = 0

        def bad_password(*cargs: Any, **cparams: Any) -> Any:
            nonlocal calls
            calls += 1
            raise asyncpg.exceptions.InvalidPasswordError("password authentication failed")

        monkeypatch.setattr(engine.sync_engine.dialect, "connect", bad_password)
        with pytest.raises(asyncpg.exceptions.InvalidPasswordError):
            async with engine.connect():
                pass

        assert calls == 1


async def test_too_many_connections_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """53300 surfaces on the FIRST attempt: it is an ADMISSION signal.

    The server is at ``max_connections`` and said so. Retrying multiplies our
    connect rate against an instance that is by definition already saturated
    (and that we share with a co-tenant). The lever for 53300 is the
    connection budget in ``get_engine``, not a retry.
    """
    with _engine_with(
        monkeypatch,
        MYCELIUM_DB_CONNECT_MAX_ATTEMPTS="5",
        MYCELIUM_DB_CONNECT_RETRY_BASE_SECONDS="0.01",
    ) as engine:
        calls = 0

        def too_many(*cargs: Any, **cparams: Any) -> Any:
            nonlocal calls
            calls += 1
            raise asyncpg.exceptions.TooManyConnectionsError("sorry, too many clients already")

        monkeypatch.setattr(engine.sync_engine.dialect, "connect", too_many)
        with pytest.raises(asyncpg.exceptions.TooManyConnectionsError):
            async with engine.connect():
                pass

        assert calls == 1


async def test_non_connection_error_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """Anything the classifier does not recognise is surfaced immediately."""
    with _engine_with(
        monkeypatch,
        MYCELIUM_DB_CONNECT_MAX_ATTEMPTS="5",
        MYCELIUM_DB_CONNECT_RETRY_BASE_SECONDS="0.01",
    ) as engine:
        calls = 0

        def boom(*cargs: Any, **cparams: Any) -> Any:
            nonlocal calls
            calls += 1
            raise RuntimeError("not a connection problem")

        monkeypatch.setattr(engine.sync_engine.dialect, "connect", boom)
        with pytest.raises(RuntimeError, match="not a connection problem"):
            async with engine.connect():
                pass

        assert calls == 1


@pytest.mark.parametrize(
    "exc",
    [
        ConnectionResetError(104, "Connection reset by peer"),
        ConnectionRefusedError(111, "Connection refused"),
        TimeoutError("handshake timed out"),
        socket.gaierror(-3, "Temporary failure in name resolution"),
        ssl.SSLError("handshake aborted"),
        # 57P03: the server itself says "not yet" (starting up / in recovery).
        asyncpg.exceptions.CannotConnectNowError("the database system is starting up"),
        # 08003 / 08006 / 08000: the connection was lost, nothing to duplicate.
        asyncpg.exceptions.ConnectionDoesNotExistError("gone"),
        asyncpg.exceptions.ConnectionFailureError("connection failure"),
        asyncpg.exceptions.PostgresConnectionError("connection exception"),
    ],
)
def test_classifier_accepts_transient_connect_failures(exc: BaseException) -> None:
    assert db._is_transient_connect_error(exc) is True


@pytest.mark.parametrize(
    "exc",
    [
        asyncpg.exceptions.InvalidPasswordError("password authentication failed"),
        asyncpg.exceptions.InvalidCatalogNameError('database "nope" does not exist'),
        asyncpg.exceptions.InsufficientPrivilegeError("permission denied"),
        asyncpg.exceptions.UndefinedTableError("relation does not exist"),
        # 53300 / 08004: admission decisions by the server or a pooler in
        # front of it. Retrying adds connect attempts to something already at
        # its limit.
        asyncpg.exceptions.TooManyConnectionsError("sorry, too many clients already"),
        asyncpg.exceptions.ConnectionRejectionError("rejected"),
        # 08P01: a wire-level disagreement is deterministic, the retry
        # reproduces it.
        asyncpg.exceptions.ProtocolViolationError("invalid startup packet"),
        # 08007: an unknown transaction OUTCOME, not a connection that failed
        # to open; retrying an unknown-outcome operation is not safe.
        asyncpg.exceptions.TransactionResolutionUnknownError("unknown"),
        # An OSError subclass, but a certificate that does not verify now will
        # not verify 100 ms later: permanent trust-store error, surface it.
        ssl.SSLCertVerificationError("certificate verify failed"),
        RuntimeError("unrelated"),
    ],
)
def test_classifier_rejects_permanent_failures(exc: BaseException) -> None:
    assert db._is_transient_connect_error(exc) is False


async def test_tenant_session_still_works_over_the_retrying_engine() -> None:
    """End-to-end sanity: the hook is installed on the live engine and the
    RLS-bearing session path is unaffected by the pooling/retry wiring."""
    engine = db.get_engine()
    assert db._connect_with_retry in engine.sync_engine.dialect.dispatch.do_connect
    org, user = uuid.uuid4(), uuid.uuid4()
    async with db.tenant_session(str(org), str(user)) as session:
        stmt = text("SELECT current_setting('app.current_org')")
        assert (await session.execute(stmt)).scalar_one() == str(org)
