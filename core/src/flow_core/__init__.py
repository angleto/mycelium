"""Flow core: dominio e service layer.

Unico punto di verita per business logic, RBAC, isolamento (org, progetto),
optimistic concurrency, macchina a stati, scheduler, memoria, SDI.
api/ e mcp/ sono adapter sottili su questo pacchetto (vedi docs/adr/0001).
"""

__version__ = "0.0.0"
