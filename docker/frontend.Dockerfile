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
RUN pnpm build

FROM nginx:1.27-alpine
LABEL org.opencontainers.image.source="https://github.com/angleto/mycelium"
LABEL org.opencontainers.image.licenses="AGPL-3.0-or-later"
# Accept build-args so the workflow can pass identical args to all three
# images; values are not embedded in the static bundle (the SPA reads
# the canonical info from the backend /api/buildinfo endpoint).
ARG MYCELIUM_VERSION=dev
ARG MYCELIUM_GIT_SHA=
ARG MYCELIUM_BUILD_AT=
# Non-root: listen on 8080 (see nginx.conf), matches the Service
# targetPort and the pod's containerPort.
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /web/dist /usr/share/nginx/html
EXPOSE 8080
