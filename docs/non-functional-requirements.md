# Non-functional requirements

## Security

- At-rest encryption = encryption of the Postgres **volume** + object
  storage (LUKS / encrypted block storage / managed TDE): body,
  tsvector and embedding stay indexable. The app-level envelope
  (libsodium/Fernet) is reserved for **opaque, non-indexed secrets**:
  OAuth tokens, credentials, SdI channel material. Stated threat model:
  protects against a stolen disk/snapshot, not against a live DB
  connection. See
  [ADR-0006](adr/0006-at-rest-volume-encryption.md).
- SdI channel certificate and (post-v1, if PA) a qualified certificate
  with dedicated custody (HSM or remote signing).
- The always-on, mutual-TLS inbound SdI SOAP endpoint is a new attack
  surface and an availability commitment: it must be treated as such
  (not as a polling worker).
- RBAC in the service layer; rate limiting on SMTP and external calls;
  append-only audit log of sensitive actions (invoice send, email
  send, workflow change, SdI channel change, cross-project memory
  access).

## Privacy and GDPR

- Per (org, project) isolation; explicit provenance and propagation of
  erasure to embedding, summary, object storage and consolidated
  blobs; no cross-subject merge.
- Local embedding: the email body does not leave the perimeter.
  Summarization/consolidation via an external LLM only with explicit,
  audited per-Org opt-in; tracking of what leaves the perimeter.

## Multi-tenant isolation

Mandatory RLS on every org-scoped entity. Memory partitioned by
`org_id` with a mandatory (org, project) predicate in every query.
Explicit test: an unfiltered search must never return data from
another tenant or another project.

## Performance and ARM node

Deterministic CPM O(V+E) (milliseconds on hundreds of tasks).
Per-partition HNSW with a stated RAM budget. Heavy re-embedding
off-peak or on a larger transient node; `halfvec`/quantization if
needed.

## Reliability

Idempotent IMAP sync and SDI callbacks, retry with backoff, fault
isolation per account/channel.

## Cloud, K8s-ready

Stateless services, arm64 images, externalized config/secrets
(12-factor), object storage for attachments/blobs, health/readiness
probes, scalable workers. v1 single-node deploy (Docker Compose)
portable to K8s. Known stateful exceptions: Postgres, Proton Bridge
sidecar, inbound SdI SOAP endpoint.

## Built-in extensibility

API-first for future mobile; notification-channel abstraction;
versioning + event log for future collaboration; pluggable `Embedder`,
`LLMProvider`, `SdiChannel`, `ConservationProvider`; generic memory
namespace.

## Observability

Structured logging, health endpoint, job metrics (email sync,
scheduler, memory/re-embedding, SdI receipts).

## Testing

Domain unit tests (the 4 dependency inequalities over a calendar with
holidays, per-person serialization, no-ubiquity, RBAC, state machine,
RRF, GDPR erasure, concurrent numbering, advisory feasibility/ranking
and within-budget knapsack selection, advisory determinism for equal
input), API integration, MCP tool tests, multi-tenant and
multi-project fixtures, golden FatturaPA XML for several cases,
SdICoop channel contract tests against the SdI test environment.
