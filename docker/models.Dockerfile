# Flow model image — the heavy, rarely-changing ML checkpoint, split out
# of the app images.
# =====================================================================
# Why a separate image: BAAI/bge-m3 (the local dense embedder, migration
# 0028 / v2.0.83) is ~2.3 GiB. Baking it into backend.Dockerfile /
# worker.Dockerfile would re-fetch it from huggingface.co on every app
# build and re-push it on every release. Here it is built ONCE per model
# version and consumed by the app images via `COPY --from` as a single
# content-addressed layer that GHCR deduplicates across every backend /
# worker release. The app images load it offline (HF_HUB_OFFLINE=1), so a
# rolled pod never depends on egress to huggingface.co and can never OOM
# mid-download (the v2.0.83 incident: the Dockerfile still pre-fetched the
# old e5-small, so bge-m3 was pulled at runtime under a 1.5 GiB limit).
#
# Build + push ONLY when the model changes — bump the tag and update the
# `COPY --from=ghcr.io/angleto/flow/models:bge-m3-N` line in backend.Dockerfile
# and worker.Dockerfile. See .github/workflows/build-model-image.yml.
#   docker build -f docker/models.Dockerfile \
#     -t ghcr.io/angleto/flow/models:bge-m3-1 .
#
# Holds bge-m3 in the HuggingFace hub cache layout
# (``$HF_HOME/hub/models--BAAI--bge-m3/...``) that the runtime loader
# (sentence-transformers >=3) expects: the same library writes the cache
# here and reads it in the app, so the layout can never drift.
FROM python:3.12-slim AS fetch

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/models

RUN pip install --no-cache-dir "sentence-transformers>=3"
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-m3')"

# Minimal final image: just the cache tree on `scratch`, so `COPY --from`
# pulls only the checkpoint, none of the Python build toolchain.
FROM scratch
# OCI image-source label so GHCR auto-links this package to the repo on
# first push from the workflow GITHUB_TOKEN (gates write on later pushes),
# same as the app images.
LABEL org.opencontainers.image.source="https://github.com/angleto/flow"
LABEL org.opencontainers.image.licenses="AGPL-3.0-or-later"
COPY --from=fetch /models /models
