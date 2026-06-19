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
# --- Dependency layer: keyed on locks/manifests only, NOT app source ---
# Install all third-party deps + the heavy ML extras BEFORE copying the app
# code, so an ordinary commit does not rebuild / re-push / re-pull the
# multi-GB torch layer on every deploy (d11a0b0f). Cache key = pyproject +
# uv.lock + the member manifests, which change rarely.
COPY pyproject.toml uv.lock ./
COPY core/pyproject.toml core/pyproject.toml
COPY api/pyproject.toml api/pyproject.toml
COPY mcp/pyproject.toml mcp/pyproject.toml
COPY worker/pyproject.toml worker/pyproject.toml
COPY sdi-inbound/pyproject.toml sdi-inbound/pyproject.toml
RUN --mount=type=cache,target=/root/.cache/uv uv sync --no-dev --no-install-workspace
# Heavy ML extras: bge-m3 (sentence-transformers, embeddings); faster-whisper
# / CTranslate2 (Telegram voice STT, task 44ba3f14); igraph/leidenalg (garden
# /clusters Leiden + ADR-0035 modularity, task 8c0a8f08). bge-m3 weights are
# NOT baked (download at runtime to HF_HOME; the bake exploded node disk,
# 30c570c — a prior line wrongly prefetched e5-small/384 vs runtime bge-m3/1024).
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /app/.venv/bin/python \
      "sentence-transformers>=3" "faster-whisper>=1.0" "python-igraph>=0.11" "leidenalg>=0.10"
# Pre-fetch the STT checkpoint (small/int8/CPU) so a fresh pod does not pay an
# HF download. Code-independent, so it stays in the cached prefix.
ENV HF_HOME=/app/.cache/huggingface
RUN /app/.venv/bin/python -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8')"

# --- App layer: workspace source; thin, rebuilt every commit ---
COPY core core
COPY api api
COPY mcp mcp
COPY worker worker
COPY sdi-inbound sdi-inbound
# ``--inexact`` so installing the workspace members does not prune the ML
# extras added above (optional, not in the default lock resolution).
RUN --mount=type=cache,target=/root/.cache/uv uv sync --no-dev --inexact

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

# Runtime shared libs:
#  - libpq5:               psycopg runtime (Alembic sync path).
#  - libpango / libharfbuzz / fontconfig + fonts-dejavu-core:
#                          WeasyPrint (/export/pdf). v60+ no longer
#                          needs GTK/Cairo, only Pango + HarfBuzz +
#                          a font path that resolves generic families.
#                          KaTeX fonts are bundled in
#                          flow_api/static/katex/ and loaded via
#                          file:// @font-face, but body text needs at
#                          least one serif/sans/mono installed so
#                          fontconfig has something to map to.
#  - libgomp1:             OpenMP runtime for CTranslate2 (faster-whisper
#                          STT). The CT2 wheel bundles most libs but not
#                          libgomp.so.1, which slim Debian lacks.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b \
        fontconfig fonts-dejavu-core \
        libgomp1 \
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
