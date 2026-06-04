# Flow worker — production image.
# Build from the Flow repo root:
#   docker build -f deploy/prod/dockerfiles/worker.Dockerfile \
#     -t ghcr.io/angleto/flow/worker:<tag> .
#
# Same workspace venv as the backend; entrypoint is the worker module.
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY core core
COPY api api
COPY mcp mcp
COPY worker worker
COPY sdi-inbound sdi-inbound

RUN --mount=type=cache,target=/root/.cache/uv uv sync --no-dev

# ---

FROM python:3.12-slim
LABEL org.opencontainers.image.source="https://github.com/angleto/flow"
LABEL org.opencontainers.image.licenses="AGPL-3.0-or-later"
ARG FLOW_VERSION=dev
ARG FLOW_GIT_SHA=
ARG FLOW_BUILD_AT=
ENV FLOW_VERSION=${FLOW_VERSION} \
    FLOW_GIT_SHA=${FLOW_GIT_SHA} \
    FLOW_BUILD_AT=${FLOW_BUILD_AT}
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    HF_HOME=/app/.cache/huggingface \
    # bge-m3 is baked from the models image below; the embedding backfill
    # must load it offline, never download it at runtime.
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

RUN apt-get update && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /app /app
# The dense embedder (bge-m3) for the embedding backfill, from the same
# separate, independently-versioned models image the backend uses (one
# build, two consumers). Bump the tag here AND in backend.Dockerfile when
# the model changes (see docker/models.Dockerfile).
COPY --from=ghcr.io/angleto/flow/models:bge-m3-1 /models/ /app/.cache/huggingface/

CMD ["python", "-m", "flow_worker.main"]
