"""Build info router: static info baked into the image at build time.

Read from process env (set by the Dockerfile via build-arg → ENV):
``MYCELIUM_VERSION``, ``MYCELIUM_GIT_SHA``, ``MYCELIUM_BUILD_AT``. All optional —
local dev images leave them blank, and the endpoint reports "dev".

Public (no auth): the Settings card needs it post-login, but the same
endpoint is also useful to identify the running build from the login
screen without prying on tenant-scoped routes.
"""

from __future__ import annotations

import os

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/buildinfo", tags=["meta"])


class BuildInfoOut(BaseModel):
    version: str
    git_sha: str
    git_sha_short: str
    built_at: str


@router.get("", response_model=BuildInfoOut)
async def get_buildinfo() -> BuildInfoOut:
    version = os.environ.get("MYCELIUM_VERSION", "dev") or "dev"
    sha = os.environ.get("MYCELIUM_GIT_SHA", "") or ""
    built_at = os.environ.get("MYCELIUM_BUILD_AT", "") or ""
    return BuildInfoOut(
        version=version,
        git_sha=sha,
        git_sha_short=sha[:7] if sha else "",
        built_at=built_at,
    )
