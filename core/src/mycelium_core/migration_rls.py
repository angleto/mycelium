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

import sqlalchemy as sa
from sqlalchemy.engine import Connection

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
    if restored != lifted:
        missing = sorted(set(lifted) - set(restored))
        raise RuntimeError(
            "rls: FORCE ROW LEVEL SECURITY non ripristinato su "
            f"{len(missing)} tabelle: {', '.join(missing)}"
        )
    log(f"rls: FORCE ripristinato su {len(restored)} tabelle")
