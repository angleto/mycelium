"""Far vedere alle migrazioni le righe di TUTTI i tenant.

ADR-0015 fonda il disegno su un fatto: "a superuser always bypasses RLS
(even with FORCE)", e stabilisce che le migrazioni girano come ruolo
proprietario mentre l'app gira come ``mycelium_app``. Sotto quell'assunto
un backfill vede tutto e funziona.

In produzione l'assunto e' falso. Su PostgreSQL gestito (Scaleway) il
ruolo proprietario NON e' superuser::

    sviluppo/CI (immagine postgres)   rolsuper=t  rolbypassrls=t
    produzione (managed)              rolsuper=f  rolbypassrls=f

Le policy sono ``org_id = nullif(current_setting('app.current_org',
true),'')::uuid``, cioe' fail-closed: senza GUC nessuna riga. Una
migrazione non imposta nessun GUC, quindi ogni ``UPDATE``/``DELETE`` su
una tabella org-scoped tocca ZERO righe **senza sollevare errori**.

Il difetto e' invisibile ovunque tranne che in produzione: in locale il
ruolo e' superuser e i test passano. Era gia' stato incontrato una volta
(vedi la docstring della 0037, che lo aggira a mano per le sole ``tasks``)
ma il runner non era mai stato sistemato, e ogni backfill successivo ci e'
ricaduto.

QUESTA E' LA CORREZIONE CENTRALE. ``FORCE ROW LEVEL SECURITY`` esiste per
vincolare il PROPRIETARIO: un ruolo non proprietario e' soggetto alle
policy comunque, con o senza FORCE. E il proprietario va vincolato davvero
quando agisce per conto dell'app, cioe' dentro le funzioni e i trigger
SECURITY DEFINER che ``mycelium_app`` invoca: per questo FORCE non si toglie.

Una migrazione e' l'altro caso: e' il proprietario che si e' COLLEGATO come
proprietario. PostgreSQL distingue i due con ``session_user``::

    migrate Job                          current_user=owner  session_user=owner
    definer invocato da mycelium_app     current_user=owner  session_user=mycelium_app
    query di mycelium_app                current_user=app    session_user=mycelium_app

Ogni tabella con RLS porta quindi una policy permissiva ``owner_session_all``,
riservata al proprietario e vera solo quando anche la sessione e' sua (vedi
``owner_policy_statement``). Il migrate vede ogni tenant senza nessun ALTER;
un definer invocato dall'app resta vincolato alle policy di tenant, e l'app
non puo' cambiare ``session_user`` (``SET SESSION AUTHORIZATION`` vuole un
superuser, ``SET ROLE`` cambia solo ``current_user``). La policy non da' al
proprietario niente che non abbia gia': con le sue credenziali si puo' fare
``NO FORCE``, che e' esattamente quello che questo modulo faceva prima.

Prima della policy il modulo sollevava FORCE da tutte le tabelle che ce
l'hanno, con un ACCESS EXCLUSIVE ciascuna a traffico vivo. Release 2.3.50: 89
tabelle, e il primo pod del migrate e' morto in ``DeadlockDetected`` su
``organizations`` contro una transazione dell'app. Nessun ordine dei lock lo
evita, perche' l'ordine in cui l'app tocca le tabelle e' arbitrario: l'unico
lock che non partecipa a un ciclo e' quello che non si prende.

Garanzie:

- **Nulla cambia dove non serve.** Se il ruolo scavalca gia' l'RLS
  (superuser o BYPASSRLS), non viene eseguito niente: sviluppo e CI.
- **FORCE si solleva solo dove la policy manca.** A regime nessuna tabella:
  zero ALTER. Il sollevamento resta per la transizione (la migrazione che
  crea le policy gira prima che esistano) e per una tabella che ne fosse
  rimasta senza.
- **All'uscita la policy c'e' su ogni tabella con RLS**, o la transazione
  fallisce e il rollback annulla la migrazione. Una tabella nuova con RLS
  nasce con ``create_owner_session_policy``.
- **Il ripristino non dipende dal percorso felice.** Gli ALTER stanno
  nella stessa transazione delle migrazioni: se qualcosa fallisce, il
  rollback li annulla insieme al resto. Sul percorso riuscito il
  ripristino e' esplicito e viene verificato.
- **Non si aspetta all'infinito.** Un ``lock_timeout`` evita che gli
  ALTER si mettano in coda dietro una query lunga trascinandosi dietro
  l'applicazione: meglio fallire il deploy che bloccare la produzione.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.engine import Connection

if TYPE_CHECKING:  # alembic is a migration-time dependency, not a runtime one
    from alembic.runtime.migration import MigrationContext

# Gli ALTER sono modifiche di solo catalogo (istantanee), ma prendono un
# ACCESS EXCLUSIVE: se una query lunga tiene la tabella, meglio fallire
# subito che accodare tutto il traffico dietro di noi.
LOCK_TIMEOUT = "5s"


def role_bypasses_rls(conn: Connection) -> bool:
    """Se il ruolo corrente vede le righe di ogni tenant senza aiuto.

    Vero per un superuser o per un ruolo con BYPASSRLS: e' il caso di
    sviluppo e CI, dove questo modulo non deve fare assolutamente nulla.
    """
    return bool(
        conn.execute(
            sa.text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).scalar()
    )


def forced_tables(conn: Connection) -> list[str]:
    """Le tabelle con FORCE ROW LEVEL SECURITY attivo, in ordine stabile.

    Include i PADRI partizionati (``relkind='p'``, es. ``memory_blobs``):
    dimenticarli e' precisamente il modo in cui una lettura torna vuota
    senza dirlo, perche' il padre filtra mentre le partizioni no.
    """
    rows = (
        conn.execute(
            sa.text(
                "SELECT c.relname FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind IN ('r','p') "
                "  AND c.relforcerowsecurity "
                "ORDER BY c.relname"
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


OWNER_POLICY = "owner_session_all"

# The predicate names the owner instead of comparing session_user with
# current_user. "TO <owner>" also applies to every role that is a MEMBER of the
# owner, and for such a role, logged in as itself, session_user = current_user
# holds: the shorter predicate would hand it every tenant. Naming the owner
# leaves no role but the owner, logged in as the owner.
#
# Both strings are format() templates that PostgreSQL fills in (%I quotes an
# identifier, %L a literal), passed as bind parameters: no name is ever
# concatenated into SQL here.
_OWNER_POLICY_TEMPLATE = (
    "CREATE POLICY %1$I ON public.%2$I AS PERMISSIVE FOR ALL TO %3$I "
    "USING (session_user = %3$L::name AND current_user = %3$L::name) "
    "WITH CHECK (session_user = %3$L::name AND current_user = %3$L::name)"
)

# How PostgreSQL renders that predicate back (pg_get_expr), measured on 16.15
# and 17.11. The check compares against it so that a policy with the right name
# and a different body does not count.
_OWNER_PREDICATE_RENDERED = "((SESSION_USER = %1$L::name) AND (CURRENT_USER = %1$L::name))"


def tables_without_owner_policy(conn: Connection) -> list[str]:
    """The tables under row level security that lack the owner-session policy.

    Every table with RLS enabled must carry it, not only the forced ones. Inside
    the bracket FORCE is lifted, so "forced" is not a property a migration can
    read there: the first version asked for forced tables, and the migration
    that creates the policies, running with FORCE lifted, found none and
    created nothing. On a table without FORCE the policy is inert (the owner
    is not subject to RLS there), and a table that gets FORCE later is
    already covered.

    A table counts as covered only if it carries ``owner_session_all`` exactly:
    permissive, for every command, granted to the table's owner and nobody
    else, with the owner-session predicate in both USING and WITH CHECK.
    """
    rows = (
        conn.execute(
            sa.text(
                "SELECT c.relname FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind IN ('r','p') "
                "  AND c.relrowsecurity "
                "  AND NOT EXISTS ("
                "        SELECT 1 FROM pg_policy p"
                "         WHERE p.polrelid = c.oid"
                "           AND p.polname = :name"
                "           AND p.polcmd = '*'"
                "           AND p.polpermissive"
                "           AND p.polroles = ARRAY[c.relowner]"
                "           AND pg_get_expr(p.polqual, p.polrelid)"
                "               = format(:rendered, pg_get_userbyid(c.relowner))"
                "           AND pg_get_expr(p.polwithcheck, p.polrelid)"
                "               = format(:rendered, pg_get_userbyid(c.relowner))"
                "      ) "
                "ORDER BY c.relname"
            ),
            {"name": OWNER_POLICY, "rendered": _OWNER_PREDICATE_RENDERED},
        )
        .scalars()
        .all()
    )
    return list(rows)


def owner_policy_statement(conn: Connection, table: str) -> str:
    """The CREATE POLICY that lets ``table``'s owner, in its own session, see every tenant."""
    statement = conn.execute(
        sa.text(
            "SELECT format(:template, CAST(:name AS text), c.relname, "
            "              pg_get_userbyid(c.relowner)) "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relname = :table"
        ),
        {"template": _OWNER_POLICY_TEMPLATE, "name": OWNER_POLICY, "table": table},
    ).scalar()
    if statement is None:
        raise LookupError(f"rls: no table public.{table}")
    return str(statement)


def create_owner_session_policy(conn: Connection, table: str) -> None:
    """Give ``table`` its owner-session policy. Call it wherever a table gets RLS.

    In a migration: ``create_owner_session_policy(op.get_bind(), "my_table")``
    right after ``ALTER TABLE my_table ENABLE ROW LEVEL SECURITY``. The runner
    refuses to commit a migration that leaves a table under RLS without it.
    """
    conn.exec_driver_sql(owner_policy_statement(conn, table))


def _set_force(conn: Connection, tables: Sequence[str], on: bool) -> None:
    verb = "FORCE" if on else "NO FORCE"
    for t in tables:
        conn.execute(sa.text(f'ALTER TABLE public."{t}" {verb} ROW LEVEL SECURITY'))


@contextmanager
def owner_sees_all_tenants(
    conn: Connection, *, log: Callable[[str], None] = print
) -> Iterator[None]:
    """Per la durata del blocco, il proprietario vede ogni tenant.

    No-op quando il ruolo scavalca gia' l'RLS. Altrimenti solleva FORCE
    dalle sole tabelle senza la policy ``owner_session_all`` (a regime
    nessuna), lo rimette identico all'uscita, e rifiuta di uscire se una
    tabella con RLS e' rimasta senza la policy.
    """
    if role_bypasses_rls(conn):
        # Sviluppo e CI: il ruolo e' superuser, l'invariante c'e' gia'.
        yield
        return

    before = forced_tables(conn)
    uncovered_before = set(tables_without_owner_policy(conn))
    lifted = [t for t in before if t in uncovered_before]
    if lifted:
        log(
            f"rls: {len(lifted)} tabelle con FORCE non hanno la policy {OWNER_POLICY}; "
            "sollevo FORCE da quelle per la durata della migrazione"
        )
        conn.execute(sa.text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
        _set_force(conn, lifted, on=False)
    try:
        yield
    finally:
        # Sul percorso di errore questo puo' non arrivare a completarsi: non
        # importa, gli ALTER sono nella transazione delle migrazioni e il
        # rollback li annulla. Su quello riuscito, invece, deve tornare
        # esattamente com'era, e lo verifichiamo.
        if lifted:
            _set_force(conn, lifted, on=True)

    restored = forced_tables(conn)
    # L'invariante e' "tutto cio' che ho sollevato e' tornato", non "l'insieme
    # e' identico". Una migrazione che CREA una tabella con FORCE la aggiunge a
    # ``restored`` senza che sia mai stata in ``lifted``, ed e' la migrazione
    # che fa il suo lavoro, non una violazione.
    #
    # Il confronto era su disuguaglianza e sollevava proprio li'. Il messaggio
    # lo diceva e nessuno poteva leggerlo: calcolava ``lifted - restored``, che
    # in quel caso e' vuoto, quindi la Job moriva su "non ripristinato su 0
    # tabelle:" con la lista vuota. Non e' mai scattato prima perche' dalla
    # baseline nessuna migrazione aveva ancora creato una tabella con FORCE;
    # la prima che lo ha fatto ha trovato un guard che rifiuta per costruzione
    # ogni tabella org-scoped futura.
    missing = sorted(set(lifted) - set(restored))
    if missing:
        raise RuntimeError(
            "rls: FORCE ROW LEVEL SECURITY non ripristinato su "
            f"{len(missing)} tabelle: {', '.join(missing)}"
        )
    added = sorted(set(restored) - set(before))
    if added:
        # Detto invece che ignorato: una tabella con FORCE che compare durante
        # una migrazione e' un cambio di schema che vale la pena leggere nel
        # log della Job, ed e' l'unico posto dove qualcuno lo guarda.
        log(f"rls: FORCE anche su {len(added)} tabelle nuove: {', '.join(added)}")
    uncovered = tables_without_owner_policy(conn)
    if uncovered:
        # Without the policy the next release would have to lift FORCE from
        # these again, under live traffic, which is the deadlock this module
        # exists to avoid. Refused here, inside the migration transaction, so
        # the migration that created them is rolled back with it.
        raise RuntimeError(
            f"rls: {len(uncovered)} tabelle con RLS senza la policy {OWNER_POLICY}: "
            f"{', '.join(uncovered)}. Una migrazione che abilita l'RLS su una tabella "
            "deve chiamare mycelium_core.migration_rls.create_owner_session_policy."
        )
    if lifted:
        log(f"rls: FORCE ripristinato su {len(lifted)} tabelle")


def migration_steps_pending(migration_context: MigrationContext) -> bool | None:
    """Whether this alembic run is going to apply anything at all.

    ``None`` means "could not tell", and a caller must read it as a yes:
    being wrong that way costs locks, being wrong the other way costs a
    backfill that touches zero rows in silence, which is the very defect
    this module exists to close.

    It asks the same function ``run_migrations()`` will iterate over
    (``_migrations_fn``, the work function ``alembic.command`` installs)
    instead of comparing the database revision against the script head.
    That public comparison is equivalent only for ``upgrade head``: it
    answers wrong for ``upgrade <intermediate>``, for ``downgrade`` and
    for ``stamp``. Measured: ``_upgrade_revs`` returns a plain list built
    from the revision map, so asking here and letting ``run_migrations``
    ask again costs nothing and touches no database.

    What it does not cover: the attribute is private, so an alembic
    upgrade can take it away. Hence ``None`` rather than an exception (an
    upgrade must not break a release, only bring back the locks it used
    to take), and hence the ``on_version_apply`` guard that catches a
    wrong answer.
    """
    fn = getattr(migration_context, "_migrations_fn", None)
    if fn is None:
        return None
    try:
        steps = list(fn(migration_context.get_current_heads(), migration_context))
    except Exception:
        # Deliberately wide: any surprise here must degrade to the old
        # behaviour (prepare anyway), never fail a release. A target alembic
        # itself rejects raises the same error a moment later, from
        # run_migrations, where it belongs.
        return None
    return bool(steps)


class MigrationRlsBracket:
    """The RLS preparation, and the guard that checks it was not skipped.

    Two points that have to agree. ``around`` decides whether to lift
    FORCE, and lifts it only when there is at least one revision to
    apply. ``on_version_apply`` checks afterwards that no migration ran
    outside that bracket.

    Why the decision is not enough on its own: if ``migration_steps_pending``
    answers "nothing to do" and is wrong, the migration runs without the
    owner seeing the tenants, which is the original defect back and just
    as mute. The guard makes it loud and harmless instead. It arrives
    after the migration has run (alembic calls the callbacks AFTER
    ``step.migration_fn``, checked in the 1.18.4 source), too late to
    prepare but in time to fail the transaction, which rolls back the
    migration that ran against nothing.

    The measurement that decided the cut: on a database already at head,
    ``upgrade head`` emitted 174 ``ALTER ... ROW LEVEL SECURITY`` out of
    183 statements. Release 2.3.34, which carried no migration at all,
    failed two attempts out of three on ``lock_timeout`` on one of them.

    What it does not cover: lifting FORCE when there is work to do is
    ``owner_sees_all_tenants``'s job, and since the owner-session policy it
    lifts nothing at all on a schema where every forced table has the policy.
    A migration that carries real DDL still takes the locks of that DDL.
    """

    def __init__(self, conn: Connection, *, log: Callable[[str], None] = print) -> None:
        self._conn = conn
        self._log = log
        self._prepared = False

    @contextmanager
    def around(self, migration_context: MigrationContext) -> Iterator[None]:
        pending = migration_steps_pending(migration_context)
        if pending is False:
            self._log("rls: no revision to apply; FORCE ROW LEVEL SECURITY left alone")
            yield
            return
        if pending is None:
            self._log(
                "rls: could not tell whether there is anything to migrate, preparing "
                "anyway (alembic changed its work function: see migration_steps_pending)"
            )
        with owner_sees_all_tenants(self._conn, log=self._log):
            self._prepared = True
            try:
                yield
            finally:
                self._prepared = False

    def on_version_apply(self, **kw: object) -> None:
        """Refuse a migration that ran outside the preparation bracket."""
        if self._prepared:
            return
        step = kw.get("step")
        raise RuntimeError(
            f"rls: migration {step} ran WITHOUT the bracket that lifts FORCE ROW "
            "LEVEL SECURITY, so its backfills may have touched zero rows in every "
            "tenant without saying so. The transaction is being rolled back, so "
            "nothing was applied. Cause: migration_steps_pending() answered "
            "'nothing to apply' and was wrong; recheck it against the installed "
            "alembic version."
        )
