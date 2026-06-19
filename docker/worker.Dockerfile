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
# --- Dependency layer: keyed on locks/manifests only, NOT app source ---
# Install all third-party deps + the heavy ML extras BEFORE copying the app
# code, so an ordinary commit does not rebuild / re-push / re-pull the
# multi-GB torch layer on every deploy. (d11a0b0f: the huge worker image on
# slow GHCR egress serialised the node's pull queue for ~hours.) This layer's
# cache key is pyproject + uv.lock + the member manifests, which change rarely.
COPY pyproject.toml uv.lock ./
COPY core/pyproject.toml core/pyproject.toml
COPY api/pyproject.toml api/pyproject.toml
COPY mcp/pyproject.toml mcp/pyproject.toml
COPY worker/pyproject.toml worker/pyproject.toml
COPY sdi-inbound/pyproject.toml sdi-inbound/pyproject.toml
RUN --mount=type=cache,target=/root/.cache/uv uv sync --no-dev --no-install-workspace
# Heavy ML extras the worker's jobs need: bge-m3 (sentence-transformers) for
# the embedding backfill (embedding_migration), igraph/leidenalg for the
# garden loop's Leiden + auto-promote (gated by garden_loop_enabled). bge-m3
# weights are NOT baked (download at runtime to HF_HOME; the bake exploded
# node disk, 30c570c).
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /app/.venv/bin/python \
      "sentence-transformers>=3" "python-igraph>=0.11" "leidenalg>=0.10"

# --- App layer: workspace source; thin, rebuilt every commit ---
COPY core core
COPY api api
COPY mcp mcp
COPY worker worker
COPY sdi-inbound sdi-inbound
# ``--inexact`` so installing the workspace members does not prune the ML
# extras added above (they are optional, not in the default lock resolution).
RUN --mount=type=cache,target=/root/.cache/uv uv sync --no-dev --inexact
# Build-time guard: fail the build (not prod) if the final sync ever pruned
# the ML extras or the workspace install broke their import.
RUN /app/.venv/bin/python -c "import sentence_transformers, igraph, leidenalg, flow_worker; print('worker ml deps ok')"

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
