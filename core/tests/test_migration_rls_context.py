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
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import sqlalchemy as sa

from mycelium_core.migration_rls import (
    _set_force,
    forced_tables,
    owner_sees_all_tenants,
    role_bypasses_rls,
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


def test_percorso_produzione_solleva_force_e_lo_rimette(monkeypatch: pytest.MonkeyPatch) -> None:
    """La prova che conta: il percorso che scatta in PRODUZIONE.

    In locale il ruolo e' superuser e il contesto va in corto circuito, il
    che e' giusto ma lascia il ramo di produzione senza copertura. Qui si
    finge il ruolo non-privilegiato e si verifica sullo schema vero che
    FORCE venga sollevato su tutte le tabelle e rimesso identico."""
    import mycelium_core.migration_rls as m

    with _conn(transaction=True) as conn:
        prima = forced_tables(conn)
        assert prima, "lo schema dovrebbe avere tabelle con FORCE"
        monkeypatch.setattr(m, "role_bypasses_rls", lambda _c: False)
        with m.owner_sees_all_tenants(conn, log=lambda _m: None):
            assert forced_tables(conn) == [], (
                "dentro il blocco il proprietario deve vedere ogni tenant"
            )
        assert forced_tables(conn) == prima, "FORCE va rimesso esattamente com'era"


def test_una_tabella_nuova_con_force_non_e_una_violazione(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Il caso che ha fermato un rilascio in produzione.

    Il bracket solleva FORCE dalle tabelle che ce l'hanno e lo rimette
    all'uscita. Una migrazione che CREA una tabella con FORCE la aggiunge
    all'insieme di uscita senza che sia mai stata in quello di ingresso, e il
    controllo confrontava i due per disuguaglianza: sollevava, e calcolava le
    mancanti come ingresso meno uscita, che li' e' vuoto. La Job moriva con
    "non ripristinato su 0 tabelle:" e la lista vuota, cioe' un messaggio che
    non si puo' nemmeno leggere.

    Non era mai scattato perche' dalla baseline nessuna migrazione aveva
    ancora creato una tabella con FORCE. La prima che lo ha fatto ha trovato
    un guard che rifiuta per costruzione ogni tabella org-scoped futura.

    Qui la creazione e' simulata dentro il blocco, che e' esattamente dove una
    migrazione la farebbe.
    """
    import mycelium_core.migration_rls as m

    # Tolta INCONDIZIONATAMENTE, e con una connessione sua: ``_conn`` apre con
    # ``begin()``, quindi su uscita pulita COMMITTA, e una tabella creata qui
    # sopravvive al test. La prima stesura non lo faceva e ha lasciato la
    # tabella nel database, dove ha fatto fallire la corsa successiva con
    # "relation already exists" -- un test che sporca lo schema che sta
    # verificando.
    def _drop() -> None:
        with _conn(transaction=True) as c:
            c.execute(sa.text("DROP TABLE IF EXISTS public.rls_nuova_probe"))

    _drop()
    try:
        with _conn(transaction=True) as conn:
            prima = forced_tables(conn)
            assert prima, "lo schema dovrebbe avere tabelle con FORCE"
            monkeypatch.setattr(m, "role_bypasses_rls", lambda _c: False)
            detto: list[str] = []
            with m.owner_sees_all_tenants(conn, log=detto.append):
                conn.execute(sa.text("CREATE TABLE public.rls_nuova_probe (id int primary key)"))
                conn.execute(
                    sa.text("ALTER TABLE public.rls_nuova_probe ENABLE ROW LEVEL SECURITY")
                )
                conn.execute(sa.text("ALTER TABLE public.rls_nuova_probe FORCE ROW LEVEL SECURITY"))

            dopo = forced_tables(conn)
            assert "rls_nuova_probe" in dopo, "la tabella nuova deve restare forzata"
            assert set(prima) <= set(dopo), "e tutto cio' che era forzato prima deve esserlo ancora"
            # Detta, non ingoiata: e' l'unico posto dove qualcuno la legge.
            assert any("rls_nuova_probe" in riga for riga in detto)
    finally:
        _drop()


def test_una_tabella_che_perde_force_resta_un_errore(monkeypatch: pytest.MonkeyPatch) -> None:
    """L'altra direzione, che il fix non deve avere allentato.

    Il controllo esiste per una tabella che il bracket ha sollevato e non ha
    rimesso: li' la tenancy resta allentata dopo la migrazione, ed e' il caso
    che deve continuare a fermare tutto. Simulato togliendo FORCE a una
    tabella dentro il blocco, cosi' il ripristino la rimette e qualcosa gliela
    toglie di nuovo -- che e' la forma di una migrazione che la disabilita
    senza accorgersene.
    """
    import mycelium_core.migration_rls as m

    with _conn(transaction=True) as conn:
        prima = forced_tables(conn)
        assert prima, "lo schema dovrebbe avere tabelle con FORCE"
        vittima = prima[0]
        monkeypatch.setattr(m, "role_bypasses_rls", lambda _c: False)

        def _sabota(c: sa.Connection, tables: object, on: bool) -> None:
            if on:
                # Il ripristino rimette tutte tranne una.
                _set_force(c, [t for t in prima if t != vittima], on=True)
            else:
                _set_force(c, prima, on=False)

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

    with _conn(transaction=True) as conn:
        prima = forced_tables(conn)
        monkeypatch.setattr(m, "role_bypasses_rls", lambda _c: False)
        with pytest.raises(RuntimeError, match="migrazione fallita"):
            with m.owner_sees_all_tenants(conn, log=lambda _m: None):
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


def test_the_bracket_still_lifts_when_there_is_work(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half: the condition must not break the case the module exists
    for. With a step to apply, FORCE is lifted on every forced table and put
    back exactly as it was."""
    import mycelium_core.migration_rls as m

    with _conn(transaction=True) as conn:
        before = forced_tables(conn)
        assert before, "lo schema dovrebbe avere tabelle con FORCE"
        monkeypatch.setattr(m, "role_bypasses_rls", lambda _c: False)
        bracket = m.MigrationRlsBracket(conn, log=lambda _msg: None)
        with bracket.around(_context_with_steps(conn, ["a step"])):
            assert forced_tables(conn) == [], "inside the bracket the owner sees every tenant"
        assert forced_tables(conn) == before


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
