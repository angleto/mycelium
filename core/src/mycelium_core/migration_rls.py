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
policy comunque, con o senza FORCE. Sollevarlo per la durata della
transazione di migrazione ripristina esattamente la semantica che
l'ADR-0015 assumeva, senza toccare l'isolamento di ``mycelium_app`` e
senza bisogno del superuser.

Garanzie:

- **Nulla cambia dove non serve.** Se il ruolo scavalca gia' l'RLS
  (superuser o BYPASSRLS), non viene eseguito nessun ALTER: niente lock,
  nessuna differenza di comportamento in sviluppo e in CI.
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
    dalle tabelle che ce l'hanno e lo rimette identico all'uscita.
    """
    if role_bypasses_rls(conn):
        # Sviluppo e CI: il ruolo e' superuser, l'invariante c'e' gia'.
        yield
        return

    lifted = forced_tables(conn)
    if not lifted:
        yield
        return

    log(
        f"rls: il ruolo delle migrazioni non scavalca l'RLS; "
        f"sollevo FORCE da {len(lifted)} tabelle per la durata della migrazione"
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
    added = sorted(set(restored) - set(lifted))
    if added:
        # Detto invece che ignorato: una tabella con FORCE che compare durante
        # una migrazione e' un cambio di schema che vale la pena leggere nel
        # log della Job, ed e' l'unico posto dove qualcuno lo guarda.
        log(f"rls: FORCE anche su {len(added)} tabelle nuove: {', '.join(added)}")
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

    What it does not cover: a release that does carry a migration still
    lifts FORCE on every forced table, because a migration does not
    declare which tables its backfills touch. That is a wider change than
    this one.
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
