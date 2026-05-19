"""Pluggable attachment byte store (same seam as the LLM/embedder).

A ``Protocol`` + a config-driven factory + a test override, exactly
like ``ai_providers``/``embedder``. Two backends:

- ``PgAttachmentStore`` (DEFAULT): a thin marker. The bytes stay in the
  ``attachments.data`` BYTEA column, atomic with the row, no external
  dependency -- today's behaviour, byte-for-byte. The service detects a
  PG store and keeps the legacy column read/write path (the store's own
  ``put``/``get``/``delete`` are deliberately unused for PG, so nothing
  about the existing API/SPA/E2E changes).
- ``S3AttachmentStore``: offloads the bytes to an S3-compatible object
  store (Scaleway Object Storage). ``boto3`` is imported lazily inside
  the methods so importing this module never requires boto3 and the
  test path never touches the network. The real impl is ``# pragma: no
  cover`` (network/credentials, like ``LocalLLM``).

The on-the-wire HTTP contract is unaffected by the choice: only where
the bytes physically live changes. ``storage_key`` is an internal
column, never surfaced to the client.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from flow_core.config import Settings


@runtime_checkable
class AttachmentStore(Protocol):
    async def put(self, key: str, data: bytes, content_type: str) -> None: ...

    async def get(self, key: str) -> bytes: ...

    async def delete(self, key: str) -> None: ...


class PgAttachmentStore:
    """The default. A marker, not a real byte mover: when this store is
    active the service keeps the legacy ``attachments.data`` column path
    (write the bytes into the row, read them back from the row). Its
    methods are never called on the PG path, so behaviour is identical
    to before this seam existed; they raise loudly if mis-wired."""

    backend = "pg"

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        raise RuntimeError("PgAttachmentStore stores bytes in the row, not via put()")

    async def get(self, key: str) -> bytes:
        raise RuntimeError("PgAttachmentStore reads bytes from the row, not via get()")

    async def delete(self, key: str) -> None:
        raise RuntimeError("PgAttachmentStore deletes bytes with the row, not via delete()")


class S3AttachmentStore:
    """S3-compatible object store (Scaleway). ``boto3`` is imported
    lazily so this module is import-safe without boto3 and tests never
    hit the network. Keys are namespaced under an optional prefix."""

    backend = "s3"

    def __init__(
        self,
        *,
        endpoint_url: str,
        region_name: str,
        bucket: str,
        aws_access_key_id: str,
        aws_secret_access_key: str,
        prefix: str = "",
    ) -> None:
        self._endpoint_url = endpoint_url
        self._region_name = region_name
        self._bucket = bucket
        self._access_key_id = aws_access_key_id
        self._secret_access_key = aws_secret_access_key
        self._prefix = prefix.strip("/")

    def _full_key(self, key: str) -> str:
        return f"{self._prefix}/{key}" if self._prefix else key

    def _client(self) -> Any:  # pragma: no cover - network/creds
        # boto3 ships no inline types; the repo convention for untyped
        # third-party libs is ignore_missing_imports (see tool.mypy
        # overrides), so the S3 client is Any. Lazy import: this module
        # stays import-safe without boto3 and tests never hit it.
        import boto3

        return boto3.client(
            "s3",
            endpoint_url=self._endpoint_url,
            region_name=self._region_name,
            aws_access_key_id=self._access_key_id,
            aws_secret_access_key=self._secret_access_key,
        )

    async def put(  # pragma: no cover - network/creds
        self, key: str, data: bytes, content_type: str
    ) -> None:
        import asyncio

        def _run() -> None:
            self._client().put_object(
                Bucket=self._bucket,
                Key=self._full_key(key),
                Body=data,
                ContentType=content_type,
            )

        await asyncio.to_thread(_run)

    async def get(self, key: str) -> bytes:  # pragma: no cover - network/creds
        import asyncio

        def _run() -> bytes:
            resp = self._client().get_object(Bucket=self._bucket, Key=self._full_key(key))
            body: bytes = resp["Body"].read()
            return body

        return await asyncio.to_thread(_run)

    async def delete(self, key: str) -> None:  # pragma: no cover - network/creds
        import asyncio

        def _run() -> None:
            self._client().delete_object(Bucket=self._bucket, Key=self._full_key(key))

        await asyncio.to_thread(_run)


_FactoryFn = Callable[[], AttachmentStore]
_override: _FactoryFn | None = None


def set_attachment_store_override(fn: _FactoryFn | None) -> None:
    """Test seam: replace the configured store with an in-memory fake.
    Production leaves this None. Never set in production code."""
    global _override
    _override = fn


def get_attachment_store(settings: Settings) -> AttachmentStore:
    """The active store. An injected override (tests) always wins;
    otherwise select by ``settings.attachment_store`` (validated
    fail-closed in ``config`` when ``s3``)."""
    if _override is not None:
        return _override()
    if settings.attachment_store == "s3":
        return S3AttachmentStore(
            endpoint_url=settings.attachment_s3_endpoint_url,
            region_name=settings.attachment_s3_region,
            bucket=settings.attachment_s3_bucket,
            aws_access_key_id=settings.attachment_s3_access_key_id,
            aws_secret_access_key=settings.attachment_s3_secret_access_key,
            prefix=settings.attachment_s3_prefix,
        )
    return PgAttachmentStore()
