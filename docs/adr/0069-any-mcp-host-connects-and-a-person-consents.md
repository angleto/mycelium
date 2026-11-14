# ADR-0069: Any MCP host connects through OAuth, and a person consents

Status: Accepted (2026-11-13)
Relates to: the device authorization grant (migration 0013), whose shape the
consent flow reuses: a party holding nothing asks, a logged-in person
answers, and the credential is created when the asking party collects.

This record decides how an MCP host that implements the MCP authorization
specification obtains a Mycelium credential without anyone copying a secret,
how the mechanism is split so that new ways of identifying a client and new
kinds of token can be added without touching the rest, and what was fixed on
the way so that such a host gets far enough to try.

## Context

Until now the only OAuth path was a shim built for one host. A person created
an AI assistant in the settings, then pasted its UUID as `client_id` and its
`mycelium_at_` token as `client_secret` into Claude's connector dialog.
`/authorize` minted a code at once and `/token` returned the secret itself as
the access token. There was no dynamic client registration and no consent
page.

Hosts that follow the specification as written (Claude web, Desktop and Code,
ChatGPT, Cursor, VS Code, ...) do not ask for a secret. They read
`WWW-Authenticate` on the first 401, fetch the protected-resource metadata,
then the authorization-server metadata, identify themselves either by
registering (RFC 7591) or by a Client ID Metadata Document (an https URL as
`client_id`, draft-ietf-oauth-client-id-metadata-document, adopted by the
current MCP specification), send the person's browser to the authorization
endpoint, and redeem a code with PKCE. Three things measured in production on
2026-11-13 stopped them before OAuth started:

- `POST /mcp` answered `307 -> /mcp/`, because the MCP app is a Starlette
  mount and a mount only matches below its prefix. Several hosts do not follow
  a redirect on POST.
- The MCP 401s carried no `WWW-Authenticate` header, so a host had no URL to
  start discovery from.
- `/.well-known/openid-configuration` and every other unknown well-known path
  fell through nginx to the SPA and came back `200 text/html`.

And the providers and the specifications will keep moving: how a client is
identified has changed twice in a year (static secrets, dynamic registration,
metadata documents), and what a token should be (long-lived bearer, short-
lived plus refresh) is open. The mechanism has to absorb the next change in
one place.

## Decision

### Three layers

The code lives in `mycelium_core.oauth`; the API router is an HTTP adapter
over it and knows no strategy by name.

1. **Client resolution** (`oauth.resolution`, `oauth.strategies`). One
   contract, `ClientResolver`, and an ordered `ClientRegistry`; the first
   enabled strategy whose `claims(client_id)` is true owns the id. A strategy
   resolves an id into a `ClientDescriptor` carrying: its kind, display name
   and registration (what the consent page says about its origin), its
   registered redirect URIs or `None` ("any URI the redirect policy
   accepts"), whether each authorization needs a person's consent, how it
   authenticates at /token (`none` or a secret) and, for the latter, the
   authenticator that checks the secret. It also contributes to discovery:
   `auth_methods` (their union across active strategies is
   `token_endpoint_auth_methods_supported`) and `metadata_fields` (merged into
   the metadata). A switched-off strategy neither claims nor contributes.
2. **Authorization** (`oauth.flow`, with `services/oauth_consent.py` for the
   consent requests and `services/oauth_codes.py` for pre-authorized codes).
   One path for every strategy, branching only on the descriptor's
   `requires_consent` and `token_auth`, never on its kind.
3. **Credential issuance** (`oauth.issuance`). The flow ends with a grant (an
   approved consent, or a pre-authorized client that proved its secret) and
   hands it to the installed `CredentialIssuer`, which builds the token
   response. Today's `AssistantBearerIssuer` creates the AI assistant and its
   bearer for an approval, and returns the client's own secret for a
   pre-authorized assistant.

### The strategies today

| strategy | `client_id` | consent | /token auth | setting (`MYCELIUM_` prefix) |
|---|---|---|---|---|
| `registered` | `mycelium_pc_...`, a row in `oauth_clients` (dynamic or by hand) | yes | `none` + PKCE | `OAUTH_REGISTERED_CLIENTS_ENABLED`; the open registration endpoint also needs `OAUTH_DYNAMIC_REGISTRATION_ENABLED` |
| `metadata_document` | an https URL | yes | `none` + PKCE | `OAUTH_CLIENT_METADATA_DOCUMENTS_ENABLED` |
| `assistant_secret` | an AI assistant UUID | no (decided in the settings) | the assistant's bearer | `OAUTH_ASSISTANT_SECRET_CLIENTS_ENABLED` |

All default to on. The discovery document is generated from the active ones:
`registration_endpoint` only while dynamic registration is open,
`client_id_metadata_document_supported: true` only while metadata documents
are on, `none` among the auth methods only while a public strategy is on,
the secret methods only while assistant secrets are. `POST /oauth/register`
answers 404 while dynamic registration is off.

The ids are disjoint by construction (a prefix, an https URL, a UUID), so
today the order decides nothing; it is there for a future strategy whose ids
overlap.

### How to add a strategy

Write a module under `core/src/mycelium_core/oauth/strategies/` with a class
satisfying `ClientResolver` (`name`, `token_auth`, `requires_consent`,
`auth_methods`, `enabled(settings)`, `claims(client_id)`, `resolve(client_id,
context)`, `metadata_fields(issuer, settings)`, `record_grant(session,
client_id)`), give it a setting to switch it off, and add an instance to
`strategies.default_registry`. A strategy that also offers registration
implements `SupportsDynamicRegistration` (`registration_open`,
`register_client`); `/register` is served by the first active one that does,
through `ClientRegistry.registration`, so the endpoint names no strategy.

What it must not touch: the endpoints, `oauth.flow`, the consent service,
the credential issuer, the redirect policy, or another strategy. `claims`
must be pure and cheap (it runs on every request, including /token, where no
I/O is allowed for a consent client) and must not overlap another strategy's
ids. `resolve` is called with NO database session open and must not hold one
across I/O of its own: a lookup opens a short session of its own, a network
fetch holds nothing (`ResolveContext.origin` is there to meter it per
caller). If the new strategy seems to need any other change, the contract is
what is missing something, and the contract is what changes, deliberately
and for every strategy.

Adding a kind of token is the same move one layer down: a class satisfying
`CredentialIssuer`, installed with `set_credential_issuer`. A refresh grant
would also need the flow to accept `grant_type=refresh_token`, which is a
change to the flow and is not anticipated here.

### Consent

For a consent client `/authorize` mints no code. It resolves the client,
checks the redirect URI against it, and stores the request (client id, name
and kind as resolved now, redirect URI, challenge, state, resource, requested
scope; ten minutes; an opaque id whose digest is stored) in
`oauth_authorization_requests`, then sends the browser to the SPA's
`/oauth/consent?request=<id>`. That page requires a login (the SPA's
return-to mechanism carries the query through it), shows the client name and,
more prominently, the redirect target as scheme and host
(`https://claude.ai`, `cursor://anysphere.cursor-mcp`), the current workspace
with the usual switcher, an editable label, and the scope picker of the
AI-assistants settings, preselecting the requested scope when it names only
catalogue keys and the read-only default otherwise. Approve and Deny are
HUMAN_ONLY JSON calls authenticated by the SPA's bearer, which a page on
another origin cannot attach, so they are not exposed to request forgery.
Approve checks the same role rule as creating an assistant by hand, records
who, where, label and scope, mints the code and returns
`redirect_uri?code=...&state=...`; Deny returns
`redirect_uri?error=access_denied&state=...`.

The credential is created at the exchange, not at approval: `/token` claims
the code (single use, ten minutes, burned even by a failed attempt), checks
client, exact redirect URI, PKCE verifier and resource, and only then hands
the approval to the issuer, in the same transaction. A host that never comes
back for its code leaves no assistant behind.

The consent page and /token read the stored request, never the client: for a
metadata-document client that would be a network fetch, and only /authorize
fetches.

### Client ID Metadata Documents and their defences

`oauth.cimd` fetches the document on an unauthenticated path, so it is a
server-side request forgery surface, and each defence answers one way of
abusing it:

- https only, port 443 or none; the id must be a plain URL with a path, no
  userinfo, no fragment, no dot segments, no backslash or `%` in the
  authority, at most 2048 characters. Any other port would make the fetch a
  probe of whatever listens there.
- Metered BEFORE any I/O, for cache misses only: 10 fetches per origin per
  minute, 120 for everybody, at most 4 in flight -- the excess refused at
  once, never queued. Per process, so a deployment bounds itself at replicas
  times these numbers. /authorize also checks the origin's budget of
  unanswered requests before the client is resolved at all.
- Resolved and fetched with no database session open: the flow resolves the
  client before it opens one. Name resolution runs on a thread pool of its
  own (4 threads), and the whole fetch, resolution included, is abandoned
  after 3 seconds. A lookup abandoned at the timeout still holds its thread,
  so the meter counts pool threads until they really end, not until the
  fetch gives up: with the pool busy a fetch is refused at once. Being full
  is a LOCAL condition, so it is never negative-cached against the id asked
  for; only failures attributable to the remote host are. No asyncio-native
  resolver is among the dependencies, and adding one for this was judged not
  worth a new dependency while the thread accounting holds.
- The document's JSON may nest at most 32 levels, counted on the bytes
  before parsing (brackets inside strings excluded); `json.loads` recurses,
  and a 16 KiB run of `[` raised RecursionError through the request as a 500.
  Too deep is an invalid document, negative-cached like any other.
- The name is resolved once and every address must be global
  (`ipaddress.is_global`, multicast excluded). An IPv6 address carrying an
  IPv4 address is judged by the IPv4 it carries -- IPv4-mapped,
  IPv4-compatible, NAT64 `64:ff9b::/96`, 6to4 `2002::/16`; local-use NAT64
  `64:ff9b:1::/48`, SIIT `::ffff:0:0:0/96`, site-local `fec0::/10` and Teredo
  are refused. One bad address refuses the name.
- The id's authority gets the redirect policy's strict parse: ASCII LDH
  labels, canonical dotted-quad IPv4, no trailing dot; and no dot segment,
  percent-encoded or not, in its path.
- The connection goes to the verified address, with the name as `Host` and as
  TLS SNI, so the certificate is still checked against the name and there is
  no second lookup for DNS rebinding to exploit. No proxy from the
  environment. Redirects are not followed.
- `Accept-Encoding: identity`, any `Content-Encoding` other than identity
  refused, and the body read raw (`aiter_raw`) up to 16 KiB counted on the
  wire. Decoding before counting let 496 bytes of nested gzip become 459 MB.
- Every network outcome -- no address, non-public address, refused
  connection, timeout, TLS failure, any status but 200, wrong content type
  or encoding, too large -- is the same answer to the client; the detail is
  logged. Otherwise the fetch reports which ports and services answer where.
- The document must name exactly its own URL as `client_id`, carry redirect
  URIs the redirect policy accepts, carry no secret and no authentication
  method other than `none`.

Cache: in the process, at most 512 entries, least recently used evicted.
A document lives for its `Cache-Control: max-age` clamped to between 5
minutes and 24 hours (absent or `no-store` gets the 5-minute floor); a
failure is remembered for one minute. In the process rather than in Postgres
because what is cached is public, cheap to refetch and not needed for
correctness, and a table would add writes on an unauthenticated path.

### Redirect policy

One component, `oauth.redirects.RedirectPolicy`, used by every strategy.
Accepted: `https://` with a host; `http://` on `127.0.0.1`, `localhost` or
`[::1]`; a private-use scheme (RFC 8252 §7.1: `cursor://...`, `vscode://...`,
`com.example.app:/cb`) only if listed in
`MYCELIUM_OAUTH_PRIVATE_USE_REDIRECT_SCHEMES` (JSON list, empty by default).
Always refused, whatever the configuration: `javascript:`, `data:`, `file:`,
`about:`, `blob:`, `vbscript:`, `http` off loopback, fragments and userinfo;
listing one of these schemes stops the service at startup.

The authority is parsed strictly, and anything a browser's WHATWG URL parser
would read differently is refused rather than interpreted, because the
consent page shows the host this server parsed and the code goes to the host
the browser picks. Measured against node's `URL`: a backslash is a path
separator (`https://evil.example` + backslash + `.claude.ai/cb` goes to
`evil.example`), `%` in a host is decoded, non-ASCII becomes punycode, and a
numeric last label makes a host an IPv4 shorthand (`127.1`, `0x7f.1`). So:
ASCII only, no backslash anywhere, no `%` in the authority, no userinfo, LDH
labels, no trailing dot, only canonical dotted-quad IPv4. A host already in
punycode is accepted and shown as punycode. The consent page shows the host
lower-cased, without the scheme's default port, as a browser shows it.

Matching is exact string equality except that an `http` loopback URI
matches on any port (RFC 8252 §7.3). A registered URI the policy no longer
accepts is not honoured, so removing a scheme from the allowlist takes
effect on clients already registered. No allowance for any vendor.

### The consent page cannot be framed by another site

The consent page's Approve button mints a credential, so a page that framed
it could steer a logged-in person's click. nginx sends
`Content-Security-Policy: frame-ancestors 'self'` and
`X-Frame-Options: SAMEORIGIN` with the SPA document. `'self'` rather than
`'none'`: the app frames its own same-origin documents (the PDF attachment
preview is an iframe on a `blob:` URL), and the browser extension does not
embed the SPA.

### Discovery fixes

`/mcp` is rewritten to `/mcp/` before routing, so the mount serves it with no
redirect. Every MCP 401 carries
`WWW-Authenticate: Bearer resource_metadata="<issuer>/.well-known/oauth-protected-resource/mcp"`,
built by the same function as the metadata (`mycelium_core.public_origin`,
which also moves `MYCELIUM_PUBLIC_BASE_URL` into `Settings`). nginx answers
any other `/.well-known/` path with 404.

### Verified with the official MCP SDK

`api/tests/test_mcp_sdk_e2e.py` runs the backend under uvicorn on a free
loopback port and connects with the official MCP Python SDK client
(`streamable_http_client` + `OAuthClientProvider`), pointed at `<base>/mcp`
without the slash and with redirects NOT followed (Claude Desktop's
behaviour; the SDK's default client follows them). Three flows: dynamic
registration, a Client ID Metadata Document (the document served by a fake
network installed on the server's fetcher, since a real fetch to loopback is
exactly what the fetcher refuses), and a static bearer with no OAuth. Each
ends with `initialize`, `list_tools` and a read-only tool call, and asserts no
response under `/mcp` was a redirect. A small stand-in for nginx strips `/api`
as production does. Driving the SDK found two defects, both fixed here:

- **Dangerous scopes preselected.** The SDK requests every key in
  `scopes_supported`, and the consent page preselected whatever a client
  asked for. It now preselects requested read and write keys only; a
  dangerous key is ticked by the person, with the picker's confirmation.
- **Sporadic 500 on `POST /mcp`.** The MCP bearer middleware was a
  `BaseHTTPMiddleware`, which raises "No response returned." when the
  streamed exchange ends without its internal stream yielding a response
  start; 2 runs out of 6 failed. It is now raw ASGI and passes `send`
  through.

### Where a person finds it

Settings has a **Connections** tab (`/settings/connections`), between the
browser extension and the workspace: the server URL to paste
(`<origin>/mcp/`), the ways a host can identify itself, the AI assistants and
the hand-registered OAuth clients, under the name of the workspace they
belong to. The Workspace tab keeps only workspace configuration and links to
Connections from where the assistants used to be.

### Bounds and storage

Registrations and authorization requests are bounded per origin -- an IPv4
address, or an IPv6 /48 (`oauth.origin`) -- at 30 unfinished per window;
completed ones do not count. Past that the origin is refused. /48, because a
single host rotates through its /64 at will and a single subscriber is
routinely given a /56 or a /48, so a /64 bucket gave one caller up to 65536
budgets; the cost is the IPv4 one, made explicit: one site shares a budget
as one NAT address does. A second, finer per-/64 bound was rejected: it would
add budget, not remove any.

The global ceilings (5000 unused registrations, 5000 unanswered requests) do
NOT refuse: reaching one evicts the oldest evictable rows to make room,
because refusing let one caller lock everybody out. What is evictable is
narrow, and the predicate is in the DELETE statement itself, not only in the
SELECT that chose the candidates, so an answer that lands in between is seen
and the row stays:

- a request only while NOBODY has answered it; an approved request (a
  person's decision whose code may not be redeemed yet) or a denied one never;
- a dynamic registration only while it has never completed an exchange AND
  no approved consent for it is waiting to be redeemed; a hand-registered
  client never. Its requests go with it only if they are still unanswered.

An unanswered request can still be evicted by a flood, which costs the
person who left a consent page open a retry; in production the flood has to
come from many attributable /48s (see production configuration). A worker
sweep deletes, by the same predicates, unused registrations after a day, and
requests a week after they expired.

`oauth_clients` is under row-level security: a person's session sees the
hand-registered clients of its own workspace; the system session with no
workspace, which the pre-tenant endpoints and the sweep run in, sees every
row. A CHECK pairs `registration = 'manual'` with a workspace and an author.
`oauth_authorization_requests` is pre-tenant like `device_authorizations`;
every read is by the digest of an unguessable value. Its `client_id` is not a
foreign key, because a metadata-document client is an https URL no table
holds; removing a hand-registered client deletes its requests explicitly,
including an approved one not yet exchanged. The legacy `oauth_codes` table
stays with the pre-authorized path; a consent client's code digest lives on
the request row.

**Provenance and audit.** An assistant created by a consent gets a row in
`ai_assistant_oauth_grants` (workspace RLS, lives as long as the assistant):
client id, kind, name, redirect URI, approver and moment. The Connections tab
shows it on the assistant ("connected through OAuth by <client>
(<scheme://host>), approved <date>") together with the token's expiry, and
the consent page's default label is "<client> via <host>", so the list a
person reviews says who holds each token. Approve and deny emit
`oauth.consent_approved` / `oauth.consent_denied` security events.

### Binding checks at consent and exchange

- Describing or approving a request re-checks that its client's strategy is
  still switched on; a request opened before a strategy was turned off reads
  as absent. The hand-registered client routes answer 404 while the
  registered-clients strategy is off.
- `/token` refuses a `resource` that differs from the one stored at
  /authorize, even one this server would otherwise accept (the MCP endpoint
  named at another host).
- The PKCE verifier must be 43 to 128 characters of the unreserved set
  (RFC 7636 §4.1), and a consent client's S256 challenge exactly 43 base64url
  characters.

### Production configuration

Two settings are required for dynamic registration and metadata documents,
in ANY environment, and this ADR is where that is decided:

- **`MYCELIUM_PUBLIC_BASE_URL`** (the public origin, e.g.
  `https://mycelium.example`). It is the ONLY protection of the issuer's
  identity. Without it the issuer, the metadata URLs and the MCP challenge
  are built from `X-Forwarded-Proto` / `X-Forwarded-Host` / `Host`, and nginx
  forwards `$host`, which is the client's own Host header: a caller choosing
  its Host chooses the name this server describes itself under. What the
  backend does guarantee without the setting is narrower: it accepts only a
  well-formed host (full-match grammar) and `http`/`https`, and escapes the
  challenge value, so no header can inject a parameter into
  `WWW-Authenticate`. nginx's `$scheme` behind a TLS-terminating load
  balancer is also `http`, another reason to pin the origin.
- **`MYCELIUM_ISSUER_KEY_TRUSTED_PROXIES`** (JSON list of CIDRs: the load
  balancer's and the nginx pods' addresses). The client address is the
  rightmost `X-Forwarded-For` hop that is not a trusted proxy
  (`deps._resolve_issuer_client_ip`; nginx appends `$remote_addr` through
  `$proxy_add_x_forwarded_for`). Without it, behind the proxy chain, no
  request's origin can be attributed and every per-origin bound is off.

**Missing either, dynamic registration and metadata documents are switched
off** (`oauth.production`): they leave the discovery document, `/register`
answers 404, https client ids are refused, and the app logs an error at
startup naming what is missing. The gate does not depend on recognising the
environment: an earlier version applied it only when `MYCELIUM_ENV` said
production, and since that defaults to `dev` and nothing else reads it, a
production deployment that never set it ran both strategies unprotected --
the gate failed open. Switched off rather than refusing to start, because
nothing else -- the app, the assistant-secret connector, hand-registered
clients -- depends on either setting.

The one way to run them without the two settings is
`MYCELIUM_OAUTH_UNATTRIBUTED_ORIGINS_OK=true`, named for what it costs: callers
are unattributed (no per-origin bound) and the issuer follows the Host
header. It is for a development stack or the test suite, which sets it in
`conftest.py`; `.env.example` ships it off. It is honoured only where
`MYCELIUM_ENV` is `dev`, `test` or `local`, and IGNORED, with an error at
startup, anywhere else: an allowlist, because the first version refused it
only for the spellings of production, and `prd`, `staging` or an empty value
then ran the strategies unattributed.

A value that is set but wrong stops the process instead (`oauth.deployment`):
a base URL that is not an https origin, a proxy entry that does not parse, or
a proxy list that covers a whole address family.

nginx: an explicit `server_name` for the public host with a `default_server`
answering 444 was considered and NOT applied. The cluster manifests (probes,
load balancer health check) are not in this repository, a kubelet HTTP probe
sends the pod address as Host, and a 444 default would fail it and take the
site out of rotation. Pinning the issuer with `MYCELIUM_PUBLIC_BASE_URL` closes
the same hole without that risk.

## Consequences

- Any specification-following MCP host connects with nothing but the server
  URL; a host that wants a client id pasted has one from the settings; a host
  that identifies itself by URL needs no registration at all.
- Each approval creates one AI assistant, visible and revocable under AI
  assistants. Deleting a hand-registered client stops new connections through
  it and does not revoke assistants earlier consents created.
- The pre-authorized `/authorize` redirect check uses the shared policy,
  which refuses `http://localhost.evil.example` that the old prefix test
  accepted. A pre-authorized code still needs the secret, so nothing leaked
  through it.
- The token request log no longer prints a prefix of the client secret.

## Limits

- **No refresh tokens.** The access token is the assistant's bearer and lives
  as long as that bearer does (a year by default); then the host runs the
  consent again. `refresh_token` is accepted in a registration request and
  left out of what is registered (RFC 7591 §3.2.1).
- **One assistant per approval.** Reconnecting creates a new assistant; the
  old one stays until revoked.
- **Client names are claims.** A dynamic registration's or a metadata
  document's `client_name` can impersonate anybody, which is why the consent
  page puts the redirect target first. The page cannot make a person read it.
- **The bounds are per attributed origin.** A distributed caller with many
  /48s or IPv4 addresses gets that many budgets, and by filling the ceilings
  can evict other people's unused registrations and unanswered requests --
  which costs them a retry, never an approved consent, never a credential,
  and never locks anyone out. The metadata-document meter is per process.
- **The opt-out trusts `MYCELIUM_ENV`.** A production deployment that sets
  `MYCELIUM_OAUTH_UNATTRIBUTED_ORIGINS_OK=true` AND declares itself `dev`,
  `test` or `local` runs the open strategies unattributed. Off is the
  default, and turning it on takes two explicit settings that both say
  "this is not production".
- **Token lifetime.** An assistant's bearer lives 365 days (shown in the
  Connections tab); short-lived tokens with refresh are the issuer's job and
  not done here.
- **A metadata-document fetch** costs this process up to 3 s, one outbound
  connection and one DNS thread per uncached id, at most 4 at a time. A
  public host serving a document with its own redirect URIs is exactly a
  legitimate client: the consent page is the control for it.
- **No CORS on the OAuth endpoints.** A purely in-browser host on another
  origin does not work.
- **The request id travels in the consent page's URL.** It is a handle on a
  pending request, not a credential: the page needs a login and answering
  needs that person's session; it is single use and expires in ten minutes.

## Rejected

- **Metadata documents only, without dynamic registration.** Not every host
  implements them yet; both are strategies and either can be switched off.
- **Dynamic registration without a consent page**, minting a code at
  `/authorize` as the original flow does. With open registration that hands a
  credential to anybody who can make a logged-in person's browser follow a
  link. The consent page is the control, not a courtesy.
- **Keeping only static secrets.** It works for one host's dialog and for
  nobody who follows the specification, and it makes the person copy a
  long-lived secret by hand.
- **Strategy branches in the endpoints** (the first shape of this design:
  `if is_public_client_id(...)` in /authorize and /token). Every new way of
  identifying a client would have edited the token path, which is the path
  that must change least.
- **Creating the assistant at approval.** A host that never redeems its code
  would leave an assistant nobody holds a secret for, on the settings page,
  looking like a connection.
- **A Postgres cache for metadata documents**, shared across replicas: see
  the cache paragraph above.
