# Mycelium worker — production image.
# Build from the Mycelium repo root:
#   docker build -f deploy/prod/dockerfiles/worker.Dockerfile \
#     -t ghcr.io/angleto/mycelium/worker:<tag> .
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
RUN /app/.venv/bin/python -c "import sentence_transformers, igraph, leidenalg, mycelium_worker; print('worker ml deps ok')"

# ---

FROM python:3.12-slim
LABEL org.opencontainers.image.source="https://github.com/angleto/mycelium"
LABEL org.opencontainers.image.licenses="AGPL-3.0-or-later"
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
# Split the copy so an ordinary code commit does NOT re-push/re-pull the multi-GB
# venv layer. A single `COPY /app /app` bundles the ~3 GB venv (torch) with the
# few-MB workspace source into ONE layer, so any code change rebuilds the whole
# blob and every node re-pulls it on deploy (d11a0b0f: that serialised the arm
# node's pull queue, ~hours). The workspace is installed EDITABLE (.venv carries
# `_editable_impl_*.pth` -> /app/<member>/src), so the venv is byte-stable across
# code-only changes; copying it as its OWN layer (with the volatile version stamp
# moved BELOW, so this COPY stays cache-hit and keeps a stable digest) means the
# node re-pulls only the thin source layer.
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/pyproject.toml /app/uv.lock /app/
COPY --from=builder /app/core /app/core
COPY --from=builder /app/api /app/api
COPY --from=builder /app/mcp /app/mcp
COPY --from=builder /app/worker /app/worker
COPY --from=builder /app/sdi-inbound /app/sdi-inbound

# Build identity LAST. These args change EVERY build, so every layer after them
# is cache-busted; keeping them below the COPYs lets buildkit reuse the cached
# `COPY .venv` blob (stable digest) instead of re-executing it and stamping fresh
# mtimes -> a new 3 GB layer the node would re-pull each deploy. ARG/ENV create
# no filesystem layer, so placing them here costs nothing. Surfaced by /api/buildinfo.
ARG MYCELIUM_VERSION=dev
ARG MYCELIUM_GIT_SHA=
ARG MYCELIUM_BUILD_AT=
ENV MYCELIUM_VERSION=${MYCELIUM_VERSION} \
    MYCELIUM_GIT_SHA=${MYCELIUM_GIT_SHA} \
    MYCELIUM_BUILD_AT=${MYCELIUM_BUILD_AT}

CMD ["python", "-m", "mycelium_worker.main"]
