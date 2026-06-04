"""Deterministic in-memory attachment store for tests (the
``attachment_store`` seam, same idea as ``_fake_ai``/``_fake_embedder``).
Repo-root module so both core/tests and api/tests can import it (the
root conftest puts this dir on sys.path). Injected via
``set_attachment_store_override`` exactly like the LLM/embedder fakes;
no boto3, no network."""

from __future__ import annotations

from collections.abc import AsyncIterator

from flow_core.attachment_store import AttachmentStreamTooLarge


class FakeAttachmentStore:
    """A dict-backed object store. Round-trips bytes; raises KeyError on
    a missing key (mirrors a real store's missing-object failure). It
    also implements ``stream_put`` so it satisfies the
    ``StreamingAttachmentStore`` capability and exercises the streaming
    upload path without boto3 / the network (the real S3 multipart impl
    is ``# pragma: no cover``)."""

    backend = "fake"

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = (data, content_type)

    async def get(self, key: str) -> bytes:
        return self.objects[key][0]

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    async def stream_put(
        self,
        key: str,
        content_type: str,
        chunks: AsyncIterator[bytes],
        *,
        max_bytes: int,
    ) -> int:
        """Consume the async chunk stream, enforcing ``max_bytes``
        incrementally exactly like the real S3 multipart impl (raise
        ``AttachmentStreamTooLarge`` mid-stream on overflow and store
        nothing), then keep the assembled bytes in the dict. Returns the
        total bytes written."""
        buf = bytearray()
        total = 0
        async for chunk in chunks:
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise AttachmentStreamTooLarge
            buf.extend(chunk)
        self.objects[key] = (bytes(buf), content_type)
        return total
