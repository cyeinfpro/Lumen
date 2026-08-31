# Lumen Agent Runtime

Private Node service that executes one Pi-native agent run for the Python
Worker. PostgreSQL remains the product state source; this service uses only
in-memory Pi sessions, settings, credentials, and model catalogs.

## Local gates

```bash
npm ci --ignore-scripts
npm test
npm run type-check
npm run lint
npm run build
```

The service requires `AGENT_RUNTIME_SHARED_SECRET` with at least 32 UTF-8 bytes.
It exposes `GET /healthz`, `GET /readyz`, `GET /metrics`, and the authenticated
backend-only `POST /v1/runs` NDJSON endpoint. Runtime accepts legacy v1/v2 and
receiver-first v3 requests. New API snapshots opt into v3; queued legacy rows
remain v2 so rolling upgrades do not add fields to a strict old receiver. Pi's
`session.prompt()` / `agent_end` lifecycle, provider-native model metadata, and
explicit user cancellation are authoritative during normal execution. The
server also enforces non-configurable-upward accident ceilings for wall clock,
turns, Provider dispatches, usage, event bytes, and repeated tool signatures.
Trips preserve known usage, text, and accepted side effects as a failed or
partial `agent_safety_budget_reached` result. Pi's supported
`httpIdleTimeoutMs: 0` setting disables the SDK request deadline. While a run is active it emits a
`run.heartbeat` event every `AGENT_RUNTIME_HEARTBEAT_INTERVAL_SECONDS`
(default 15 seconds), including during provider silence and context preparation.
Heartbeats are enabled only when the Worker advertises `heartbeat-v1`. A v2
Worker also advertises `text-reset-v1` for provider retry resets. Only explicit
pre-prompt Pi compaction checkpoints are persisted; current-turn automatic
compaction is disabled until every retained Pi entry has a durable projection.
It must
not be published on a host port or mounted to a repository, media
directory, user home, or Pi config path. Pi built-ins and resource discovery
remain disabled. Runtime v5 exposes only Lumen's first-party image, bounded
public web-search, and in-memory virtual text-file tools. Web search calls fixed
public endpoints and cannot fetch a model-supplied URL; file tools cannot read
or write a host/container path.

Production Compose keeps one always-running replica on `lumen_backend` with a
read-only root filesystem, bounded `/tmp`, non-root UID, dropped capabilities,
and `no-new-privileges`. `/healthz` is liveness. `/readyz` initializes Pi
isolation and validates auth without a Provider or paid model call. API requires
it only while authoritative `agent.enabled=1`.

`/metrics` and structured logs are backend-only. Prompts, message content,
credentials, capabilities, headers, and image data are scrubbed. Worker owns the
OTEL span joining Runtime events, tool receipts, and Generation identifiers.

`npm run live:provider` is disabled unless `AGENT_LIVE_CHECK=1`. The live harness
uses billable credentials and is intentionally separate from normal tests.
`AGENT_LIVE_SCENARIO` selects `text`, `vision`, `tool`, `reasoning`, `abort`,
`error-429`, `error-5xx`, or `truncated`; see ADR 0004 for the required matrix
and the separate no-interception full-stack browser harness. To include the v3
Provider permit boundary, set `AGENT_LIVE_PROVIDER_DISPATCH_URL` and
`AGENT_LIVE_PROVIDER_DISPATCH_CAPABILITY` together for the same run and epoch.

Production decisions are recorded in `docs/adr/0001-agent-runtime-pi-provider-boundary.md`
and `docs/adr/0003-agent-deployment-release-compatibility.md`.
