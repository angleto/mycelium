"""Deterministic in-memory attachment store for tests (the
``attachment_store`` seam, same idea as ``_fake_ai``/``_fake_embedder``).
Repo-root module so both core/tests and api/tests can import it (the
root conftest puts this dir on sys.path). Injected via
``set_attachment_store_override`` exactly like the LLM/embedder fakes;
no boto3, no network."""

from __future__ import annotations


class FakeAttachmentStore:
    """A dict-backed object store. Round-trips bytes; raises KeyError on
    a missing key (mirrors a real store's missing-object failure)."""

    backend = "fake"

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = (data, content_type)

    async def get(self, key: str) -> bytes:
        return self.objects[key][0]

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)
