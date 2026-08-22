# ADR 0003: Agent Deployment and Release Compatibility

## Status

Accepted for the closed-by-default Agent rollout.

## Decision

`agent-runtime` is an always-running core backend service. A database setting
cannot start a host Compose profile, so `agent.enabled` controls API behavior
and readiness, not container lifecycle. The Runtime uses a compatibility
profile only to let a supported four-image updater parse the first transition
release; current install, update, rollback, and `lumenctl` paths always activate
that profile and explicitly manage the service. While the fifth ref is absent,
the profiled service resolves to the already-immutable API image solely so an
old image enumerator sees no mutable or missing reference; it is never started.

When Agent is disabled, API readiness checks PostgreSQL and Redis but does not
require Runtime readiness. Runtime liveness remains a Compose health signal.
When Agent is enabled, API `/readyz` additionally requires both internal
secrets and paid-call-free Runtime `/healthz` plus `/readyz`. Admin enablement is
rejected until those checks pass.

Release manifest schema 1 retains the exact legacy `images` set
`api/worker/tgbot/web`. The additive `components.agent-runtime` entry carries
the fifth immutable digest. Old guards ignore the new top-level field and can
perform one safe transition while the seeded Agent flags remain off. On the
next update invocation, the newly installed scripts bind the fifth digest,
backfill secrets, and recreate the complete core service set even if the target
tag is unchanged. Operators must not enable Agent between those two passes.

All five final multi-platform image digests are keyless-signed. SPDX JSON SBOMs
are generated, attached as `spdxjson` attestations, verified in CI, and uploaded
with the GitHub Release. Formal release proof verifies signatures, SBOM
attestations, manifest digests, and stable aliases.

Runtime has no host port or mounts. It runs as non-root with a read-only root
filesystem, dropped capabilities, `no-new-privileges`, bounded `/tmp`, CPU and
memory limits, and only the backend network. Worker-advertised SSH SOCKS binds
to that private network; loopback proxy endpoints are rejected for a remote
Runtime.

## Rollback

New release proofs include Runtime Image ID and digest. Rolling back to a
pre-Agent release uses that release's three-service core set and stops the
orphan Runtime. Database downgrade remains prohibited; the normal Alembic
capability guard still decides whether application rollback is allowed.

## Environment-Dependent Verification

Docker DNS, read-only filesystem behavior, multi-architecture images, GHCR
signing/attestation, old-host two-pass upgrades, blue/green traffic, and live
proxy/provider behavior require CI or a Linux staging host. Unit and contract
tests verify the configuration and transition rules without claiming those
external systems were exercised locally.
