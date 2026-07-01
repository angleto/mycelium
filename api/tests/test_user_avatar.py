"""Per-user avatar: upload (PNG/JPEG, decode-validated) + fetch + the
``has_avatar`` flag on /auth/me. The bytes live on the global users table;
the write goes through the no-tenant admin session like the other /auth/me
flows.
"""

from __future__ import annotations

import io
import uuid

from httpx import ASGITransport, AsyncClient
from PIL import Image

from mycelium_api.main import app


def _png() -> bytes:
    # A real RGBA PNG, like the browser canvas.toBlob('image/png') the avatar
    # generator produces; reportlab ImageReader decodes it (image_is_decodable).
    buf = io.BytesIO()
    Image.new("RGBA", (16, 16), (74, 107, 62, 255)).save(buf, "PNG")
    return buf.getvalue()


_PNG = _png()


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_avatar_upload_fetch_and_has_avatar_flag() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        tok = (
            await c.post("/auth/signup", json={"email": _email(), "password": "pw-strong-123"})
        ).json()["token"]
        h = _bearer(tok)

        # Fresh account: no avatar yet.
        me = (await c.get("/auth/me", headers=h)).json()
        assert me["has_avatar"] is False
        assert (await c.get("/auth/me/avatar", headers=h)).status_code == 404

        # Upload a generated avatar + its styling identity.
        up = await c.post(
            "/auth/me/avatar",
            headers=h,
            files={"file": ("avatar.png", _PNG, "image/png")},
            data={"seed": "MYC-abc123", "bg": "#4a6b3e", "net": "#ffffff"},
        )
        assert up.status_code == 200
        assert up.json()["has_avatar"] is True

        # Fetch the bytes back.
        got = await c.get("/auth/me/avatar", headers=h)
        assert got.status_code == 200
        assert got.headers["content-type"] == "image/png"
        assert got.content == _PNG
        assert got.headers.get("cache-control") == "no-store"

        # /auth/me now reflects it AND returns the styling identity, so the
        # avatar card can show the SAVED avatar and an issuer logo can reuse
        # the exact same mycelium (seed + colours).
        me2 = (await c.get("/auth/me", headers=h)).json()
        assert me2["has_avatar"] is True
        assert me2["avatar_seed"] == "MYC-abc123"
        assert me2["avatar_bg"] == "#4a6b3e"
        assert me2["avatar_net"] == "#ffffff"


async def test_avatar_rejects_non_image() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        tok = (
            await c.post("/auth/signup", json={"email": _email(), "password": "pw-strong-123"})
        ).json()["token"]
        h = _bearer(tok)
        # Declared PNG but not a decodable raster -> 400, nothing stored.
        bad = await c.post(
            "/auth/me/avatar",
            headers=h,
            files={"file": ("x.png", b"definitely not a png", "image/png")},
        )
        assert bad.status_code == 400
        assert (await c.get("/auth/me", headers=h)).json()["has_avatar"] is False


async def test_avatar_requires_auth() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        assert (await c.get("/auth/me/avatar")).status_code == 401
