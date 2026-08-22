# Lumen Agent Runtime

Private Node service that executes one bounded Pi agent run for the Python
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
backend-only `POST /v1/runs` NDJSON endpoint. It must not be published on a host
port or mounted to a repository, media directory, user home, or Pi config path.

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
and the separate no-interception full-stack browser harness.

Production decisions are recorded in `docs/adr/0001-agent-runtime-pi-provider-boundary.md`
and `docs/adr/0003-agent-deployment-release-compatibility.md`.
