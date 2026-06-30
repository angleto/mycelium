# ADR-0006 At-rest encryption at the volume level

Status: accepted. Resolves an internal contradiction in an earlier
draft.

## Context

A draft required "email body encrypted at-rest in Postgres" with
app-level primitives (libsodium/Fernet) and, at the same time,
full-text `tsvector` and a local embedding of the body. These are
incompatible: a GIN/tsvector index extracts lexemes from cleartext, and
an embedding of ciphertext is noise. With app-level body encryption,
FTS and embedding over the body do not work.

## Decision

At-rest encryption = encryption of the Postgres **volume** + object
storage (LUKS / encrypted block storage / managed TDE): body,
`tsvector` and `embedding` stay in cleartext inside the DB but the disk
is encrypted, so they remain indexable and searchable. The app-level
envelope (libsodium/Fernet) is reserved for **opaque, non-indexed
secrets**: OAuth tokens, credentials, SdI channel material. Stated
threat model: protects against a stolen disk/snapshot, not against a
live DB connection or a compromised app/DBA.

## Consequences

- Body FTS + embedding work (hybrid-retrieval requirement).
- Confidentiality against an attacker with a live DB is not covered by
  this measure: a stated, not implicit, choice; compensated by
  mandatory RLS (ADR-0002) and isolation (ADR-0007).

## Alternatives rejected

- App-level envelope of the body: breaks FTS and embedding (the
  original contradiction).
- Searchable encryption (EQL/encrypted indexes): no cosine ANN over
  encrypted vectors; complexity out of proportion for a single ARM
  node.
