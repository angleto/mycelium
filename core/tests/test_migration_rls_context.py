"""Il runner delle migrazioni deve vedere le righe di ogni tenant.

Il difetto che questi test presidiano non produce errori: produce ZERO
righe. Una migrazione che gira come proprietario non-superuser su tabelle
con FORCE ROW LEVEL SECURITY esegue i suoi UPDATE senza toccare niente e
senza dirlo, e la 0035, la 0036, la 0086 e la 0099 lo hanno gia' fatto in
produzione. In sviluppo il ruolo e' superuser, quindi il caso non si
riproduce da solo: va costruito.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

import pytest
import sqlalchemy as sa

from mycelium_core.migration_rls import (
    _set_force,
    forced_tables,
    owner_sees_all_tenants,
    role_bypasses_rls,
    tables_without_owner_policy,
)


@contextmanager
def _conn(*, transaction: bool = False) -> Iterator[sa.Connection]:
    """Una connessione su un engine effimero, che viene DISPOSTO all'uscita.

    Chiudere la connessione non basta: il pool dell'engine la tiene aperta
    dopo il blocco, e psycopg la segnala come cancellata-mentre-aperta quando
    l'engine viene raccolto. In un test solo e' innocuo, in una suite e' una
    connessione persa per test contro un database che ha un limite. Gli altri
    gate di migrazione dispongono gia'; questo no, ed e' per questo che
    falliva con i warning trattati come errori.

    ``transaction=True`` apre con ``begin()`` invece che ``connect()``, che e'
    l'unica differenza fra i due usi in questo file."""
    url = os.environ.get("MYCELIUM_DATABASE_URL_SYNC")
    if not url:
        pytest.skip("MYCELIUM_DATABASE_URL_SYNC non impostata")
    engine = sa.create_engine(url, future=True)
    try:
        with engine.begin() if transaction else engine.connect() as conn:
            yield conn
    finally:
        engine.dispose()


@contextmanager
def _rolled_back() -> Iterator[sa.Connection]:
    """A transaction that is rolled back whatever happens inside it.

    For the tests that break the catalog on purpose (a policy dropped, FORCE
    taken away): ``_conn(transaction=True)`` commits on a clean exit, and a
    test that catches the expected error inside it would commit the hole it
    made into the database every later test runs against."""
    with _conn() as conn:
        tx = conn.begin()
        try:
            yield conn
        finally:
            tx.rollback()


def _drop_owner_policy(conn: sa.Connection, table: str) -> None:
    from mycelium_core.migration_rls import OWNER_POLICY

    conn.execute(sa.text(f'DROP POLICY {OWNER_POLICY} ON public."{table}"'))


def test_dove_il_ruolo_scavalca_gia_rls_non_tocca_niente() -> None:
    """In sviluppo e CI il ruolo e' superuser: il contesto non deve
    emettere nessun ALTER, quindi nessun lock e nessuna differenza."""
    with _conn() as conn:
        if not role_bypasses_rls(conn):
            pytest.skip("questo ambiente non usa un ruolo che scavalca l'RLS")
        prima = forced_tables(conn)
        with owner_sees_all_tenants(conn, log=lambda _m: None):
            # Nessun ALTER: l'insieme e' identico ANCHE dentro il blocco.
            assert forced_tables(conn) == prima
        assert forced_tables(conn) == prima


def test_solleva_e_ripristina_force_esattamente() -> None:
    """Il ciclo solleva/ripristina deve tornare all'insieme di partenza.

    Esercitato sulle primitive, perche' il contesto va in corto circuito
    quando il ruolo e' superuser."""
    with _conn(transaction=True) as conn:
        conn.execute(sa.text("CREATE TABLE _rls_probe (id int, org_id uuid)"))
        conn.execute(sa.text("ALTER TABLE _rls_probe ENABLE ROW LEVEL SECURITY"))
        conn.execute(sa.text("ALTER TABLE _rls_probe FORCE ROW LEVEL SECURITY"))
        try:
            assert "_rls_probe" in forced_tables(conn)
            _set_force(conn, ["_rls_probe"], on=False)
            assert "_rls_probe" not in forced_tables(conn)
            _set_force(conn, ["_rls_probe"], on=True)
            assert "_rls_probe" in forced_tables(conn)
        finally:
            conn.execute(sa.text("DROP TABLE _rls_probe"))


def test_il_padre_partizionato_non_viene_dimenticato() -> None:
    """``memory_blobs`` e' un PADRE partizionato (relkind='p'). Una query
    che filtra su relkind='r' lo salta, e allora il padre continua a
    filtrare mentre le partizioni no: la lettura torna vuota senza errore.
    E' esattamente cosi' che 1073 righe sono quasi andate perse durante il
    recupero del 22/08."""
    with _conn() as conn:
        forzate = forced_tables(conn)
        if "memory_blobs" not in [t for t in forzate]:
            pytest.skip("memory_blobs non ha FORCE in questo database")
        kind = conn.execute(
            sa.text(
                "SELECT c.relkind FROM pg_class c JOIN pg_namespace n "
                "ON n.oid = c.relnamespace WHERE n.nspname='public' "
                "AND c.relname='memory_blobs'"
            )
        ).scalar()
        assert kind == "p", "memory_blobs dovrebbe essere un padre partizionato"
        assert "memory_blobs" in forzate


def test_production_path_takes_no_lock_on_a_covered_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The path that runs in PRODUCTION, on the schema at head.

    Locally the role is a superuser and the bracket short-circuits, which is
    right but leaves the production branch untested: here the unprivileged
    role is faked. Every forced table carries the owner-session policy, so
    the bracket must emit no ALTER at all, which is the property that keeps a
    migrate Job out of a deadlock with live traffic (release 2.3.50: 89
    ACCESS EXCLUSIVE, first pod dead in DeadlockDetected)."""
    import mycelium_core.migration_rls as m

    with _rolled_back() as conn:
        before = forced_tables(conn)
        assert before, "lo schema dovrebbe avere tabelle con FORCE"
        monkeypatch.setattr(m, "role_bypasses_rls", lambda _c: False)
        seen, spy = _count_rls_alters()
        try:
            with m.owner_sees_all_tenants(conn, log=lambda _m: None):
                assert forced_tables(conn) == before
        finally:
            sa.event.remove(sa.engine.Engine, "before_cursor_execute", spy)
        assert seen["alters"] == 0, f"{seen['alters']} ALTER ... ROW LEVEL SECURITY emitted"
        assert forced_tables(conn) == before


def test_every_rls_table_at_head_carries_the_owner_policy() -> None:
    """The invariant the lock-free bracket stands on, read from the catalog.

    A table that gets RLS without ``create_owner_session_policy`` fails
    here in CI, where the bracket itself never runs (the role is a superuser
    there)."""
    with _conn() as conn:
        assert forced_tables(conn), "lo schema dovrebbe avere tabelle con FORCE"
        assert tables_without_owner_policy(conn) == []


def test_a_policy_with_the_right_name_and_another_body_does_not_count() -> None:
    """The check reads the body, not the name: a policy called
    ``owner_session_all`` that is granted to PUBLIC, or that is simply true,
    would open every tenant to whoever it covers."""
    from mycelium_core.migration_rls import OWNER_POLICY

    with _rolled_back() as conn:
        table, other = forced_tables(conn)[:2]
        _drop_owner_policy(conn, table)
        conn.execute(sa.text(f'CREATE POLICY {OWNER_POLICY} ON public."{table}" USING (true)'))
        _drop_owner_policy(conn, other)
        assert tables_without_owner_policy(conn) == sorted([table, other])


_PROBE_ROLES = ("rls_probe_member", "rls_probe_app", "rls_probe_owner")
_PROBE_PASSWORD = "rls-probe"  # a role on the disposable test database, dropped after use


def _drop_probe() -> None:
    with _conn(transaction=True) as conn:
        conn.execute(sa.text("DROP TABLE IF EXISTS public.rls_probe CASCADE"))
        conn.execute(sa.text("DROP FUNCTION IF EXISTS public.rls_probe_count()"))
        for role in _PROBE_ROLES:
            exists = conn.execute(
                sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}
            ).scalar()
            if exists:
                conn.execute(sa.text(f"DROP OWNED BY {role}"))
                conn.execute(sa.text(f"DROP ROLE {role}"))


@pytest.fixture
def owner_probe() -> Iterator[None]:
    """A real NOSUPERUSER owner, as in production, with a forced table of its own.

    Built on the test database, which is disposable; the roles are
    cluster-wide, so they are dropped before (a run that died half-way) and
    after. The table lives in ``public`` because that is the schema the
    policy helper addresses."""
    _drop_probe()
    with _conn(transaction=True) as conn:
        for role in _PROBE_ROLES:
            # A utility statement takes no bind parameters; both are constants.
            conn.execute(
                sa.text(
                    f"CREATE ROLE {role} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD '{_PROBE_PASSWORD}'"
                )
            )
        conn.execute(sa.text("GRANT rls_probe_owner TO rls_probe_member"))
        conn.execute(sa.text("GRANT USAGE, CREATE ON SCHEMA public TO rls_probe_owner"))
        conn.execute(sa.text("GRANT USAGE ON SCHEMA public TO rls_probe_app, rls_probe_member"))
        conn.execute(sa.text("SET LOCAL ROLE rls_probe_owner"))
        conn.execute(sa.text("CREATE TABLE public.rls_probe (org text, v int)"))
        conn.execute(sa.text("ALTER TABLE public.rls_probe ENABLE ROW LEVEL SECURITY"))
        conn.execute(sa.text("ALTER TABLE public.rls_probe FORCE ROW LEVEL SECURITY"))
        conn.execute(
            sa.text(
                "CREATE POLICY tenant ON public.rls_probe "
                "USING (org = current_setting('app.org', true)) "
                "WITH CHECK (org = current_setting('app.org', true))"
            )
        )
        conn.execute(
            sa.text(
                "CREATE FUNCTION public.rls_probe_count() RETURNS bigint LANGUAGE sql "
                "SECURITY DEFINER SET search_path = public "
                "AS 'SELECT count(*) FROM public.rls_probe'"
            )
        )
        conn.execute(sa.text("GRANT SELECT, INSERT, UPDATE ON public.rls_probe TO rls_probe_app"))
        conn.execute(sa.text("GRANT SELECT ON public.rls_probe TO rls_probe_member"))
        conn.execute(sa.text("GRANT EXECUTE ON FUNCTION public.rls_probe_count() TO rls_probe_app"))
    try:
        yield
    finally:
        _drop_probe()


def _scalar_as(role: str, *statements: str) -> object:
    """Run ``statements`` in one transaction logged in as ``role``; the last one's value."""
    url = sa.engine.make_url(os.environ["MYCELIUM_DATABASE_URL_SYNC"])
    engine = sa.create_engine(url.set(username=role, password=_PROBE_PASSWORD), future=True)
    try:
        with engine.begin() as conn:
            *before, last = statements
            for statement in before:
                conn.execute(sa.text(statement))
            result = conn.execute(sa.text(last))
            return result.scalar() if result.returns_rows else None
    finally:
        engine.dispose()


@pytest.mark.usefixtures("owner_probe")
def test_the_owner_policy_opens_the_owner_session_and_nothing_else() -> None:
    """The policy, as shipped, against real roles: no superuser anywhere.

    Each case is the one that would be wrong if the predicate were: the
    owner's own session sees and writes both tenants (the migrate Job); the
    app sees its tenant only, directly and through a SECURITY DEFINER
    function that runs as the owner; with no tenant set the definer sees
    nothing; a role that is a MEMBER of the owner, in its own session, sees
    nothing (``session_user = current_user`` alone would have let it see
    everything); and without the policy the owner's session sees nothing,
    which is the defect the policy closes."""
    from mycelium_core.migration_rls import OWNER_POLICY, create_owner_session_policy

    with _conn(transaction=True) as conn:
        create_owner_session_policy(conn, "rls_probe")

    owner, app, member = "rls_probe_owner", "rls_probe_app", "rls_probe_member"
    _scalar_as(owner, "INSERT INTO public.rls_probe VALUES ('a', 0), ('b', 0)")
    touched = _scalar_as(
        owner,
        "UPDATE public.rls_probe SET v = 1",
        "SELECT count(*) FROM public.rls_probe WHERE v = 1",
    )
    assert touched == 2
    assert _scalar_as(app, "SET app.org = 'a'", "SELECT count(*) FROM public.rls_probe") == 1
    assert _scalar_as(app, "SET app.org = 'a'", "SELECT public.rls_probe_count()") == 1
    assert _scalar_as(app, "SELECT public.rls_probe_count()") == 0
    assert _scalar_as(member, "SELECT count(*) FROM public.rls_probe") == 0

    with _conn(transaction=True) as conn:
        conn.execute(sa.text(f"DROP POLICY {OWNER_POLICY} ON public.rls_probe"))
    assert _scalar_as(owner, "SELECT count(*) FROM public.rls_probe") == 0


def test_a_new_forced_table_with_its_policy_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A migration that creates a forced table and gives it the policy is the
    migration doing its job: it passes, and the new table is said in the log."""
    import mycelium_core.migration_rls as m

    with _rolled_back() as conn:
        before = forced_tables(conn)
        monkeypatch.setattr(m, "role_bypasses_rls", lambda _c: False)
        said: list[str] = []
        with m.owner_sees_all_tenants(conn, log=said.append):
            conn.execute(sa.text("CREATE TABLE public.rls_nuova_probe (id int primary key)"))
            conn.execute(sa.text("ALTER TABLE public.rls_nuova_probe ENABLE ROW LEVEL SECURITY"))
            conn.execute(sa.text("ALTER TABLE public.rls_nuova_probe FORCE ROW LEVEL SECURITY"))
            m.create_owner_session_policy(conn, "rls_nuova_probe")
        after = forced_tables(conn)
        assert "rls_nuova_probe" in after
        assert set(before) <= set(after)
        assert any("rls_nuova_probe" in line for line in said)


def test_a_new_forced_table_without_its_policy_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the policy the next release would lift FORCE from it again,
    under live traffic. The bracket refuses, inside the migration
    transaction, and says which table and which helper."""
    import mycelium_core.migration_rls as m

    with _rolled_back() as conn:
        monkeypatch.setattr(m, "role_bypasses_rls", lambda _c: False)
        with pytest.raises(RuntimeError, match="create_owner_session_policy") as err:
            with m.owner_sees_all_tenants(conn, log=lambda _m: None):
                conn.execute(sa.text("CREATE TABLE public.rls_nuova_probe (id int primary key)"))
                conn.execute(
                    sa.text("ALTER TABLE public.rls_nuova_probe ENABLE ROW LEVEL SECURITY")
                )
                conn.execute(sa.text("ALTER TABLE public.rls_nuova_probe FORCE ROW LEVEL SECURITY"))
        assert "rls_nuova_probe" in str(err.value)


def test_una_tabella_che_perde_force_resta_un_errore(monkeypatch: pytest.MonkeyPatch) -> None:
    """Il controllo esiste per una tabella che il bracket ha sollevato e non ha
    rimesso: li' la tenancy resta allentata dopo la migrazione, ed e' il caso
    che deve continuare a fermare tutto. Serve una tabella che il bracket
    sollevi, quindi le si toglie la policy; poi il ripristino la salta."""
    import mycelium_core.migration_rls as m

    with _rolled_back() as conn:
        vittima = forced_tables(conn)[0]
        _drop_owner_policy(conn, vittima)
        monkeypatch.setattr(m, "role_bypasses_rls", lambda _c: False)

        def _sabota(c: sa.Connection, tables: Sequence[str], on: bool) -> None:
            if not on:
                _set_force(c, tables, on=False)

        monkeypatch.setattr(m, "_set_force", _sabota)
        with pytest.raises(RuntimeError) as err:
            with m.owner_sees_all_tenants(conn, log=lambda _m: None):
                pass
        assert vittima in str(err.value)
        assert "0 tabelle" not in str(err.value)


def test_un_errore_non_lascia_force_spento(monkeypatch: pytest.MonkeyPatch) -> None:
    """Se la migrazione esplode, l'RLS non deve restare allentata.

    Il ripristino e' nel finally, ma la garanzia vera e' che gli ALTER
    stanno nella transazione delle migrazioni: qui si verifica il finally,
    che e' la parte che potrebbe sbagliare da sola."""
    import mycelium_core.migration_rls as m

    with _rolled_back() as conn:
        prima = forced_tables(conn)
        _drop_owner_policy(conn, prima[0])
        monkeypatch.setattr(m, "role_bypasses_rls", lambda _c: False)
        with pytest.raises(RuntimeError, match="migrazione fallita"):
            with m.owner_sees_all_tenants(conn, log=lambda _m: None):
                assert prima[0] not in forced_tables(conn)
                raise RuntimeError("migrazione fallita")
        assert forced_tables(conn) == prima


# Che un ruolo non privilegiato veda zero righe senza il GUC e' gia'
# asserito da test_rls.py::test_fail_closed_without_guc, sul ruolo
# runtime e con il setup a due ruoli: non lo si duplica qui.


# --- Lifting FORCE only when a migration actually runs -----------------------
#
# The tests below are in English while the ones above are not: the rule is that
# new code is written in English, and it binds what is added, not what is here.


def _count_rls_alters() -> tuple[dict[str, int], object]:
    """Count ALTER ... ROW LEVEL SECURITY on every engine, until removed.

    env.py builds its own engine, so the listener goes on the Engine class:
    there is no other handle on the connection alembic will use.
    """
    seen = {"alters": 0, "statements": 0}

    def spy(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        seen["statements"] += 1
        if "ROW LEVEL SECURITY" in statement.upper():
            seen["alters"] += 1

    sa.event.listen(sa.engine.Engine, "before_cursor_execute", spy)
    return seen, spy


def test_a_release_with_no_migration_does_not_touch_the_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one that was red before the fix, and it is the release that failed.

    2.3.34 carried no migration at all, and its migrate Job still emitted 174
    ALTER ... ROW LEVEL SECURITY (out of 183 statements), each an ACCESS
    EXCLUSIVE under a 5s lock_timeout. Two attempts out of three died on one of
    them. An upgrade that applies nothing must leave the catalog alone.
    """
    from alembic import command
    from alembic.config import Config

    import mycelium_core.migration_rls as m

    url = os.environ.get("MYCELIUM_DATABASE_URL_SYNC")
    if not url:
        pytest.skip("MYCELIUM_DATABASE_URL_SYNC non impostata")

    # Production's condition: managed PostgreSQL, owner role is not superuser.
    # Without this the bracket short-circuits and the test measures nothing.
    monkeypatch.setattr(m, "role_bypasses_rls", lambda _c: False)

    root = Path(__file__).resolve().parents[2]
    seen, spy = _count_rls_alters()
    try:
        command.upgrade(Config(str(root / "core" / "alembic.ini")), "head")
    finally:
        sa.event.remove(sa.engine.Engine, "before_cursor_execute", spy)

    assert seen["alters"] == 0, (
        f"an upgrade with nothing to apply emitted {seen['alters']} "
        "ALTER ... ROW LEVEL SECURITY; each one is an ACCESS EXCLUSIVE on a "
        "production table"
    )


def _context_with_steps(conn: sa.Connection, steps: list[object]) -> object:
    """A real MigrationContext whose work function returns ``steps``.

    Same shape alembic.command installs: a callable taking (heads, context).
    """
    from alembic.runtime.migration import MigrationContext

    return MigrationContext.configure(connection=conn, opts={"fn": lambda _h, _c: steps})


def test_pending_steps_answers_both_ways() -> None:
    from mycelium_core.migration_rls import migration_steps_pending

    with _conn() as conn:
        assert migration_steps_pending(_context_with_steps(conn, [])) is False
        assert migration_steps_pending(_context_with_steps(conn, ["a step"])) is True


def test_pending_steps_says_unknown_rather_than_raising() -> None:
    """An alembic upgrade that renames the work function must cost locks, not
    a broken release: the answer degrades to None, which reads as a yes."""
    from mycelium_core.migration_rls import migration_steps_pending

    class _NoWorkFunction:
        pass

    assert migration_steps_pending(_NoWorkFunction()) is None  # type: ignore[arg-type]


def test_the_bracket_lifts_only_the_tables_without_the_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The release that ships 0022, in miniature.

    When it starts the policies do not exist, so the bracket has to lift FORCE
    from exactly the tables without one, let the migration create the
    policies, and put FORCE back. Here one table plays that part."""
    import mycelium_core.migration_rls as m

    with _rolled_back() as conn:
        before = forced_tables(conn)
        table = before[0]
        _drop_owner_policy(conn, table)
        monkeypatch.setattr(m, "role_bypasses_rls", lambda _c: False)
        bracket = m.MigrationRlsBracket(conn, log=lambda _msg: None)
        with bracket.around(_context_with_steps(conn, ["a step"])):
            assert sorted(set(before) - set(forced_tables(conn))) == [table]
            m.create_owner_session_policy(conn, table)
        assert forced_tables(conn) == before
        assert tables_without_owner_policy(conn) == []


def test_the_bracket_skips_when_there_is_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    import mycelium_core.migration_rls as m

    with _conn(transaction=True) as conn:
        before = forced_tables(conn)
        monkeypatch.setattr(m, "role_bypasses_rls", lambda _c: False)
        bracket = m.MigrationRlsBracket(conn, log=lambda _msg: None)
        with bracket.around(_context_with_steps(conn, [])):
            assert forced_tables(conn) == before, "nothing to apply: no ALTER at all"
        assert forced_tables(conn) == before


def test_a_migration_outside_the_bracket_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """What makes the condition safe rather than merely cheap.

    If the prediction is wrong, the migration runs unprepared and its backfills
    touch zero rows in silence, which is the original defect. The guard turns
    that into a loud failure inside the migration transaction, so nothing is
    left applied."""
    import mycelium_core.migration_rls as m

    with _conn(transaction=True) as conn:
        monkeypatch.setattr(m, "role_bypasses_rls", lambda _c: False)
        bracket = m.MigrationRlsBracket(conn, log=lambda _msg: None)

        with pytest.raises(RuntimeError, match="WITHOUT the bracket"):
            bracket.on_version_apply(ctx=None, step="0099_something", heads=set(), run_args={})

        # And it stays quiet for a migration that did run inside the bracket.
        with bracket.around(_context_with_steps(conn, ["a step"])):
            bracket.on_version_apply(ctx=None, step="0099_something", heads=set(), run_args={})


def test_the_guard_is_wired_into_env_py(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard is only worth what its wiring is worth.

    Every other test here calls the bracket directly, so none of them would
    notice ``on_version_apply=`` being dropped from ``context.configure`` in
    env.py, which is precisely the mistake that would leave a wrong prediction
    silent again. So: make the prediction lie, run a real migration step
    through the real env.py, and require that it is refused and rolled back.
    """
    from alembic import command
    from alembic.config import Config

    import mycelium_core.migration_rls as m

    url = os.environ.get("MYCELIUM_DATABASE_URL_SYNC")
    if not url:
        pytest.skip("MYCELIUM_DATABASE_URL_SYNC non impostata")

    root = Path(__file__).resolve().parents[2]
    cfg = Config(str(root / "core" / "alembic.ini"))

    def _revision() -> str | None:
        with _conn() as conn:
            return conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()

    before = _revision()
    # The lie: "nothing to apply" while a downgrade step is about to run.
    monkeypatch.setattr(m, "migration_steps_pending", lambda _ctx: False)
    try:
        with pytest.raises(RuntimeError, match="WITHOUT the bracket"):
            command.downgrade(cfg, "-1")
        assert _revision() == before, (
            "the guard fired but the transaction was not rolled back: the schema moved"
        )
    finally:
        monkeypatch.undo()
        # Belt and braces: if the rollback above ever fails, the shared test
        # database must not be left one revision behind for every later test.
        if _revision() != before:
            command.upgrade(cfg, "head")


def test_the_release_that_ships_0022_through_the_real_env_py(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production release itself: a database at 0021 with no owner policy,
    an owner that is not a superuser, ``upgrade head`` through env.py.

    That release must lift FORCE from every forced table once (the policies do
    not exist yet when it starts), leave every one covered, and put FORCE
    back. The release after it, with a migration to apply, must lift none."""
    from alembic import command
    from alembic.config import Config

    import mycelium_core.migration_rls as m

    if not os.environ.get("MYCELIUM_DATABASE_URL_SYNC"):
        pytest.skip("MYCELIUM_DATABASE_URL_SYNC non impostata")
    cfg = Config(str(Path(__file__).resolve().parents[2] / "core" / "alembic.ini"))

    with _conn(transaction=True) as conn:
        before = forced_tables(conn)
        assert before
        rls_tables = (
            conn.execute(
                sa.text(
                    "SELECT c.relname FROM pg_class c JOIN pg_namespace n "
                    "ON n.oid = c.relnamespace WHERE n.nspname = 'public' "
                    "AND c.relkind IN ('r','p') AND c.relrowsecurity"
                )
            )
            .scalars()
            .all()
        )
        for table in rls_tables:
            _drop_owner_policy(conn, table)
    # This replays the release that shipped 0022, so it upgrades TO 0022, not
    # to head. Stamping 0021 and upgrading to head would re-run every later
    # migration against a schema that already has it (0023 failed exactly so,
    # with a duplicate column). The later ones are stepped down for real
    # first, then back up at the end.
    command.downgrade(cfg, "0022")
    command.stamp(cfg, "0021")
    try:
        monkeypatch.setattr(m, "role_bypasses_rls", lambda _c: False)
        seen, spy = _count_rls_alters()
        try:
            command.upgrade(cfg, "0022")
        finally:
            sa.event.remove(sa.engine.Engine, "before_cursor_execute", spy)
        assert seen["alters"] == 2 * len(before), "lifted and restored once per table"
        with _conn() as conn:
            assert forced_tables(conn) == before
            assert tables_without_owner_policy(conn) == []

        # The next release: 0022's downgrade is empty, so stepping back and
        # forth through it is a release with a migration to apply.
        command.stamp(cfg, "0021")
        seen, spy = _count_rls_alters()
        try:
            command.upgrade(cfg, "0022")
        finally:
            sa.event.remove(sa.engine.Engine, "before_cursor_execute", spy)
        assert seen["alters"] == 0, f"{seen['alters']} ALTER ... ROW LEVEL SECURITY emitted"
    finally:
        # Whatever happened above, rebuild the policies through 0022 itself, so
        # a failure here does not leave every later test an uncovered schema.
        monkeypatch.undo()
        command.stamp(cfg, "0021")
        command.upgrade(cfg, "0022")
        command.upgrade(cfg, "head")
        with _conn() as conn:
            assert tables_without_owner_policy(conn) == [], "the test left the schema uncovered"
