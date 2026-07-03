# Runbook: ISSUER_KEY_PEPPER (issuer-API-key keyed hash)

The pepper is the HMAC key under which every issuer-API-key secret is hashed
at rest (`issuer_api_keys.secret_hash = HMAC-SHA256(pepper, raw)`, ADR-0045).
A database-only dump is inert without it. It lives ONLY in the secret manager
and the pod env, never in the database or its backups. The app is fail-closed:
without `MYCELIUM_ISSUER_KEY_PEPPER` (min 32 chars) the backend refuses to
start.

Because no raw secret is ever stored, existing hashes cannot be recomputed
under a new pepper: **changing the pepper invalidates every existing key**
unless the rotation window below is used.

## Bootstrap (new environment)

1. Generate: `openssl rand -hex 32`.
2. Store in the secret manager (prod: Scaleway SM, secret
   `MYCELIUM_ISSUER_KEY_PEPPER`, same project as the other prod secrets;
   pass the RAW value -- `scw secret version create data=` base64-encodes
   internally, never pre-encode).
3. Map it into the `mycelium-secret` k8s Secret (ESO,
   `eso/external-secrets.yaml`) and into the env of every Settings consumer:
   backend, worker, sdi-inbound, admin-job (already wired in the deploy
   manifests).

## Planned rotation (orderly, zero downtime)

The dual-pepper window (`MYCELIUM_ISSUER_KEY_PEPPER_PREVIOUS`, task
d3dd69c3): `authenticate` probes the current pepper first and, only on a
miss, the previous one. New/rotated keys always hash under the CURRENT
pepper.

1. Generate the new pepper; set `MYCELIUM_ISSUER_KEY_PEPPER=<new>` and
   `MYCELIUM_ISSUER_KEY_PEPPER_PREVIOUS=<old>`; roll the deployments.
   Every existing key keeps authenticating (via the previous probe).
2. Re-mint each key: `POST /issuer-profiles/{id}/api-keys/{kid}/rotate`
   (grace as needed) and hand the new raw to the integrator. Rotated
   secrets hash under the new pepper.
3. Watch the `issuer_key.previous_pepper_used` security events (below):
   when they stop, every live key is on the new pepper.
4. Unset `MYCELIUM_ISSUER_KEY_PEPPER_PREVIOUS`; roll the deployments.
   The old pepper is dead.

## Compromise response

If the pepper leaks TOGETHER with a database dump, the attacker can verify
candidate raws offline (not forge new ones: raws have 256 bits of entropy,
but any raws they also hold are confirmable). Response is key-centric, not
pepper-centric:

1. Immediately revoke or hard-rotate (grace 0) every issuer key
   (`DELETE`/`rotate` on each; revoke kills current + grace secrets at once).
2. Rotate the pepper with the window above, but keep the window SHORT: while
   `PREVIOUS` is set, hashes under the leaked pepper still authenticate.
3. Audit `activity_log` (actor_kind `issuer_api_key`) and the security
   events for the window of exposure.

## IP allowlist enforcement contract (READ before relying on it)

The per-key CIDR allowlist is a defence-in-depth control, and it is only a
real one once the deployment attributes the client IP from a position the
caller cannot forge. The app derives the source from the RAW
`X-Forwarded-For` header (uvicorn runs `--forwarded-allow-ips '*'`, which
makes `request.client` client-forgeable), taking the rightmost hop that is
NOT a configured trusted proxy. For that to be trustworthy:

1. Set `MYCELIUM_ISSUER_KEY_TRUSTED_PROXIES` (JSON list of CIDRs) to the real
   proxy hops in front of the backend (the frontend nginx pod range, plus the
   cloud LB egress range if it appears in the chain). While this is UNSET, a
   key that HAS an allowlist FAILS CLOSED behind the proxy (source
   unattributable -> `issuer_key.ip_unresolved` -> 401). A key with NO
   allowlist is unaffected.
2. Ensure the backend pod is reachable ONLY through nginx (a NetworkPolicy
   restricting `:8000`/the Service to the ingress). Otherwise a caller
   connecting directly to the pod controls the entire `X-Forwarded-For` chain
   and the rightmost-untrusted rule can be defeated.
3. Ensure the real client IP survives the cloud LB to nginx (Scaleway LB:
   proxy-protocol, or the Service `externalTrafficPolicy: Local`), else nginx
   appends the LB IP and no allowlist can distinguish clients.

The paired k8s manifests (a `NetworkPolicy` plus the `TRUSTED_PROXIES` env)
ship in the deploy repo; apply them WITH the rollout of the release that
carries this feature. Until (1)-(3) are in place, treat the allowlist as
"set but not yet enforced" -- it can only deny, never falsely allow.

Known limitation (accepted): `last_used_at` is bumped when a valid secret is
presented, before the IP gate, so a stream of `ip_denied` requests keeps a
key from looking dormant. This is telemetry-only: each such request already
emits the LOUDER `issuer_key.ip_denied`, so the dormant signal being masked
does not hide the attack.

## Security events (log-stack alerting)

The app emits single-line JSON events on the `mycelium.security` logger
(`security_event {"event": ...}` at WARNING); thresholds live in the log
pipeline, not in the app. Suggested starting points:

| event | alert on |
|---|---|
| `issuer_key.auth_failed` | spike per source ip (brute force / stuffing) |
| `issuer_key.ip_denied` | any occurrence (leaked key tried off-net, or an integrator moved egress) |
| `issuer_key.ip_unresolved` | any occurrence (an allowlisted key behind an unconfigured/untrusted proxy chain -- fix `MYCELIUM_ISSUER_KEY_TRUSTED_PROXIES`) |
| `issuer_key.dormant_key_used` | any occurrence (stolen dormant credential) |
| `issuer_key.rate_limited` | sustained occurrences (runaway integration) |
| `issuer_key.previous_pepper_used` | any occurrence AFTER a rotation window should be over |
| `issuer_key.grace_secret_used` | occurrences near `previous_secret_expires_at` (integrator not switching) |

Example (kubectl): `kubectl -n mycelium-production logs deploy/mycelium-backend | grep security_event | grep ip_denied`.
