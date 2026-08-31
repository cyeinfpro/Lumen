# ADR 0001: Pi Agent Runtime and Provider Boundary

## Status

Accepted for the closed-by-default Agent rollout.

## Context

Lumen needs Pi's native model/tool loop without moving product state, provider
selection, credentials, billing, image generation, or recovery into Pi. The
Python Worker and the Node Runtime are separate containers, and existing Lumen
providers may require direct, HTTP-proxy, SOCKS5, or Worker-created SSH SOCKS
transport.

The implementation was developed against these installed versions:

- Node `22.22.0`
- `@earendil-works/pi-coding-agent` `0.84.4`
- `@earendil-works/pi-agent-core` `0.84.4`
- `@earendil-works/pi-ai` `0.84.4`
- `typebox` `1.3.7`

The Runtime package pins those versions exactly and commits its complete npm
lockfile. Pi `0.84.4` requires Node `>=22.19.0`.

## Decision

### State and resources

Each request restores one logical Pi session with
`SessionManager.inMemory(agent_session_id)`, `SettingsManager.inMemory()`, and a
hand-built `ResourceLoader` returning no extensions, skills, prompts, themes,
or context files. Lumen supplies sanitized retained history and the exact
system prompt from PostgreSQL; Pi session files are never a product data source.

When Pi's native `shouldCompact` threshold is reached before the current prompt,
the Runtime calls the public `session.compact()` API once. A split turn may use
at most two summary requests. The returned Pi summary, Lumen first-kept message
ID, the source run's user-message continuation boundary, usage, source
run/epoch/event sequence, and token boundary are persisted as a versioned ready
checkpoint on the source AgentRun. A later run restores it with `appendMessage()` plus `appendCompaction()` at the
original tree position, so messages after the checkpoint remain eligible for a
later native compaction. Runtime v2 also leaves automatic post-turn and
overflow-retry compaction enabled. Before a Pi retry, the negotiated
`text-reset-v1` event replaces the discarded streamed draft in Worker,
PostgreSQL, SSE, and Web. Paid image submission remains protected by semantic
idempotency and unknown results are never automatically resubmitted.

The Runtime uses `noTools: "builtin"`, an explicit tool allowlist, and checks
both active and configured tool names after session construction. Runtime v5
admits only five first-party custom tools: `lumen_create_image`,
`lumen_web_search`, `lumen_list_files`, `lumen_read_file`, and
`lumen_search_files`. It never enables Pi's host `read`, `write`, `edit`,
`bash`, package discovery, or third-party extension loading. Execution is
sequential at both agent and tool level.

### Service authentication and framing

Worker signs the raw request body with HMAC-SHA256 over:

```text
v1\nMETHOD\nPATH\nTIMESTAMP\nNONCE\nSHA256(body)
```

Runtime enforces clock skew, fixed-length constant-time signature comparison,
and a bounded one-use nonce cache before parsing credentials or writing NDJSON.
Request bodies and individual response lines have transport bounds. NDJSON has
no aggregate event/byte lifecycle cutoff; it accepts LF framing only, requires monotonic sequence
numbers and matching run/epoch identity, honors response backpressure, and
requires exactly one terminal event.

Runtime nonce state is process-local. Production deployment therefore keeps a
single Runtime replica for this phase. Worker also uses one execution epoch and
never retries a Runtime invocation after the request may have reached a
provider. Runtime v2 removes Lumen wall-clock, turn, text-size, and output-token
lifecycle budgets. A new Worker never downgrades v2 to an old Runtime contract;
an unsupported Runtime fails before provider dispatch. Deployment updates
Runtime before Worker, while the new Runtime accepts old Worker v1 requests and
ignores their lifecycle fields. Disconnects, 5xx responses, and any NDJSON
response never retry.
Horizontal Runtime replicas require a durable ticket-redemption service before
they are supported.

Nonce entries are retained for at least the complete accepted timestamp window
(`2 * clock_skew + 1` seconds), even when a shorter nonce TTL is configured.
Pre-auth request admission is acquired synchronously before body reads, and a
bounded body deadline closes stalled peers without opening an execution slot.

### Credentials and tools

Provider Pool or BYOK selection remains in Worker. BYOK is decrypted only in
Worker. Runtime receives one signed, run-scoped in-memory envelope containing
the selected adapter, model, endpoint, credential, bounded headers, verified
capabilities, and resolved proxy URL. It does not write auth or model files and
does not log the envelope.

For a direct BYOK endpoint, Worker also passes the bounded IP set that passed
the existing public-target validation. Runtime's Undici connector dials only
those addresses while preserving the original Host and TLS SNI; a redirect to
another host fails the pin. When a configured proxy is used, the existing proxy
trust path remains authoritative and no direct IP pin is sent.

Worker issues the existing HMAC capability token for the image Tool Gateway. It
binds run, user, session, epoch, tool, reference labels, capability ID, and
expiry. Runtime sends only tool ordinal, Pi call ID, epoch, and validated
business arguments. The API recomputes normalization and semantic idempotency
and remains the only process that creates Generation, wallet hold, and Outbox
rows.
Each token also carries a random nonce backed by an `agent_capability_grants`
row. API locks that grant with the run, validates every claim binding and
expiry, and consumes one bounded redemption only for a new tool ordinal. Exact
receipt replay does not consume another redemption. GPT Image 2 accepts at
most 16 selected `ref_N` inputs per call. The session catalog retains at most
64 owned, visible images with stable labels; ready images and queued/running
Generation reservations share that limit so asynchronous work cannot overbook
it. Ownership, readiness, deletion, and BYOK retention visibility are checked
again when Worker loads preview bytes and when the Tool Gateway redeems labels.

The web tool performs bounded read-only requests to fixed public search
endpoints, rejects redirects, credentials, non-HTTP(S) result URLs, oversized
responses, and control characters, and returns at most eight sanitized sources.
It cannot fetch an arbitrary model-supplied URL. File tools operate only on at
most eight text files embedded in the signed Runtime v5 request after API
validation. They accept exact virtual names and literal searches, never paths,
mounts, commands, or writes. File and search results are bounded before they
enter Pi history and are persisted as generic `AgentToolCall` receipts.

### Provider and proxy compatibility

The accepted adapter set is:

| Adapter | Image input metadata | Injected fetch | Runtime status |
| --- | --- | --- | --- |
| OpenAI Responses | supported by model declaration | supported | enabled |
| OpenAI Chat Completions | supported by model declaration | supported | enabled |
| Anthropic Messages | supported by model declaration | supported | enabled |
| Google Generative AI / Vertex | SDK supports image input | adapter rejects custom fetch | disabled |

Source inspection verified that Pi `0.84.4` forwards custom `fetch` through the
three enabled adapters and explicitly rejects it in the Google adapters. Google
is excluded instead of bypassing Lumen proxy policy.

Runtime uses Undici directly, `ProxyAgent` for HTTP(S), and
`Socks5ProxyAgent` for SOCKS5. For an SSH proxy, Worker opens the existing
strict-host-key-checked dynamic forward on a backend-network bind address and
advertises the Worker service hostname to Runtime. Direct loopback SOCKS is
rejected when Runtime is remote. The SSH SOCKS listener must remain reachable
only on the private backend network; it is not authenticated or published on a
host port.

### Recovery and billing

PostgreSQL is authoritative for execution epoch, text snapshots, event sequence,
tool receipts, and terminal state. Text flushes lock AgentRun then Message,
merge only the text projection, increment the durable event sequence, and stage
SSE atomically. The Tool Gateway uses the same lock order.

A recovered `running` run is replayable only when persisted evidence proves the
Runtime request did not start. Once delivery is `starting` or later, recovery
does not invoke Pi again. Existing Generation metadata repairs nonterminal tool
receipts; otherwise the tool becomes `tool_result_unknown`. A successful image
side effect plus parent failure yields `partial`.

Text usage aggregates Pi turn usage and pre-prompt Pi compaction usage. A
compaction checkpoint advances durable provider-completion evidence only after
Pi returns canonical usage for all one or two underlying summary requests.
Exact usage settles the Agent text hold from its pinned pricing snapshot, which
reserves the bounded summary-call budget. Proven pre-dispatch absence releases
it. A post-dispatch result with unknown usage settles the reserved hold
conservatively. Image tool costs remain independent Generation settlements.
Reasoning and one-hour cache-write breakdowns are preserved. Terminal usage
bounds include the two reserved native-compaction calls. Runtime rejects usage
outside the selected model context/output bounds, Worker retains
monotonic completed-turn totals, and settlement falls back to the reserved hold
if reported usage exceeds the reservation snapshot.

## Verified Locally

The automated faux-provider suite verifies:

- `text -> lumen_create_image -> text` ordering through Pi's real agent loop;
- virtual file list/search/read ordering and bounded persisted results;
- fixed-provider web-search normalization and unavailable-provider failure;
- no built-in tools or discovered resources;
- disabled tools are absent from the Pi session;
- a model-emitted `bash` call cannot execute;
- in-memory sessions have no session file;
- HMAC tamper, expiry, and nonce replay rejection;
- strict request contracts and secret redaction;
- monotonic bounded NDJSON with one terminal event;
- HTTP boundary authentication and replay rejection;
- Node type-check, lint, build, and paid-call-free readiness initialization.

These checks use Pi's bundled faux provider and make no external provider call.

## Environment-Dependent Gates

The executable opt-in harness is:

```bash
cd apps/agent-runtime
AGENT_LIVE_CHECK=1 \
AGENT_LIVE_API=openai-responses \
AGENT_LIVE_BASE_URL=https://provider.example/v1 \
AGENT_LIVE_MODEL=model-id \
AGENT_LIVE_API_KEY=... \
npm run live:provider
```

Set `AGENT_LIVE_SCENARIO` to `text`, `vision`, `tool`, `reasoning`, `abort`,
`error-429`, `error-5xx`, or `truncated`. Vision additionally requires a PNG at
`AGENT_LIVE_REFERENCE_IMAGE`; tool requires a real run-scoped
`AGENT_LIVE_TOOL_GATEWAY_URL` and `AGENT_LIVE_TOOL_CAPABILITY`. Proxy coverage
uses the same scenario with `AGENT_LIVE_PROXY_URL` set separately for direct,
HTTP, SOCKS5, and Worker-advertised SSH SOCKS paths. Error scenarios require an
opt-in fixture endpoint that deterministically emits the named response; a
normal paid provider is not assumed to manufacture errors.

It is disabled unless `AGENT_LIVE_CHECK=1` because it can incur cost. Real
provider text/vision/tool/usage/reasoning behavior, abort, 429/5xx, truncated
streams, HTTP/SOCKS/SSH proxies, Linux amd64/arm64 images, Docker DNS/read-only
runtime, and cross-container SSH reachability are environment-dependent and are
not claimed as verified by local faux tests.

## Consequences

Agent remains closed by default until the Runtime service and Worker secrets are
deployed. Existing Studio, Generation, Provider Pool, wallet, and memory paths
remain authoritative. Adding another adapter or tool requires an explicit
capability, transport, idempotency, billing, cancellation, and unknown-result
contract plus focused tests.
