# Mycelium frontend — production image (Vite SPA on nginx).
# Build from the Mycelium repo root:
#   docker build -f docker/frontend.Dockerfile \
#     -t ghcr.io/angleto/mycelium/frontend:<tag> .
#
# nginx serves the static bundle and reverse-proxies /api → the backend
# Service, stripping /api exactly like the Vite dev proxy. One origin,
# no CORS.
FROM node:22-alpine AS build
WORKDIR /web
RUN corepack enable
# .npmrc carries the supply-chain min-release-age policy; without it
# pnpm v11 applies a default 24h cutoff and a same-day dependency
# upgrade (e.g. a TipTap patch) silently fails the install.
COPY web/package.json web/pnpm-lock.yaml web/.npmrc ./
RUN pnpm install --frozen-lockfile
COPY web/ ./
# Bundle identity. The backend takes these same three arguments in its
# RUNTIME stage, because it reads them from the environment when it
# answers /api/buildinfo. The SPA cannot: its identity is baked into the
# bundle and into the /version.json the running app polls to notice a
# deploy, so the values must reach the stage that runs `pnpm build`. They
# were declared only in the nginx stage below, so the bundle never saw
# them and 2.3.9 shipped `{"buildId":"dev-<clock>"}` to production while
# the workflow was passing them correctly. Declared after the dependency
# layers, so a new commit invalidates the bundle layer only.
ARG MYCELIUM_VERSION=
ARG MYCELIUM_GIT_SHA=
ARG MYCELIUM_BUILD_AT=
ENV MYCELIUM_VERSION=${MYCELIUM_VERSION} \
    MYCELIUM_GIT_SHA=${MYCELIUM_GIT_SHA} \
    MYCELIUM_BUILD_AT=${MYCELIUM_BUILD_AT}
# The assert is what stops this from recurring: an image whose bundle
# cannot name its release fails here instead of reaching production with
# a placeholder. A developer's plain `pnpm build` keeps the fallback.
RUN pnpm build && node scripts/assert-build-identity.mjs dist/version.json

FROM nginx:1.27-alpine
LABEL org.opencontainers.image.source="https://github.com/angleto/mycelium"
LABEL org.opencontainers.image.licenses="AGPL-3.0-or-later"
# Re-declared: ARG does not cross stages. Here they name the IMAGE, so
# `docker inspect` answers the same question the bundle does.
ARG MYCELIUM_VERSION=dev
ARG MYCELIUM_GIT_SHA=
ARG MYCELIUM_BUILD_AT=
LABEL org.opencontainers.image.version="${MYCELIUM_VERSION}"
LABEL org.opencontainers.image.revision="${MYCELIUM_GIT_SHA}"
LABEL org.opencontainers.image.created="${MYCELIUM_BUILD_AT}"
# Non-root: listen on 8080 (see nginx.conf), matches the Service
# targetPort and the pod's containerPort.
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /web/dist /usr/share/nginx/html
EXPOSE 8080
