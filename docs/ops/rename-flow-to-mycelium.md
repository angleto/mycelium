# Rename Flow -> Mycelium: operational runbook

The in-repo code rename landed in one commit (`refactor: rename Flow ->
Mycelium across the monorepo`). This file tracks the **out-of-band** steps
that live outside this repository and must be done by an operator. Primary
host is **mycelium.xeno.garden** (`51.159.112.226`); **flow.xeno.garden**
stays as an accepted alias / redirect.

Order matters: do DNS + TLS + Scaleway first (no downtime), then the
coordinated cutover (images, secrets, DB) in one release window.

## 0. Already done in-repo (no action)

- Python packages, dist names, env-var prefix (`FLOW_` -> `MYCELIUM_`),
  CLI/MCP names, brand strings, SPA, docs, nvim, homebrew formula.
- DB / role defaults + CI + compose + migration grants -> `mycelium`.
- GHCR image refs in `build-images.yml` -> `ghcr.io/angleto/mycelium/*`.
- Mirror workflows -> `mycelium-nvim`, `homebrew-mycelium`.
- Backward compat: legacy agent tokens (`flow_at_`) still authenticate;
  refresh tokens unaffected; SPA migrates `flow.*` localStorage keys.

## 1. GitHub (done by Claude via `gh`)

- [ ] `gh repo rename mycelium` on `angleto/flow` (GitHub keeps a redirect
      from the old URL; existing clones keep working until re-pointed).
- [ ] Create/rename mirror repos `angleto/mycelium-nvim` and
      `angleto/homebrew-mycelium` (the mirror workflows push to these).
      Re-issue their push tokens (`NVIM_PUSH_TOKEN`, the homebrew one) if
      the old repos are deleted rather than renamed.
- [ ] Update local remote: `git remote set-url origin git@github.com:angleto/mycelium.git`.

## 2. Cloudflare DNS

- [x] `A mycelium.xeno.garden -> 51.159.112.226` (done by owner).
- [ ] If the API is served on a subdomain (the config references
      `api.mycelium.xeno.garden`): add its `A`/`CNAME`. NOTE: the CLI default
      is path-based (`https://mycelium.xeno.garden/api`), so a subdomain is
      only needed if the ingress splits hosts. Verify against flow-deploy.
- [ ] Email records — only if the transactional FROM moves to
      `@mycelium.xeno.garden` (see section 3). Keeping the FROM on
      `@flow.xeno.garden` needs no DNS change.
  - [ ] SPF: `TXT mycelium.xeno.garden` including Scaleway TEM
        (`include:_spf.tem.scaleway.com` or the value Scaleway shows).
  - [ ] DKIM: the selector `CNAME`/`TXT` records Scaleway TEM generates.
  - [ ] DMARC: `TXT _dmarc.mycelium.xeno.garden` (can mirror the flow one).

## 3. Scaleway — transactional email (TEM)

The SMTP credentials themselves do not change (same TEM project); only the
env-var NAMES moved (`FLOW_SMTP_*` -> `MYCELIUM_SMTP_*`, handled in §5).

- [ ] DECISION: keep `MYCELIUM_SMTP_FROM = <something>@flow.xeno.garden`
      (zero email work, flow domain still owned) **or** move it to
      `@mycelium.xeno.garden`.
- [ ] If moving: add `mycelium.xeno.garden` as a verified **sender domain**
      in Scaleway TEM, publish the DKIM/SPF records it returns (§2), and
      wait for verification before flipping `MYCELIUM_SMTP_FROM`.
- [ ] mTLS / SdI client certs (FatturaPA) are a separate legal identity and
      do **not** change. The env-var names did (`MYCELIUM_SDI_*`); values stay.

## 4. TLS certificates

- [ ] Issue a cert for `mycelium.xeno.garden` (and `api.` if used).
      cert-manager/Let's Encrypt: add the host to the Ingress `tls:` block
      and the ACME solver. Scaleway LB cert: add the SAN.
- [ ] Keep the `flow.xeno.garden` cert valid as long as the alias/redirect
      is served.

## 5. flow-deploy repo (Kubernetes manifests)

This repo is external; mirror these renames there.

- [ ] Image refs: `ghcr.io/angleto/flow/*` -> `ghcr.io/angleto/mycelium/*`
      (backend, worker, frontend, sdi-inbound).
- [ ] Secrets/ConfigMaps: rename every `FLOW_*` env key to `MYCELIUM_*`
      (VALUES unchanged). Includes `MYCELIUM_DATABASE_URL(_SYNC)`,
      `MYCELIUM_DB_APP_PASSWORD`, `MYCELIUM_JWT_SECRET`, `MYCELIUM_SECRET_KEY`,
      `MYCELIUM_SMTP_*`, `MYCELIUM_SDI_*`, `MYCELIUM_TELEGRAM_*`,
      `MYCELIUM_CORS_ORIGINS`, `MYCELIUM_PUBLIC_BASE_URL`, etc.
  - [ ] `MYCELIUM_CORS_ORIGINS` must list BOTH `https://mycelium.xeno.garden`
        and `https://flow.xeno.garden` while the alias is live.
  - [ ] `MYCELIUM_PUBLIC_BASE_URL` (OAuth/MCP) -> `https://mycelium.xeno.garden`.
- [ ] Ingress: add the `mycelium.xeno.garden` host; serve a 308 redirect
      `flow.xeno.garden -> mycelium.xeno.garden` (or co-serve both).
- [ ] DB connection string secret -> `mycelium` / `mycelium_app` (after §6).
- [x] k8s Service + namespace renamed in mycelium-deploy: `flow-backend` ->
      `mycelium-backend`, namespace `flow-production` -> `mycelium-production`;
      `docker/nginx.conf` updated in lockstep
      (`mycelium-backend.mycelium-production`). A namespace CANNOT be renamed
      in place: apply the renamed manifests to create `mycelium-production`,
      migrate the model-cache PVC + re-sync ESO, cut the ingress over, then
      delete the old `flow-production`.
- [ ] Logger namespaces moved `flow.*` -> `mycelium.*`. If mycelium-deploy's
      log config sets per-logger levels keyed on `flow.*`, move them to
      `mycelium.*` (otherwise those loggers fall back to the root level).

### Deliberately kept as `flow` (external coupling, not user-facing)

Renaming each would break a live external resource for no user benefit:
- Postgres `NOTIFY`/`LISTEN` channel `flow.event` (DB trigger <-> event_bus):
  needs a trigger-recreate migration.
- Legacy agent-token prefix `flow_at_` (still accepted on read).
- Scaleway SM secret names (`name:FLOW_*`) + the `/flow/prod` SM path: the k8s
  side is `MYCELIUM_*` and ESO maps Scaleway `FLOW_*` -> k8s `MYCELIUM_*`, so
  NO Scaleway SM rename is needed.
- Scaleway Object Storage bucket `flow-prod-attachments` (holds the data).
- Email/TEM on the `flow.xeno.garden` label: `MYCELIUM_SMTP_FROM` keeps
  `no-reply@flow.xeno.garden` (TEM-verified); SPF/DKIM/DMARC/MX stay on the
  `flow` label, only the display name became "Mycelium". The app A record is
  on `mycelium`; the DEPLOYMENT-GUIDE/CUTOVER DNS sections predate this split,
  so read them with app=`mycelium`, email=`flow` in mind.
- Scaleway node pool `flow` (`pool: flow` + taint `dedicated=flow`): renaming
  needs a node-pool recreate.
- The recovery ConfigMap path `/var/lib/flow/recovery` (historical migration).
- The org/project literally named `flow` in the DB (that is data).

NOTE: the SdI RicezioneNotifiche host moved to `sdi.mycelium.xeno.garden`
(re-declared at the AdE portal), and the manifests were updated accordingly.

## 6. Production database (maintenance window)

Run `deploy/rename-flow-to-mycelium.sql` as a superuser, app stopped,
connected to another DB (e.g. `postgres`):

```
psql "postgresql://<superuser>@<host>:5432/postgres" \
     -v ON_ERROR_STOP=1 -f deploy/rename-flow-to-mycelium.sql
```

`ALTER DATABASE/ROLE RENAME` preserves data, passwords, grants, ownership
and RLS. Then point the app at `.../mycelium` with `mycelium`/`mycelium_app`
(the secret VALUES are unchanged; only names moved in §5). No Alembic
migration runs as part of this.

## 7. Client reconfiguration

- [ ] MCP client (Claude Desktop/CLI): the server identifier changed
      `flow` -> `mycelium`. Re-add the connector; its tools move from
      `...Flow...` to `...Mycelium...`. Existing agent tokens (`flow_at_…`)
      keep working (legacy-accepted).
- [ ] CLI: binary is now `mycelium` (was `flow`). Config dir moved
      `~/.config/flow` -> `~/.config/mycelium`; copy `credentials.toml`
      across or re-run `mycelium auth login`.

## 8. Local working copy

- [ ] Rename the checkout dir `…/WORK/flow` -> `…/WORK/mycelium` (optional;
      cosmetic). Update any tmux/editor/session paths.

## 9. Post-cutover verification

- [ ] CI green on the renamed repo (full suite against a fresh `mycelium`
      Postgres).
- [ ] `https://mycelium.xeno.garden` serves the SPA; `flow.xeno.garden`
      redirects.
- [ ] Login works (existing sessions may need one re-login if refresh
      cookies were domain-scoped to flow).
- [ ] A transactional email sends and passes SPF/DKIM.
- [ ] The MCP connector lists tools and a `mycelium_cap_` capability flow
      round-trips.
