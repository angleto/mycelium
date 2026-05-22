# Flow backend (FastAPI) — production image.
# Build from the Flow repo root:
#   docker build -f deploy/prod/dockerfiles/backend.Dockerfile \
#     -t ghcr.io/angleto/flow/backend:<tag> .
#
# Also used by the Alembic migrate Job (alembic + core/migrations are
# inside; core/alembic.ini -> script_location core/migrations,
# prepend_sys_path core/src, so `alembic -c core/alembic.ini upgrade
# head` works from /app).
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app
# Whole uv workspace (members: core, api, mcp, worker, sdi-inbound).
COPY pyproject.toml uv.lock ./
COPY core core
COPY api api
COPY mcp mcp
COPY worker worker
COPY sdi-inbound sdi-inbound

RUN --mount=type=cache,target=/root/.cache/uv uv sync --no-dev
# Local embedder (intfloat/multilingual-e5-small, 384-dim = FLOW
# embed_dim). Explicit install is deterministic regardless of how the
# optional extra propagates across the workspace.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /app/.venv/bin/python "sentence-transformers>=3"

# Pre-fetch the embedding checkpoint so a freshly-rolled pod does not
# pay an HF download (and does not depend on egress to huggingface.co).
ENV HF_HOME=/app/.cache/huggingface
RUN /app/.venv/bin/python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('intfloat/multilingual-e5-small')"

# ---

FROM python:3.12-slim
# OCI image-source label: GHCR uses it to auto-link the package to the
# source repo on first push from a workflow GITHUB_TOKEN, which gates
# write permission on subsequent pushes.
LABEL org.opencontainers.image.source="https://github.com/angleto/flow"
LABEL org.opencontainers.image.licenses="AGPL-3.0-or-later"
# Build-time identity (bake into env so /api/buildinfo can return it).
# Passed via --build-arg from the build-images workflow; empty in local
# `docker build` (the endpoint then reports "dev").
ARG FLOW_VERSION=dev
ARG FLOW_GIT_SHA=
ARG FLOW_BUILD_AT=
ENV FLOW_VERSION=${FLOW_VERSION} \
    FLOW_GIT_SHA=${FLOW_GIT_SHA} \
    FLOW_BUILD_AT=${FLOW_BUILD_AT}
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    HF_HOME=/app/.cache/huggingface

# libpq5: psycopg runtime (Alembic sync path).
RUN apt-get update && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /app /app

EXPOSE 8000
# --proxy-headers + --forwarded-allow-ips so uvicorn trusts the
# X-Forwarded-Proto/-Host nginx (and the Kapsule LB) set. Without it
# the app sees the TLS-terminated request as plain http and builds
# redirect Location headers with scheme=http — e.g. the MCP mount's
# /mcp -> /mcp/ 307 pointed at http://, which Claude Desktop refuses
# to follow (downgrade), breaking the connector handshake.
CMD ["uvicorn", "flow_api.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
