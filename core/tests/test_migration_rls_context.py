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

import pytest
import sqlalchemy as sa

from mycelium_core.migration_rls import (
    _set_force,
    forced_tables,
    owner_sees_all_tenants,
    role_bypasses_rls,
)


def _engine() -> sa.Engine:
    url = os.environ.get("MYCELIUM_DATABASE_URL_SYNC")
    if not url:
        pytest.skip("MYCELIUM_DATABASE_URL_SYNC non impostata")
    return sa.create_engine(url, future=True)


def test_dove_il_ruolo_scavalca_gia_rls_non_tocca_niente() -> None:
    """In sviluppo e CI il ruolo e' superuser: il contesto non deve
    emettere nessun ALTER, quindi nessun lock e nessuna differenza."""
    with _engine().connect() as conn:
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
    with _engine().begin() as conn:
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
    with _engine().connect() as conn:
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

    with _engine().begin() as conn:
        prima = forced_tables(conn)
        assert prima, "lo schema dovrebbe avere tabelle con FORCE"
        monkeypatch.setattr(m, "role_bypasses_rls", lambda _c: False)
        with m.owner_sees_all_tenants(conn, log=lambda _m: None):
            assert forced_tables(conn) == [], (
                "dentro il blocco il proprietario deve vedere ogni tenant"
            )
        assert forced_tables(conn) == prima, "FORCE va rimesso esattamente com'era"


def test_un_errore_non_lascia_force_spento(monkeypatch: pytest.MonkeyPatch) -> None:
    """Se la migrazione esplode, l'RLS non deve restare allentata.

    Il ripristino e' nel finally, ma la garanzia vera e' che gli ALTER
    stanno nella transazione delle migrazioni: qui si verifica il finally,
    che e' la parte che potrebbe sbagliare da sola."""
    import mycelium_core.migration_rls as m

    with _engine().begin() as conn:
        prima = forced_tables(conn)
        monkeypatch.setattr(m, "role_bypasses_rls", lambda _c: False)
        with pytest.raises(RuntimeError, match="migrazione fallita"):
            with m.owner_sees_all_tenants(conn, log=lambda _m: None):
                raise RuntimeError("migrazione fallita")
        assert forced_tables(conn) == prima


# Che un ruolo non privilegiato veda zero righe senza il GUC e' gia'
# asserito da test_rls.py::test_fail_closed_without_guc, sul ruolo
# runtime e con il setup a due ruoli: non lo si duplica qui.
