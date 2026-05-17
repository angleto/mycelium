"""FastAPI app factory. Maps domain errors to HTTP codes and renders
messages via the i18n catalog (docs/adr/0017). No business logic here
(docs/adr/0001)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request
from starlette.responses import JSONResponse, Response

from flow_api.routers import (
    advisory,
    auth,
    billing,
    budgets,
    calendars,
    dependencies,
    email,
    events,
    org,
    schedule,
    tags,
    tasks,
    time_tracking,
    workflows,
)
from flow_core.errors import (
    AuthError,
    ConflictError,
    DomainError,
    ForbiddenError,
    NotFoundError,
)
from flow_core.i18n import DEFAULT_LOCALE, render

_STATUS: dict[type[DomainError], int] = {
    AuthError: 401,
    ForbiddenError: 403,
    NotFoundError: 404,
    ConflictError: 409,
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


def create_app() -> FastAPI:
    app = FastAPI(title="Flow API", version="0.0.0")

    @app.get("/healthz", tags=["meta"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    for exc_type, status in _STATUS.items():
        app.add_exception_handler(exc_type, _make_handler(status))

    app.include_router(auth.router)
    app.include_router(org.router)
    app.include_router(tags.router)
    app.include_router(tasks.router)
    app.include_router(workflows.router)
    app.include_router(dependencies.router)
    app.include_router(calendars.router)
    app.include_router(events.router)
    app.include_router(schedule.router)
    app.include_router(time_tracking.router)
    app.include_router(budgets.router)
    app.include_router(advisory.router)
    app.include_router(email.router)
    app.include_router(billing.router)
    return app
