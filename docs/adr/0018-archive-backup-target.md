# ADR-0018 Archive backup target (dual copy), distinct from legal conservation

Status: accepted. Origin: user request for a Proton Drive backup copy.

## Context

The user wants a redundant copy of archived data (invoice XML, SdI
receipts, possibly cold memory blobs/attachments) on Proton Drive, in
addition to the database: "double conservation, DB + Proton Drive".

Two facts shape the decision:

1. **Backup is not legal conservation.** Conservazione sostitutiva a
   norma (ADR-0010, AdE free service) is a regulated process
   (responsabile, manuale, hashing/time reference, exhibition). A file
   copy on Proton Drive is redundancy, not a second legal conservation.
   Conflating them would create false compliance confidence.
2. **Proton Drive has no stable official API (May 2026).** SDK is in
   preview; official CLI targeted Q2 2026. The realistic programmatic
   path today is the community **rclone Proton Drive backend** (E2E
   encryption preserved, headless-capable, maintained with Proton
   engineer input), which needs full account credentials, not a share
   link. A shared-folder link is not a writable API.

## Decision

- Introduce `ArchiveBackupTarget`, a pluggable abstraction **separate
  from `ConservationProvider`**. It mirrors selected archived artifacts
  to an external store. It is explicitly labeled redundancy/backup, not
  legal conservation (which stays ADR-0010).
- **v1 backend = S3-compatible object storage** (EU region). Reliable,
  already anticipated by the design for blobs/attachments. Delivers the
  requested "DB + external store" double copy now.
- **Proton Drive backend = pluggable, experimental/deferred**, via an
  rclone sidecar configured with a dedicated Proton account stored in
  the secret manager (operationally akin to the Proton Mail Bridge
  sidecar, ADR-0011). Swap to Proton's official SDK/CLI when stable
  (drop-in, the abstraction isolates it).
- Dual-write is **asynchronous, idempotent, retried**, performed by the
  worker; per-artifact backup status is recorded. Backup-target failure
  must NOT block issuance (degraded + alerted + reconcilable).

## Consequences

- Resilience and user-controlled, E2E-encrypted, EU redundancy without
  making the external store a hard dependency of the issuance path.
- Proton Drive carries community-tooling and ToS risk until the
  official SDK is stable; isolated behind the abstraction.
- New per-artifact `backup_status`/`backup_ref` state; worker job.
- "Share a folder" is reframed: the system writes via rclone logged
  into a controlled Proton account into a configured Drive path, not
  via an ad-hoc share link.

## Alternatives rejected

- Presenting the Proton Drive copy as legal conservation: false
  compliance; legal conservation stays ADR-0010.
- Proton Drive as the only/primary backend now: no stable API; would
  block a core resilience feature on community tooling.
- Hard, synchronous dual-write before issuance: couples issuance
  availability to an external store.
