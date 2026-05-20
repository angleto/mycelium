# Flow frontend — production image (Vite SPA on nginx).
# Build from the Flow repo root:
#   docker build -f docker/frontend.Dockerfile \
#     -t ghcr.io/angleto/flow/frontend:<tag> .
#
# nginx serves the static bundle and reverse-proxies /api → the backend
# Service, stripping /api exactly like the Vite dev proxy. One origin,
# no CORS.
FROM node:22-alpine AS build
WORKDIR /web
RUN corepack enable
COPY web/package.json web/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY web/ ./
RUN pnpm build

FROM nginx:1.27-alpine
LABEL org.opencontainers.image.source="https://github.com/angleto/flow"
LABEL org.opencontainers.image.licenses="AGPL-3.0-or-later"
# Accept build-args so the workflow can pass identical args to all three
# images; values are not embedded in the static bundle (the SPA reads
# the canonical info from the backend /api/buildinfo endpoint).
ARG FLOW_VERSION=dev
ARG FLOW_GIT_SHA=
ARG FLOW_BUILD_AT=
# Non-root: listen on 8080 (see nginx.conf), matches the Service
# targetPort and the pod's containerPort.
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /web/dist /usr/share/nginx/html
EXPOSE 8080
