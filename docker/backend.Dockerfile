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

# Local STT (faster-whisper / CTranslate2): the Telegram voice webhook
# transcribes inline in this (API) process, so the dep must live here.
# Without it LocalSTT raises and voice notes save with no transcript
# (task 44ba3f14). Explicit install, same rationale as the embedder.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /app/.venv/bin/python "faster-whisper>=1.0"

# Graph clustering (python-igraph + leidenalg): the garden /clusters
# endpoint runs Leiden in this (API) process. Without it the endpoint
# degrades to "no clusters" (task 8c0a8f08); shipping it enables
# cluster-colouring + the ADR-0035 modularity sensor. manylinux wheels,
# no model download.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /app/.venv/bin/python "python-igraph>=0.11" "leidenalg>=0.10"

# Pre-fetch the STT checkpoint so a freshly-rolled pod does not pay an HF
# download (and does not depend on egress to huggingface.co). Size/quant
# mirror the LocalSTT defaults (FLOW_STT_MODEL=small,
# FLOW_STT_COMPUTE_TYPE=int8, CPU). faster-whisper-small is ~480 MiB and
# backend-only, so it stays baked here. The dense embedder (bge-m3, ~2.3
# GiB, shared with the worker) is NOT fetched here: it arrives from the
# separate models image via `COPY --from` in the final stage, built once
# per model version and deduplicated across releases.
ENV HF_HOME=/app/.cache/huggingface
RUN /app/.venv/bin/python -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8')"

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
    HF_HOME=/app/.cache/huggingface \
    # Both checkpoints are baked (whisper from the builder stage, bge-m3
    # from the models image below), so the loaders must never reach out
    # to huggingface.co at runtime: fail fast on a missing file instead of
    # silently downloading 2.3 GiB into the request path.
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

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
# The dense embedder (bge-m3) as its own late layer from the separate,
# independently-versioned models image — built once per model version,
# deduplicated by GHCR across every release, and merged into the HF cache
# tree alongside the baked whisper checkpoint (disjoint hub subdirs, so
# they coexist). Bump the tag here AND in worker.Dockerfile when the model
# changes (see docker/models.Dockerfile).
COPY --from=ghcr.io/angleto/flow/models:bge-m3-1 /models/ /app/.cache/huggingface/

EXPOSE 8000
# --proxy-headers + --forwarded-allow-ips so uvicorn trusts the
# X-Forwarded-Proto/-Host nginx (and the Kapsule LB) set. Without it
# the app sees the TLS-terminated request as plain http and builds
# redirect Location headers with scheme=http — e.g. the MCP mount's
# /mcp -> /mcp/ 307 pointed at http://, which Claude Desktop refuses
# to follow (downgrade), breaking the connector handshake.
CMD ["uvicorn", "flow_api.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
