"""Eccezioni di dominio.

Sollevate dal service layer e mappate dagli adapter (api/mcp) a codici
di errore. ``ConflictError`` corrisponde al 409 dell'optimistic
concurrency (docs/adr/0002).
"""

from __future__ import annotations


class DomainError(Exception):
    """Base di tutti gli errori di dominio."""


class NotFoundError(DomainError):
    """Entita inesistente o non visibile nel contesto tenant corrente."""


class ConflictError(DomainError):
    """Scrittura su versione stale: optimistic concurrency (409)."""


class AuthError(DomainError):
    """Credenziali non valide o token assente/scaduto (401)."""


class ForbiddenError(DomainError):
    """Ruolo insufficiente nel contesto org corrente (403)."""
