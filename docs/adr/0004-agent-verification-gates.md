# ADR 0004: Agent Verification Gates and Evidence Boundaries

## Status

Accepted for remediation and the closed-by-default rollout.

## Decision

Agent verification is split into deterministic local gates and explicit live
gates. Mocked browser tests are UI evidence only. They are never reported as
Provider, PostgreSQL, Redis, container-network, proxy, or physical-keyboard
evidence.

## Deterministic Gates

The normal repository gates exercise Core/API/Worker state and billing,
Runtime Pi faux-provider isolation, Web state/reconciliation, migration shape,
and mocked browser geometry. Runtime tests include nonce replay after nominal
TTL, pre-auth slow-body admission, direct unpinned redirects, parsed Gateway
5xx unknown results, detailed usage, impossible usage, post-turn exceptions,
unknown-result submission freezing, and image limits.
They also prove Runtime v2 has no Lumen lifecycle budget, Pi recoverable-length
compaction emits `text.reset` before regeneration, and NDJSON has no aggregate
event/byte cutoff.

These tests make no paid call and do not require Docker, PostgreSQL, or Redis.

## Live Provider Matrix

`apps/agent-runtime/scripts/live-provider-check.ts` is fail-closed unless
`AGENT_LIVE_CHECK=1`. Run each scenario separately for every enabled adapter:

```text
text / vision / tool / reasoning / abort
error-429 / error-5xx / truncated
direct / HTTP proxy / SOCKS5 proxy / Worker SSH-SOCKS
```

The command and scenario-specific prerequisites are documented in ADR 0001.
Output is machine-readable JSON and includes outcome, error code, turn/tool
counts, detailed usage, Provider statuses, and text event count. A missing
reasoning breakdown fails the reasoning scenario; it is not silently accepted.
The `truncated` scenario follows Pi semantics: a provider-native `length` stop is
a settled assistant turn, while a recoverable context-clamped length may compact,
reset the discarded draft, and retry once.

## Full-Stack Matrix

Run:

```bash
cd apps/web
AGENT_FULL_STACK_E2E=1 \
PLAYWRIGHT_BASE_URL=https://staging.example \
AGENT_E2E_CONTROL_URL=https://fixture-controller.example \
AGENT_E2E_CONTROL_TOKEN=... \
AGENT_E2E_USER_A_EMAIL=... AGENT_E2E_USER_A_PASSWORD=... \
AGENT_E2E_USER_B_EMAIL=... AGENT_E2E_USER_B_PASSWORD=... \
AGENT_E2E_REFERENCE_IMAGE=/absolute/path/reference.png \
npm run test:e2e:fullstack
```

The controller is staging-only and must expose authenticated
`POST /scenario` with `{name,test_id}`. It resets isolated test users and
configures deterministic fake chat/image Provider behavior without intercepting
Lumen HTTP calls. The Playwright file contains no `page.route` handlers and
covers the ten plan scenarios: text, T2I, one-reference I2I, ordered multi-ref,
refresh recovery, pre-tool disconnect, post-tool disconnect, cancellation,
preflight failures, and two-user isolation. Balance, BYOK, and vision preflight
are three independent scenarios with exact expected errors. Two-user isolation
attaches a user-A reference, rejects user-B run and image reads, and checks that
the public response contains no credential or capability fields.

`run-agent-fullstack-e2e.mjs` exits `2` when opt-in, controller, credentials, or
reference media are absent. A skipped default run is not a pass for this gate.

## Infrastructure Evidence

On a Linux staging host with Docker Compose v2, run immutable-image config,
build/start, Runtime `/readyz`, read-only root, writable bounded `/tmp`, backend
DNS, and no-published-port checks. PostgreSQL and Redis concurrency suites
require `LUMEN_TEST_POSTGRES_URL` and `LUMEN_TEST_REDIS_URL`. Release CI remains
the authority for amd64/arm64 manifests, signatures, and SPDX attestations.

When PostgreSQL is available,
`apps/api/tests/test_agent_postgres_integration.py` creates an isolated schema
and runs production service code for concurrent duplicate messages, billed
tool replay, and both cancel/tool lock orderings. These tests skip explicitly
without a PostgreSQL URL; SQLite tests are not presented as row-lock evidence.

Physical iOS/Android software-keyboard and safe-area behavior remains a manual
device gate. Browser viewport screenshots cannot prove it. Phase 5 enablement
therefore requires recorded staging results for the live Provider matrix,
full-stack matrix, Compose checks, and device keyboard check before changing
either closed-by-default setting.
