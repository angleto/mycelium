# Mycelium backend (FastAPI) — production image.
# Build from the Mycelium repo root:
#   docker build -f deploy/prod/dockerfiles/backend.Dockerfile \
#     -t ghcr.io/angleto/mycelium/backend:<tag> .
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
# The CLI is a workspace member too. Its manifest is here so the workspace is
# whole and ``--locked`` can check uv.lock against it; the CLI itself is not
# installed (``--no-install-package`` below), the image has no use for it.
COPY cli/pyproject.toml cli/pyproject.toml
# Heavy ML extras, installed from the lock like everything else: bge-m3
# (``local-embedder``: sentence-transformers), faster-whisper / CTranslate2
# (``stt``: voice transcription, task 44ba3f14), igraph/leidenalg
# (``clustering``: garden /clusters Leiden + ADR-0035 modularity, task
# 8c0a8f08). They used to come from an unpinned ``uv pip install`` outside the
# lock, which resolved whatever was newest on the day of the build: from 2.4.1
# that was av 19, with which faster-whisper cannot decode a single file.
# ``--locked`` also refuses a uv.lock that no longer matches the manifests.
# bge-m3 weights are NOT baked (download at runtime to HF_HOME; the bake
# exploded node disk, 30c570c).
RUN --mount=type=cache,target=/root/.cache/uv uv sync --no-dev --locked --no-install-workspace \
      --extra local-embedder --extra stt --extra clustering
# STT (faster-whisper) weights are NOT baked. Like bge-m3, the model downloads
# at runtime to HF_HOME, which prod points at the shared model-cache PVC
# (HF_HOME=/models, deploy 08174ab) so it persists across restarts/rollouts and
# stays off the ephemeral imagefs. Baking it added ~460 MB of dead weight: the
# runtime HF_HOME=/models never consulted the build-time /app/.cache copy.

# --- App layer: workspace source; thin, rebuilt every commit ---
COPY core core
COPY api api
COPY mcp mcp
COPY worker worker
COPY sdi-inbound sdi-inbound
# The maintained docs ship in the image so the MCP ``help`` tool can serve them
# as the system's own knowledge (docs are the source of truth; no separate FAQ).
COPY docs docs
# The same extras again, so this exact sync keeps them rather than pruning them.
RUN --mount=type=cache,target=/root/.cache/uv uv sync --no-dev --locked \
      --no-install-package mycelium-cli --extra local-embedder --extra stt --extra clustering
# Build-time guards. The import catches a sync that dropped an extra or a
# workspace install that broke one. The STT smoke decodes the containers the
# channels deliver (ogg/opus, webm/opus, mp4/aac, wav) through faster-whisper's
# own decoder: an import alone passed on the images that could not transcribe.
RUN /app/.venv/bin/python -c "import sentence_transformers, faster_whisper, igraph, leidenalg, mycelium_api; print('backend ml deps ok')"
COPY docker/stt_smoke.py /tmp/stt_smoke.py
RUN /app/.venv/bin/python /tmp/stt_smoke.py

# ---

FROM python:3.12-slim
# OCI image-source label: GHCR uses it to auto-link the package to the
# source repo on first push from a workflow GITHUB_TOKEN, which gates
# write permission on subsequent pushes.
LABEL org.opencontainers.image.source="https://github.com/angleto/mycelium"
LABEL org.opencontainers.image.licenses="AGPL-3.0-or-later"
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
#                          mycelium_api/static/katex/ and loaded via
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
# Split the copy so an ordinary code commit does NOT re-push/re-pull the multi-GB
# venv layer. A single `COPY /app /app` bundles the ~3 GB venv (torch) with the
# few-MB workspace source into ONE layer, so any code change rebuilds the whole
# blob and every node re-pulls it on deploy (d11a0b0f). The venv is
# code-independent (changes only when deps change); the workspace is installed
# EDITABLE (.venv `_editable_impl_*.pth` -> /app/<member>/src). Copying the venv
# as its OWN layer keeps its digest stable so the node re-pulls only the thin
# source layer below. (No /app/.cache: STT/embedder weights download to the PVC
# at runtime, not baked.)
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/pyproject.toml /app/uv.lock /app/
COPY --from=builder /app/core /app/core
COPY --from=builder /app/api /app/api
COPY --from=builder /app/mcp /app/mcp
COPY --from=builder /app/worker /app/worker
COPY --from=builder /app/sdi-inbound /app/sdi-inbound
# The maintained docs, which the BUILDER stage has copied since 2026-07-02 and
# the runtime stage never carried forward: ``/app/docs`` did not exist in a
# running pod, so the MCP ``help`` tool served an empty ``doc_topics`` and its
# own overview promised a ``help('<topic>')`` drill-down that could not work.
# The final stage is an allowlist, which is the right shape and has this cost:
# a thing not named does not ship, and this one was not named (task 677ea7c4).
COPY --from=builder /app/docs /app/docs
# The migrate Job re-asserts the prod function-execute posture after Alembic
# (`python -m mycelium_core.db_harden`, ADR-0015). Bundle the SQL it runs.
# Copied from the build context (the repo root), not the builder stage, and
# kept in this thin trailing layer so it never busts the cached venv blob.
COPY deploy/local/harden_function_acls.sql /app/deploy/local/harden_function_acls.sql

# Build identity LAST (bake into env so /api/buildinfo can return it). These args
# change EVERY build; keeping them below the COPYs lets buildkit reuse the cached
# `COPY .venv` blob (stable digest) instead of re-executing it with fresh mtimes
# -> a new 3 GB layer the node re-pulls each deploy. ARG/ENV add no filesystem
# layer. Empty in local `docker build` (the endpoint then reports "dev").
ARG MYCELIUM_VERSION=dev
ARG MYCELIUM_GIT_SHA=
ARG MYCELIUM_BUILD_AT=
ENV MYCELIUM_VERSION=${MYCELIUM_VERSION} \
    MYCELIUM_GIT_SHA=${MYCELIUM_GIT_SHA} \
    MYCELIUM_BUILD_AT=${MYCELIUM_BUILD_AT}

EXPOSE 8000
# --proxy-headers + --forwarded-allow-ips so uvicorn trusts the
# X-Forwarded-Proto/-Host nginx (and the Kapsule LB) set. Without it
# the app sees the TLS-terminated request as plain http and builds
# redirect Location headers with scheme=http — e.g. the MCP mount's
# /mcp -> /mcp/ 307 pointed at http://, which Claude Desktop refuses
# to follow (downgrade), breaking the connector handshake.
CMD ["uvicorn", "mycelium_api.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
