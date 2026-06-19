"""FastAPI app factory. Maps domain errors to HTTP codes and renders
messages via the i18n catalog (docs/adr/0017). No business logic here
(docs/adr/0001)."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse, Response

from flow_api.routers import (
    actors,
    admin_users,
    advisory,
    agent_runs,
    agent_tokens,
    ai_assistants,
    attachments,
    auth,
    billing,
    budgets,
    buildinfo,
    calendars,
    dependencies,
    dispatch,
    email,
    embedder_provider,
    executors,
    export,
    garden,
    invoices,
    llm_provider,
    lookup,
    memory,
    memory_channels,
    mfa,
    notes,
    notifications,
    oauth,
    oauth_google,
    received_invoices,
    schedule,
    search,
    tags,
    task_relations,
    tasks,
    telegram,
    time_tracking,
    workflows,
    workspace,
)
from flow_api.routers import (
    annotations as annotations_router,
)
from flow_core.ai_providers import set_llm_override
from flow_core.config import Settings, get_settings
from flow_core.errors import (
    AuthError,
    ConflictError,
    DomainError,
    ForbiddenError,
    LockedError,
    NotFoundError,
    QuotaExceededError,
)
from flow_core.i18n import DEFAULT_LOCALE, render
from flow_core.llm_ollama import OllamaLLM
from flow_core.notification_channel import set_sender_override
from flow_core.services.mailer import build_system_mailer, set_mailer
from flow_core.services.notification_sender import build_notification_sender

_STATUS: dict[type[DomainError], int] = {
    AuthError: 401,
    ForbiddenError: 403,
    NotFoundError: 404,
    ConflictError: 409,
    LockedError: 423,
    QuotaExceededError: 429,
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


def _wire_local_llm_override(settings: Settings) -> bool:
    """Install the bundled local Ollama provider as the rank-0 env fallback
    when configured, so narration (T3) and any in-process LLM use run on the
    in-cluster model -- the API-process counterpart of the worker startup.
    Returns whether an override was installed. NOT a hosted-provider
    override: per-org OpenAI/Anthropic selection is owned by
    ``resolve_llm`` (task 8afda4e7). A later ``set_llm_override`` (e.g. CI's
    scripted fake) still wins; unset settings leave the ``LocalLLM`` stub."""
    if settings.ollama_url and settings.open_model:
        url = settings.ollama_url
        model = settings.open_model
        set_llm_override(lambda: OllamaLLM(base_url=url, model=model))
        return True
    return False


def _make_lifespan(mcp_app: Any) -> Any:
    """Parent lifespan that ALSO drives the mounted MCP sub-app's
    lifespan. FastAPI/Starlette do NOT run a mounted app's lifespan
    automatically, so the MCP ``StreamableHTTPSessionManager.run()``
    (entered by ``streamable_http_app()``'s own lifespan) would never
    start — every POST /mcp then 500s with "Task group is not
    initialized. Make sure to use run()". We enter the sub-app's
    lifespan context here so its session-manager task group is live
    for the process lifetime.

    Lifespan events fire only under a real ASGI server (uvicorn) or a
    ``with TestClient(...)`` block; the unit suite drives the app via
    bare ``httpx.ASGITransport`` which doesn't emit lifespan, so the
    MCP task group simply isn't started in tests (no MCP HTTP test
    exercises it in-process). SMTP wiring stays as before."""

    @contextlib.asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        import asyncio
        import logging

        from flow_mcp.gateway import prewarm as prewarm_mcp_gateway

        settings = get_settings()
        # Local Ollama provider as the rank-0 fallback (task T5), the
        # API-process counterpart of the worker. Cleared on shutdown below.
        _wire_local_llm_override(settings)
        if settings.smtp_configured:
            set_mailer(build_system_mailer(settings))
        # Install the concrete notification sender (telegram over an email
        # fallback that uses the mailer wired just above). Without this,
        # get_sender() returns DefaultSender and EVERY reminder dispatch
        # fails. Unconditional and safe: in a dev/OSS deploy the email half
        # falls back to LogMailer and the telegram half fails per-item
        # until the bot is configured. Must run after set_mailer.
        _sender = build_notification_sender()
        set_sender_override(lambda: _sender)
        # Drive the mounted MCP app's lifespan (starts the streamable
        # HTTP session manager's task group).
        async with mcp_app.router.lifespan_context(mcp_app):
            # Warm the embedding index for the MCP search/describe/execute
            # gateway off the request path. Without this, the first
            # ``search_tools`` call after a roll pays the model load +
            # ~140-text encode inline (10-20s worst case), long enough
            # that the MCP client reports "connection lost". Fired as a
            # background task so a missing optional dep or a slow load
            # never blocks app boot or the readiness probe.
            async def _prewarm() -> None:
                try:
                    await prewarm_mcp_gateway()
                except Exception:  # pragma: no cover - defensive
                    logging.getLogger(__name__).exception(
                        "mcp gateway prewarm failed; first search_tools will pay the cost"
                    )

            # Keep a reference so the task is not garbage-collected
            # while it runs (asyncio holds only weakrefs to tasks).
            prewarm_task = asyncio.create_task(_prewarm())
            try:
                yield
            finally:
                prewarm_task.cancel()
                # Clear the local LLM override on shutdown (hygiene the
                # worker omits): keeps set_llm_override precedence + clean
                # teardown predictable across reloads and in CI.
                set_llm_override(None)

    return _lifespan


def create_app() -> FastAPI:
    from flow_mcp.server_http import make_mcp_app

    mcp_app = make_mcp_app()
    app = FastAPI(
        title="Flow API",
        version="0.0.0",
        lifespan=_make_lifespan(mcp_app),
    )

    @app.get("/healthz", tags=["meta"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    for exc_type, status in _STATUS.items():
        app.add_exception_handler(exc_type, _make_handler(status))

    # Cross-origin SPA (production serves the SPA and the API on
    # different hosts: flow.xeno.garden vs api.flow.xeno.garden). Enabled
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
    app.include_router(annotations_router.router)
    app.include_router(workflows.router)
    app.include_router(dependencies.router)
    app.include_router(task_relations.router)
    app.include_router(calendars.router)
    app.include_router(schedule.router)
    app.include_router(executors.router)
    app.include_router(agent_runs.router)
    app.include_router(agent_tokens.router)
    app.include_router(ai_assistants.router)
    app.include_router(actors.router)
    # OAuth shim (#48). The /api-prefixed router carries the
    # authorize + token endpoints; the well-known one mounts at the
    # host root so MCP clients fetch metadata from
    # /.well-known/oauth-* (RFC 8414 / 9728). The latter is included
    # on the SAME FastAPI app — see the api_prefix("") below for
    # why nginx must proxy /.well-known/oauth-* to the backend.
    app.include_router(oauth.oauth_router)
    app.include_router(oauth.well_known_router)
    app.include_router(dispatch.router)
    app.include_router(time_tracking.router)
    app.include_router(budgets.router)
    app.include_router(advisory.router)
    app.include_router(email.router)
    app.include_router(billing.router)
    app.include_router(llm_provider.router)
    app.include_router(embedder_provider.router)
    app.include_router(memory.router)
    app.include_router(memory_channels.router)
    app.include_router(search.router)
    app.include_router(lookup.router)
    app.include_router(notes.router)
    app.include_router(garden.router)
    app.include_router(attachments.router)
    app.include_router(invoices.router)
    app.include_router(received_invoices.router)
    app.include_router(notifications.router)
    app.include_router(oauth_google.router)
    app.include_router(telegram.router)
    app.include_router(buildinfo.router)
    app.include_router(export.router)

    # MCP streamable-http transport — same process, same DB, same
    # ingress. Authenticated by Authorization: Bearer flow_at_…; the
    # middleware in flow_mcp.server_http resolves the principal via
    # the SECURITY DEFINER authenticate_agent_token (migration 0059)
    # and publishes it into a ContextVar that ``_tenant`` inside every
    # @mcp.tool short-circuits on. URL becomes /mcp at the public
    # ingress (flow.xeno.garden/mcp); behind nginx the SPA's same-origin
    # routing already covers /api/, the deploy adds a /mcp/ proxy.
    # Mount the SAME mcp_app instance whose lifespan _make_lifespan
    # drives — mounting a fresh one would leave the session manager
    # uninitialised again.
    app.mount("/mcp", mcp_app)
    return app
