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
- Tokens: agent tokens must be RE-MINTED (the legacy `flow_at_` prefix is no
  longer accepted; new ones use `mycelium_at_`). Refresh tokens are unaffected
  (hash lookup). The SPA migrates `flow.*` localStorage keys on first load.

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
- [ ] Email records — REQUIRED. `MYCELIUM_SMTP_FROM` is now
      `no-reply@mycelium.xeno.garden` (no `flow` in the brand), so the TEM
      records move to the `mycelium` label and MUST exist before mail sends:
  - [ ] SPF: `TXT mycelium.xeno.garden` including Scaleway TEM
        (`include:_spf.tem.scaleway.com` or the value Scaleway shows).
  - [ ] DKIM: the selector `CNAME`/`TXT` records Scaleway TEM generates
        (`<project-id>._domainkey.mycelium`).
  - [ ] DMARC: `TXT _dmarc.mycelium.xeno.garden`.
  - [ ] MX: the TEM bounce/verification record on the `mycelium` label.

## 3. Scaleway — transactional email (TEM)

The SMTP credentials themselves do not change (same TEM project); only the
env-var NAMES moved (`FLOW_SMTP_*` -> `MYCELIUM_SMTP_*`, handled in §5) and
the FROM domain moved to `mycelium.xeno.garden`.

- [ ] PREREQUISITE (manifest already set `MYCELIUM_SMTP_FROM` to
      `no-reply@mycelium.xeno.garden`): add `mycelium.xeno.garden` as a
      verified **sender domain** in Scaleway TEM, publish the DKIM/SPF/DMARC
      records it returns on the `mycelium` label (§2), and wait for
      verification. Until then, transactional mail from the new domain will
      fail SPF/DKIM. The old TEM domain `flow.xeno.garden` can be removed
      once the cutover is verified.
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

### Data-preserving external migrations (do BEFORE applying the manifests)

Nothing `flow` remains in either repo (the agent-token legacy prefix, the
`flow.event` channel via migration 0058, the recovery path, the bucket, the
node pool and the SM names were all renamed). The manifests now reference the
`mycelium` names, so the underlying external resources must be migrated first,
**preserving data**:

- [ ] **Scaleway Object Storage bucket** `flow-prod-attachments` ->
      `mycelium-prod-attachments`. Buckets cannot be renamed in place: create
      the new bucket, copy every object (keys are bucket-relative and stay the
      same, so DB `storage_key`s keep resolving), verify, then delete the old.
      e.g. `rclone sync scw:flow-prod-attachments scw:mycelium-prod-attachments`.
- [ ] **Scaleway Secret Manager**: recreate each `FLOW_*` secret as
      `MYCELIUM_*` under path `/mycelium/prod` with the SAME value (or move the
      folder), then point ESO at them (already done in `eso/external-secrets.yaml`:
      `remoteRef.key: name:MYCELIUM_*`). The k8s Secret names + keys are all
      `mycelium-*` / `MYCELIUM_*` now. Values are unchanged.
- [ ] **Kapsule node pool** `flow` -> `mycelium`: create a new pool named
      `mycelium` with taint `dedicated=mycelium:NoSchedule`, label
      `pool=mycelium`; the renamed `nodeSelector`/`tolerations` schedule pods
      there. Drain + delete the old `flow` pool. The model-cache PVC is block
      storage and re-attaches (no data loss; the model just re-downloads if the
      PVC is recreated).
- [ ] **Telegram bot** `@flow_leto_bot` -> a new mycelium-branded bot via
      @BotFather. Telegram does not let you change an existing bot's
      @username, so mint a fresh bot (e.g. `@mycelium_garden_bot`); its
      @username is a free choice and need not echo any personal name. Then
      update the `MYCELIUM_TELEGRAM_BOT_TOKEN` + `MYCELIUM_TELEGRAM_BOT_USERNAME`
      SM values and re-register the webhook. Note: the bot is a single global
      platform bot (one per deployment); each user links their own chat to it
      (per-user `telegram_links` row), so existing users must re-link after a
      token change.
- [ ] **`og.png`**: re-export `assets/mycelium-og.svg` to PNG (the raster can't
      be edited as text). The 6 logo SVGs were redesigned as a mycelial network
      (no wordmark); the social-card PNG needs regeneration.
- [ ] **Project named `flow`** in the DB (your data): rename it in-app if you
      want it gone from the UI (the diag scripts key off its UUID, not the name).

The transactional email FROM is now `no-reply@mycelium.xeno.garden` (§3) and
the SdI RicezioneNotifiche host is `sdi.mycelium.xeno.garden` (re-declared at
AdE). The only `flow` left in the deploy repo is the literal word "workflow"
(GitHub Actions) and `flow.leto.blue` in `CUTOVER-*.md`, which documents a PAST
migration and is historical (delete/archive it if it bothers you).

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
      `...Flow...` to `...Mycelium...`. Existing `flow_at_…` agent tokens NO
      LONGER validate (legacy acceptance was dropped) — mint a fresh
      `mycelium_at_…` token and paste it into the connector config.
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
