"""FastAPI app factory. Maps domain errors to HTTP codes and renders
messages via the i18n catalog (docs/adr/0017). No business logic here
(docs/adr/0001)."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse, Response

from flow_api.routers import (
    admin_users,
    advisory,
    agent_runs,
    attachments,
    auth,
    billing,
    budgets,
    buildinfo,
    calendars,
    dependencies,
    dispatch,
    email,
    events,
    executors,
    invoices,
    memory,
    memory_channels,
    mfa,
    notes,
    notifications,
    oauth_google,
    schedule,
    tags,
    tasks,
    telegram,
    time_tracking,
    workflows,
    workspace,
)
from flow_core.config import get_settings
from flow_core.errors import (
    AuthError,
    ConflictError,
    DomainError,
    ForbiddenError,
    LockedError,
    NotFoundError,
)
from flow_core.i18n import DEFAULT_LOCALE, render
from flow_core.services.mailer import build_system_mailer, set_mailer

_STATUS: dict[type[DomainError], int] = {
    AuthError: 401,
    ForbiddenError: 403,
    NotFoundError: 404,
    ConflictError: 409,
    LockedError: 423,
    DomainError: 400,
}


def _locale(request: Request) -> str:
    raw = request.headers.get("accept-language", DEFAULT_LOCALE)
    return raw.split(",")[0].split("-")[0].strip().lower() or DEFAULT_LOCALE


def _make_handler(
    status: int,
) -> Callable[[Request, Exception], Awaitable[Response]]:
    async def handler(request: Request, exc: Exception) -> Response:
        if isinstance(exc, DomainError):
            detail = render(exc.code, _locale(request), **exc.params)
            body = {"code": exc.code.value, "detail": detail}
        else:
            body = {"code": "internal", "detail": "internal error"}
        return JSONResponse(status_code=status, content=body)

    return handler


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Process-global wiring for the *real* ASGI server.

    Lifespan events fire only under an ASGI server (uvicorn) or a
    ``with TestClient(...)`` block; the suite drives the app via
    ``httpx.ASGITransport`` / bare ``TestClient(...)``, neither of
    which emits lifespan, so this never runs in unit tests and a
    test-injected fake mailer is never clobbered.

    Defensive belt-and-braces even if it did run: only swap the
    process-global when SMTP is actually configured. Unconfigured
    (dev/OSS/tests) the module default is already ``LogMailer`` and we
    leave the global untouched, so an explicit ``set_mailer(fake)``
    always wins regardless of lifespan ordering."""
    settings = get_settings()
    if settings.smtp_configured:
        set_mailer(build_system_mailer(settings))
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Flow API", version="0.0.0", lifespan=_lifespan)

    @app.get("/healthz", tags=["meta"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    for exc_type, status in _STATUS.items():
        app.add_exception_handler(exc_type, _make_handler(status))

    # Cross-origin SPA (production serves the SPA and the API on
    # different hosts: flow.leto.blue vs api.flow.leto.blue). Enabled
    # only when origins are configured (FLOW_CORS_ORIGINS).
    origins = get_settings().cors_origin_list
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.include_router(auth.router)
    app.include_router(admin_users.router)
    app.include_router(mfa.router)
    app.include_router(workspace.router)
    app.include_router(tags.router)
    app.include_router(tasks.router)
    app.include_router(workflows.router)
    app.include_router(dependencies.router)
    app.include_router(calendars.router)
    app.include_router(events.router)
    app.include_router(schedule.router)
    app.include_router(executors.router)
    app.include_router(agent_runs.router)
    app.include_router(dispatch.router)
    app.include_router(time_tracking.router)
    app.include_router(budgets.router)
    app.include_router(advisory.router)
    app.include_router(email.router)
    app.include_router(billing.router)
    app.include_router(memory.router)
    app.include_router(memory_channels.router)
    app.include_router(notes.router)
    app.include_router(attachments.router)
    app.include_router(invoices.router)
    app.include_router(notifications.router)
    app.include_router(oauth_google.router)
    app.include_router(telegram.router)
    app.include_router(buildinfo.router)
    return app
