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
# Local embedder (BAAI/bge-m3, 1024-dim = FLOW embed_dim): the worker runs
# the embedding backfill (embedding_migration.run_forever, always on) that
# re-embeds rows written keyword-only (model_id='none'). Without this extra
# get_embedder() raises and the backfill is a silent no-op, so the whole
# dense tier stays empty (task WS-A / 0a96ba96). Explicit install mirrors
# backend.Dockerfile; the lean `uv sync` omits the optional extra.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /app/.venv/bin/python "sentence-transformers>=3"
# Graph clustering (python-igraph + leidenalg): the garden loop's Leiden +
# auto-promote sweep (garden.run_forever, gated by garden_loop_enabled)
# runs in THIS process. Without it the sweep degrades to "no clusters".
# manylinux wheels, no model download (task 8c0a8f08 / 44b4c212).
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /app/.venv/bin/python "python-igraph>=0.11" "leidenalg>=0.10"
# NB: the bge-m3 weights are deliberately NOT baked here (the bake exploded
# node disk, 30c570c); they download at runtime to HF_HOME. The resulting
# image bloat + GHCR egress is tracked separately (task d11a0b0f → shared
# ML base image for backend+worker).

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
    HF_HOME=/app/.cache/huggingface

# Runtime shared libs:
#  - libpq5:    psycopg runtime.
#  - libgomp1:  OpenMP runtime for torch CPU (sentence-transformers /
#               bge-m3). slim Debian lacks libgomp.so.1; without it the
#               first embed raises at import/encode time.
RUN apt-get update && apt-get install -y --no-install-recommends libpq5 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /app /app

CMD ["python", "-m", "flow_worker.main"]
