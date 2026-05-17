"""App-level envelope for opaque secrets (ADR-0006, ADR-0023).

OAuth refresh tokens and IMAP passwords are not indexed, so they get a
Fernet envelope (key from ``FLOW_SECRET_KEY``, fail-closed) rather than
relying on volume encryption alone. Threat model: protects a stolen
volume/snapshot and a live DB connection without the app key; it does
not protect a compromised app process.
"""

from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet

from flow_core.config import get_settings


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    return Fernet(get_settings().secret_key.encode())


def encrypt_secret(plaintext: str) -> str:
    """Return the Fernet ciphertext (urlsafe str) for an opaque secret."""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    """Inverse of :func:`encrypt_secret`."""
    return _fernet().decrypt(ciphertext.encode()).decode()
