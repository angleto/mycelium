"""Mycelium core: domain and service layer.

The single source of truth for business logic, RBAC, (org, project)
isolation, optimistic concurrency, the state machine, the scheduler,
memory and SDI. api/ and mcp/ are thin adapters over this package
(see docs/adr/0001).
"""

__version__ = "0.0.0"
