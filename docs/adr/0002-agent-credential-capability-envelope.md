# ADR 0002: Agent Credential and Capability Envelopes

## Status

Accepted for the closed-by-default Agent rollout.

## Decision

Worker is the only component that selects a Provider and decrypts BYOK
credentials. For each execution epoch it sends Runtime one HMAC-authenticated,
in-memory provider envelope containing only the selected adapter, endpoint,
model, bounded headers, credential, verified modality capabilities, proxy, and
optional DNS-pinned addresses. Runtime never writes this envelope to a Pi auth
store, file, log, metric, trace, or error response.

The image tool uses a separate API capability. Worker signs claims binding the
run, user, Agent session, execution epoch, exact tool set, exact reference
labels, random nonce, and expiry. Worker persists the corresponding bounded
grant before dispatch. Runtime can forward only strict business arguments.
API revalidates the claims, row ownership, epoch, references, parameters,
billing, grant redemption, and semantic idempotency before creating a
Generation. A repeated exact callback returns the original receipt; a new
ordinal consumes one grant redemption, and no callback is accepted after the
grant expires or reaches its snapshotted tool-call bound.

`AGENT_RUNTIME_SHARED_SECRET` and `AGENT_TOOL_CAPABILITY_SECRET` must be
different random values of at least 32 bytes. Installer and updater backfill
missing transport secrets without printing them. A non-empty weak value is
rejected instead of silently rotated.

## Consequences

- Browser and Web never receive either internal secret or a Provider key.
- Runtime compromise does not grant database, Redis, storage, or arbitrary
  asset access.
- Provider and tool retries remain governed by Worker/API durable evidence.
- Horizontal Runtime replicas remain unsupported until nonce redemption is
  durable across replicas.
