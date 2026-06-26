# Mycelium sdi-inbound (FastAPI on uvicorn) — production image.
# Build from the Mycelium repo root:
#   docker build -f docker/sdi-inbound.Dockerfile \
#     -t ghcr.io/angleto/mycelium/sdi-inbound:<tag> .
#
# Always-on SOAP endpoint SdI POSTs RC/MC/NS/AT notifications to
# (docs/adr/0011). Mutual TLS terminates at the edge (Traefik
# TLSOption.clientAuth, see deploy/prod/.../ingress/sdi-tlsoption.yaml);
# this container is plain HTTP behind it. Reuses the same workspace
# venv as the backend/worker images.
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

# ---

FROM python:3.12-slim
LABEL org.opencontainers.image.source="https://github.com/angleto/mycelium"
LABEL org.opencontainers.image.licenses="AGPL-3.0-or-later"
ARG MYCELIUM_VERSION=dev
ARG MYCELIUM_GIT_SHA=
ARG MYCELIUM_BUILD_AT=
ENV MYCELIUM_VERSION=${MYCELIUM_VERSION} \
    MYCELIUM_GIT_SHA=${MYCELIUM_GIT_SHA} \
    MYCELIUM_BUILD_AT=${MYCELIUM_BUILD_AT}
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

# libpq5: psycopg runtime (cross-org SECURITY DEFINER resolver +
# tenant_session writes during ingest_receipt).
RUN apt-get update && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /app /app

EXPOSE 8000
# --proxy-headers + --forwarded-allow-ips so uvicorn trusts the
# X-Forwarded-* that Traefik sets after terminating mTLS at the edge.
CMD ["uvicorn", "mycelium_sdi_inbound.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
