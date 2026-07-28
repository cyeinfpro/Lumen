# Lumen Deep Optimization Execution Ledger

Source plan:
`Lumen-deep-optimization-plan-2026-07-28.md`

This file records implementation status and evidence. It does not replace or
reinterpret the source plan.

## V2 Continuation Baseline

- Date: 2026-07-28
- Source plan:
  `Lumen-deep-optimization-v2-current-main-2026-07-28.md`
- Target: `origin/main`
- WORK_BASE_SHA: `d2b8c259d52edc0a839b695fecc7b7ab2029bbe3`
- WORK_BASE_TAG: `v1.2.81`
- Integration branch: `codex/lumen-deep-optimization-v2-20260728`
- Initial worktree: clean
- Frozen-plan delta: none; `WORK_BASE_SHA` equals the V2 audit freeze point
- Prior ledger status: F-01 through F-13 and prior Wave 0 through Wave 4
  remain completed and are not being reimplemented

### Branch Consolidation

- `main` was fast-forwarded from `e28346544ce44ddf0598cb195dd15db22573c8f2`
  to `origin/main` at `WORK_BASE_SHA`.
- Every previous local optimization branch was checked with
  `git cherry main <branch>`; all commits were patch-equivalent to `main` or
  the branch already pointed at an ancestor/current `main`.
- Five associated worktrees were clean and removed.
- Eighteen absorbed local branches were deleted.
- The absorbed remote branch `codex/desktop-mac-win-full-smoke` was deleted.
- After cleanup, the only persistent branches are `main`, `origin/main`, and
  the current V2 integration branch.

### V2 Wave 0 Completion

- Existing impact planner tests:
  `uv run pytest -q tests/test_test_impact.py tests/test_run_test_plan.py`
  -> 6 passed.
- Existing static gates:
  `uv run ruff check scripts tests` -> passed.
- Existing backend governance:
  architecture passed with 3 runtime-coupling findings; complexity passed
  with 11 findings; runtime-state passed with 15 mutable instances across 14
  modules.
- Existing Web runner syntax:
  `cd apps/web && node --check scripts/run-tests.mjs` -> passed.
- Confirmed Asset baseline gaps at `WORK_BASE_SHA`: search is limited to
  loaded pages; grid candidates can fall back to `/binary`; a failed source
  does not remove the same URL from static `srcSet`; mounted tiles grow with
  loaded pages; image prewarm has no queue-length bound, cancellation, or
  timeout.
- The first parallel Wave 0 command batch hit the host file-descriptor limit
  (`Too many open files`, soft limit 256) before product commands started.
  Commands were rerun serially and passed; this is recorded as an environment
  constraint, not a product failure.

#### Commits

- A9 manifest linter:
  `7662f25` (`test: add impact manifest linter`).
- A0 shared manifest/CI mapping:
  `acda552` (`test: map v2 optimization domains`).
- A8 perf/fault harness:
  `66d9593` (`test(perf): add wave 0 baseline harness`).

#### Agent Evidence

- A1 confirmed RT-01/02/03 at `WORK_BASE_SHA`: task+user live delivery emits
  two frames for one `sse_id`; the API non-Lua fallback can produce two Stream
  entries for one stable event ID; durable/user/compat fanout outcomes are not
  independently observable.
- A3 confirmed GEN-01/02/03/04/06 at `WORK_BASE_SHA`: enqueue-result-unknown
  immediately retries; selecting 10 jobs from 1000 candidates performs 1000
  per-candidate Redis reads; provider selection still probes with `TypeError`;
  admission remains count-only; 100 wake hints cause 100 scheduler scans.
- A5 confirmed the Asset gaps listed above from current source paths. This was
  a read-only Lead audit; no Wave 3 implementation was pulled forward.
- A8 produced `perf/wave0/` and
  `docs/perf/lumen-wave0-baseline-2026-07-28.{md,json}` without changing
  product code.
- A9 produced a read-only manifest audit and linter. A0 retained sole ownership
  of `scripts/test-manifest.toml` and `.github/workflows/ci.yml`.

#### Metrics

- Manifest before -> after:
  stale `2 -> 0`, unmatched `2 -> 0`, critical fallback-only `47 -> 0`,
  shadowed `0 -> 0`; 18 intentional full-mandatory patterns remain explained.
- Realtime baseline: same live event on task+user channels -> 2 frames;
  replay-seen live event -> 0 frames.
- Queue baseline:
  10/100/1000 candidates -> 29/119/1019 Redis commands, with 8 enqueues.
- Synthetic RSS:
  1MP 52.6 MiB, 4K 159.1 MiB, edit 144.2 MiB, dual race 192.4 MiB.
- Browser contract fixture:
  1000 mounted tiles, 3009 DOM elements, 1013 requests, 0 `/binary` requests,
  20 thumb-404-to-preview fallbacks.
- Authenticated feed/search remained explicitly `gated`; the harness requires
  a representative authenticated API dataset before claiming DB latency.

#### Verification

- `uv run pytest -q tests/test_test_manifest_lint.py
  tests/test_test_impact.py tests/test_run_test_plan.py` -> 16 passed.
- `uv run pytest -q perf/wave0/test_run.py` -> 4 passed.
- Manifest linter -> passed for 1218 production files.
- Changed-file Ruff and Ruff format -> passed.
- Backend architecture, complexity, and runtime-state gates -> passed at
  `3 / 11 / 15`.
- Web runner and browser harness Node syntax -> passed.
- CI YAML parse and `git diff --check` -> passed.
- No full API/Worker/Web suite or repository-wide gate was run in Wave 0.

#### Invariants And Rollback

- No business behavior, database schema, queue lease, billing, artifact,
  cache, delivery, or BYOK retention logic changed.
- Shared Core, lockfiles, migrations, CI workflow changes, and test
  infrastructure remain full-mandatory.
- Rollback the three Wave 0 commits in reverse order. Removing the independent
  harness requires no data migration or feature flag.

### V2 Wave 1 Completion

#### Commits

- Connection-level replay/live/live dedupe:
  `3f95c6f` (`fix(realtime): dedupe connection events`).
- Shared durable append and typed fanout outcomes:
  `7404e97` (`fix(realtime): unify durable fanout semantics`).
- Web progress coalescing:
  `89cf2ae` (`perf(web): coalesce realtime progress`).

#### Behavior And Invariants

- Replay, live task/compat, live user, and compaction events share one bounded
  per-connection deduper keyed by recoverable `sse_id` and stable `event_id`.
- The deduper stores at most 4096 keys, evicts oldest keys, and is owned by the
  connection state; no process-wide mutable global was added.
- Progress and terminal events with distinct identities are never merged.
- API and Worker runtime append paths now use
  `lumen_core.sse_durable` for owner-token reservation, bounded wait, stream
  recovery, stale compare-delete, and transactional `XADD + EXPIRE`.
- Lua remains the normal fast path. The non-Lua path requires a transactional
  Redis pipeline and no longer deletes an in-flight reservation immediately.
- Durable append success defines publication success. User and compat live
  fanout run independently, user first; a live failure is logged/measured and
  does not reverse durable success.
- Web coalesces only `generation.progress` and `completion.progress`, latest
  per task, at a 100 ms interval. Delta/thinking-delta events remain
  uncoalesced. Terminal/state barriers discard pending progress for the same
  task and dispatch immediately.

#### Metrics

- Same `sse_id` on task+user live channels:
  `2 frames -> 1 frame`.
- Concurrent non-Lua append for the same stable `event_id`:
  `2 Stream entries -> 1 Stream entry`.
- Accepted `EXEC` with lost response:
  retry recovers the existing Stream ID and keeps one entry.
- Live fanout order:
  `compat,user -> user,compat`.
- Live outcome, bytes, and duration are exposed as:
  `lumen_sse_live_publish_total`,
  `lumen_sse_live_publish_bytes_total`, and
  `lumen_sse_live_publish_duration_seconds`.
- Web progress application is bounded to at most 10 scheduled flushes per
  second per runtime, with one latest event per active task in each flush.

#### Verification

- Backend realtime/domain batch, including shared durable and API publisher
  contracts: 133 passed, 1 unrelated node deselected.
- Focused durable/fanout/route contract set after failure fixes:
  36 passed.
- Web realtime batch, including the new progress coalescer:
  24 passed.
- Web type-check, architecture, and complexity gates passed.
- Changed-file Ruff and Ruff format passed.
- Backend architecture, complexity, and runtime-state gates passed at
  `3 / 11 / 15`; no baseline was raised.
- Manifest linter passed with stale/unmatched/critical-fallback/shadowed all
  zero.
- No full repository invocation was run. The shared Core change remains
  full-mandatory for the single final Wave 5 gate.

#### Risks And Rollback

- The fallback requires WATCH/MULTI support when Lua cannot execute XADD. A
  deployment lacking both capabilities must fail durable publication and
  retry the source event rather than emit an unrecoverable live event.
- A connection may see a duplicate again after the bounded dedupe window is
  evicted; the downstream store still retains stable event identity.
- Rollback Web coalescing independently with `89cf2ae`.
- Rollback backend fanout/durable behavior with `7404e97`, then connection
  dedupe with `3f95c6f`. Do not roll back the Redis Stream itself.

## Baseline

- Date: 2026-07-28
- Target: `origin/main`
- Baseline SHA: `e28346544ce44ddf0598cb195dd15db22573c8f2`
- Baseline tag: `v1.2.76`
- Integration branch: `codex/lumen-deep-optimization-20260728`
- Initial worktree: clean
- Baseline GitHub CI: success, run `30333783342`
- Baseline Docker Release: success, run `30333783345`

## Baseline Gates

- Backend architecture: passed; 3 reported runtime-coupling findings
- Backend complexity: passed; 33 multi-dimensional findings
- Module runtime state: passed; 21 instances across 20 modules
- Web architecture: passed; 550 files, 2105 internal edges
- Web complexity: passed; 0 findings
- Web UI governance: passed; 0 findings

## Baseline Debt

| Metric | Baseline |
| --- | ---: |
| Functions over 200 lines | 5 |
| Functions over parameter threshold | 22 |
| Functions over nesting threshold | 6 |
| Mutable module runtime instances | 21 |
| Active compatibility facades | 10 |

## Finding Verification

All findings were rechecked against the baseline SHA before implementation.

| ID | Initial status | Current evidence |
| --- | --- | --- |
| F-01 | Fixed | Canvas now serializes `(namespace, user_id, idempotency_key)` with a PostgreSQL transaction advisory lock and recovers the unique-constraint winner without leaking `IntegrityError`. |
| F-02 | Fixed | Hard-link and copy fallback paths resolve identical concurrent winners through one identity check while preserving `409 storage_conflict` for mismatched content. |
| F-03 | Fixed | Disappeared metadata is treated as a tombstone, counted as scanned, and no longer stalls cursor progress. |
| F-04 | Fixed | Copy fallback keeps explicit fd ownership and closes/unlinks the partial destination on source-open failure. |
| F-05 | Fixed | Sweep failures now expose stable error codes, structured context, counters, and remain isolated from completed row reconciliation. |
| F-06 | Phase 1 fixed | Poster tagging sends a bounded preview, reuses lifecycle-owned HTTP clients, and uses a Redis TTL/owner-token capacity lease; durable worker/outbox migration remains for a later wave. |
| F-07 | Fixed | `useSSE` registers only real recovery adapters while retaining the rule that every registered adapter must succeed. |
| F-08 | Fixed | Auth response snapshots are captured before commit and runtime-default/audit enrichment failures fall back without changing committed success semantics. |
| F-09 | Fixed | Completion now exposes typed command/result and seven behavioral services; public runtime/contracts contain no `Any`, ORM/SQL/private helpers or RuntimeSlot, phase state is explicit, and capability imports fail deterministically. |
| F-10 | Fixed | Outbox rows are claimed in a short owner/TTL transaction; Redis/SSE delivery runs after commit with bounded concurrency, owner-checked finalization, stable job IDs, retries, and DLQ handling. |
| F-11 | Fixed | Message and task post-commit publishers start together under one total latency budget; failures are isolated and pending publishers are cancelled after the shared deadline. |
| F-12 | Fixed | Video submission computes the idempotency fingerprint first, checks the winner before expensive work, then serializes and rechecks under a transaction advisory lock before provider/media/pricing work. |
| F-13 | Fixed | Artifact staging uses one lifecycle-owned blocking writer with a bounded byte queue; capacity is initially bounded and atomically resized to measured staged/output demand. |

## Wave Checklist

### Wave 0

- [x] Impact test manifest
- [x] Impact planner with reverse dependencies and full-gate escalation
- [x] Resource-aware test plan runner
- [x] Targeted Web test runner
- [x] Lead review
- [x] Wave impact plan passed

### Wave 1

- [x] Image F-02/F-03/F-04/F-05
- [x] Canvas/Auth F-01/F-08
- [x] Poster F-06 phase 1
- [x] Web SSE F-07
- [x] Lead review and ownership audit
- [x] Wave impact plan passed

### Wave 2

- [x] Outbox F-10
- [x] Message/Video F-11/F-12
- [x] Upload F-13
- [x] CI impact batching
- [x] Lead review and ownership audit
- [x] Wave impact plan passed

### Wave 3

- [x] Workflow typed vertical slices
- [x] Completion runtime C1/C2/C3
- [x] API/Worker runtime container
- [x] Web feature boundary gate
- [x] Lead review and ownership audit
- [x] Wave impact plan passed

### Wave 4

- [x] Compatibility facades retired
- [x] Complexity baseline lowered
- [x] Runtime state baseline lowered
- [x] Public port type audit passed
- [x] Final impact plan passed
- [x] Final local gate completed with one full invocation and failed-node-only fixes
- [ ] GitHub Actions passed

## Agent Handoffs

### A10: Impact Test Planner / CI Test Batching

- Baseline SHA: `e28346544ce44ddf0598cb195dd15db22573c8f2`
- Commit SHA: `2ee6938edc6e5e1bf35d76fbdea3c35f35fc4b63`
- Changed files: impact manifest/planner/runner, targeted Web runner, and
  direct tests
- Invariants: no CI, baseline, composition, shared model, or business changes
- Targeted tests: Python 6 passed; Web 4 passed
- Static gates: Ruff, Node syntax, Web architecture, Web complexity,
  TypeScript type-check, and diff check passed
- Not run: full suite, build, and business suites
- Metrics: per-command stable ID, duration, exit code, failures, output, and
  result JSON
- Rollback: revert the single A10 commit
- Remaining risk: process-group Ctrl-C cleanup has unit coverage but no live
  long-running interruption integration test

### Wave 0 Lead Evidence

- Plan: `/tmp/lumen-wave0-plan.json`
- Results: `/tmp/lumen-wave0-results.json`
- Changed files: 7
- Matched rules: `test-infrastructure`
- Commands: 4
- Result: all passed
- Full mandatory: true because test infrastructure changed; deferred to the
  single final full gate required by the source plan

### A1: Image Store / Reconcile

- Baseline SHA: `e28346544ce44ddf0598cb195dd15db22573c8f2`
- Commits: `e9361b71c50716d7b38fc72ced1777abdbc26c94`,
  `6c9b7babedc974829cd95f8e5056c0665f5c0c5b`
- Changed files: filesystem artifact store, reconcile policy, image metrics,
  and targeted image tests
- Invariants: identical concurrent publishes are idempotent; conflicting
  content remains a `FileExistsError`-compatible `409`; directory fsync,
  identity verification, cursor progress, and lease-loss behavior remain
  intact
- Targeted tests: 42 passed
- Static gates: Ruff, architecture, complexity, and runtime-state passed
- Metrics: publish conflicts/winners and staged sweep failures/tombstones
- Rollback: revert the two commits in reverse order
- Remaining risk: production NFS/CIFS lock semantics need environment proof;
  repeated sweep failure still needs runtime health degradation ownership

### A2: Canvas Idempotency / Auth

- Baseline SHA: `e28346544ce44ddf0598cb195dd15db22573c8f2`
- Commits: `77366e992f874513d832231963f2231b13f1799a`,
  `2867d31c7d61b4d34e2bd4dafff3c81287c6cf18`
- Changed files: Canvas execution, shared advisory-lock primitive, auth route,
  and targeted Canvas/Auth tests
- Invariants: database uniqueness remains the second defense; fingerprint
  conflicts remain structured `409`; active-node exclusion is unchanged;
  post-commit non-critical failures cannot change committed auth success
- Targeted tests: 85 passed
- Static gates: Ruff, architecture, complexity, and runtime-state passed
- Integration probe: two PostgreSQL sessions serialized on the advisory lock
- Rollback: revert the two commits in reverse order
- Remaining risk: the live two-session probe is not yet a repository CI node

### A3: Poster Tagging

- Baseline SHA: `e28346544ce44ddf0598cb195dd15db22573c8f2`
- Commit: `efc7c8796f88debf17fc038f54e85e7d48f505c6`
- Changed files: Poster route/resources/tagging, lifecycle client pool,
  Redis capacity lease, and targeted tests
- Invariants: no synchronous original-file read in async code; provider sees
  only bounded preview bytes; one BackgroundTask path remains; route mutable
  semaphore state is removed
- Targeted tests: 75 passed
- Static gates: Ruff, architecture, complexity passed
- Runtime-state: reduced from 21 to 20; shared ledger lowered by Lead
- Rollback: revert the single commit
- Remaining risk: durable worker/outbox execution, signed preview URLs, and
  production Redis capacity validation remain

### A4: Web SSE Recovery

- Baseline SHA: `e28346544ce44ddf0598cb195dd15db22573c8f2`
- Commits: `c913fe60e33c7ae22185282d3475f8cfa2944609`,
  `6dcf2b181103820760f9f335137f45ffcb0baea1`
- Changed files: `useSSE` and realtime runtime tests
- Invariants: passive subscribers do not register fake recovery adapters;
  every real adapter must still succeed
- Targeted tests: 20 passed
- Static gates: Web architecture, complexity, and type-check passed
- Rollback: revert the two commits in reverse order
- Remaining risk: effect dependency is source-contract tested rather than
  mounted through a DOM Hook lifecycle test

### Wave 1 Lead Evidence

- Plan: `/tmp/lumen-wave1-plan.json`
- Results: `/tmp/lumen-wave1-results.json`
- Changed files: 21 before the Lead ledger update
- Matched rules: Canvas/Auth, Image, Poster, Upload reverse dependencies,
  runtime container reverse dependencies, Web realtime, and conservative API/
  Web fallbacks
- Commands: 15
- Initial result: 14 passed; runtime-state correctly rejected a stale
  `21 -> 20` baseline
- Failed-only rerun: runtime-state passed after removing the retired Poster
  semaphore entry and lowering `max_total` to 20
- Test evidence: API 1522 passed with 2 existing skips; Image 42; Canvas/Auth
  85; Upload 47; runtime lifecycle 27; Poster 68; Web 475; SSE 20
- Final Wave result: all selected gates and tests passed

### A5: Durable Outbox Claims

- Baseline SHA: `1eef268a5042fad652de24bd1aae5be8a408737f`
- Commit SHA: `9b90aeb78dcfee8548d102ffc9654b8e8cb229c3`
  (integrated as `61757cc4fcf2fccc9e6149f7ad81fb62a95dec82`)
- Changed files: outbox claim/publisher/contracts/tasks, media workflow model,
  expand-only migration `0050`, and targeted Worker tests
- Invariants: at-least-once delivery retains stable event/job identity;
  external delivery does not run inside the row-lock transaction; only the
  current claim owner can finalize a row; retry and DLQ semantics remain
  explicit
- Confirmed defect: the prior publisher held `FOR UPDATE SKIP LOCKED` rows
  while performing Redis/SSE delivery
- Implementation: short owner/TTL claims, bounded concurrent delivery outside
  the transaction, owner-checked finalize, retry diagnostics, and stale-claim
  recovery
- Targeted tests: 57 passed; 3 PostgreSQL environment-conditional tests skipped
  in the Lead worktree; the Agent's PostgreSQL probe passed 3 tests
- Static gates: Ruff, format, architecture, complexity, runtime-state, and
  migration lint passed
- Not run: full repository suite
- Metrics/logging: claim attempts, delivery attempts/errors, retry and DLQ
  context use stable outbox identifiers and claim owners
- Migration/rollback: additive nullable claim columns, non-negative attempt
  check, and partial claim index; rollback drops only the new index/columns
- Remaining risk: production broker latency and crash recovery require runtime
  observation after release

### A6: Message / Video Submission

- Baseline SHA: `1eef268a5042fad652de24bd1aae5be8a408737f`
- Commit SHA: `3e11376`
- Changed files: message/regenerate routes, video submission service, targeted
  route/video tests, and one deleted complexity baseline entry
- Invariants: committed message/regeneration success is not reversed by
  non-critical publishing; video unique-key conflicts retain structured `409`;
  deferred commit does not emit an empty Canvas publish payload
- Confirmed defects: publishers consumed serial two-second budgets and video
  submission performed expensive external work before winner detection
- Implementation: one shared publish deadline with concurrent fast paths;
  early fingerprint/winner lookup plus transaction advisory lock and recheck;
  video preparation and construction split into typed internal plans
- Targeted tests: 171 passed; the one intentionally updated source-contract
  node passed on failed-node-only rerun
- Static gates: Ruff, format, architecture, complexity, and runtime-state passed
- Not run: full repository suite
- Metrics/logging: per-publisher failures retain structured route logging;
  timeout cancellation is bounded by the shared deadline
- Rollback: revert `3e11376`
- Remaining risk: provider work is serialized per idempotency key, so production
  lock wait and provider latency need observation

### A1: Upload Writer / Capacity Lease

- Baseline SHA: `1eef268a5042fad652de24bd1aae5be8a408737f`
- Commit SHA: `444e85e`
- Changed files: filesystem writer/store, upload application and metrics,
  storage capacity ports/adapters, and targeted image tests
- Invariants: staged-file hashing, fsync, abort cleanup, and artifact identity
  remain intact; one lease is atomically resized instead of stacking
  reservations
- Confirmed defects: every input chunk used `asyncio.to_thread`, and the
  reservation was always `max_bytes * 5`
- Implementation: one blocking writer lifecycle with a 2 MiB byte-bounded
  queue; measured resize after inspection; Redis and file capacity adapters
  implement owner-safe resize
- Targeted tests: 67 passed initially with four fake-lease failures; only the
  four failed node IDs were rerun after adding the port method and passed; the
  writer node passed after the file split
- Static gates: Ruff, format, architecture, complexity, and runtime-state passed
- Not run: full repository suite
- Metrics/logging: upload bytes, writer queue wait/duration, and capacity
  reservation ratio histograms
- Rollback: revert `444e85e`
- Remaining risk: queue sizing and reservation ratios require production
  telemetry calibration

### A10 / Lead: CI Impact Batching

- Baseline SHA: `1eef268a5042fad652de24bd1aae5be8a408737f`
- Commit SHA: `03db73d`
- Changed files: `.github/workflows/ci.yml`
- Invariants: static gates remain always-run; main and full-mandatory changes
  retain the existing full backend/frontend suites; empty unexplained plans
  fail closed
- Confirmed risk: CI ignored the impact plan and always executed sequential
  full test jobs
- Implementation: one plan job resolves the comparison base, emits backend and
  frontend command matrices, uploads a pinned artifact, and conditionally runs
  impacted tests/builds while preserving existing job names
- Tests: workflow YAML parsed; real plan split produced 7 backend and 3 frontend
  commands; `actionlint v1.7.7` passed
- Static gates: shell lint through actionlint and `git diff --check` passed
- Not run: GitHub-hosted workflow, deferred to post-push Actions
- Metrics/logging: plan artifact records selected rules, reasons, commands, and
  full-gate escalation
- Rollback: revert `03db73d`
- Remaining risk: GitHub artifact path/expression behavior needs the required
  live Actions proof

### Wave 2 Lead Evidence

- Plan: `/tmp/lumen-wave2-plan.json`
- Results: `/tmp/lumen-wave2-results.json`
- Base: `1eef268a5042fad652de24bd1aae5be8a408737f`
- Changed files: 25
- Matched rules: 12, including Outbox, Message/Video, Upload, migrations,
  shared core, and conservative API/Worker reverse dependencies
- Commands: 19
- Full mandatory: true because CI, migration, and shared core changed; the
  source plan still reserves the single repository-wide gate for Wave 4
- Result: 19 passed, 0 failed; no failed-node rerun required
- Test evidence: API 1528 passed with 2 existing skips; Worker 1547 passed
  with 6 existing skips; Core 561 passed; Image 43 and Upload 50 passed;
  Message/Video 160 passed; Outbox 57 passed with 3 environment-conditional
  PostgreSQL skips; migration set 22 passed with 1 existing skip
- Governance: complexity debt reduced from 33 to 31; runtime-state remained
  20 instances across 19 modules; no baseline expansion
- Final Wave result: all selected gates and tests passed

### A7: Workflow Typed Vertical Slices

- Baseline SHA: `b17ed266b167c7b4b78149ad4ca87550f62f30ca`
- Commits: `5afb3800`, `33108da5`, `127b46d6`, `b5ae3723`,
  `c367cc97`, `a1000641`, `d890105f`
- Changed files: 24 Workflow route/application/port/adapter/transport files
  and directly corresponding tests; Lead-owned composition remained untouched
- Invariants: migrated routes construct typed commands/queries; migrated
  application modules import neither FastAPI nor SQLAlchemy; migrated public
  ports contain no opaque transport/ORM types; project cleanup stays
  post-commit
- Confirmed risk: list/upsert/create/cancel routes crossed a generic HTTP
  facade carrying request/session/user objects
- Implementation: independently revertible list-runs, project-upsert,
  run-creation, and project-cancellation slices plus stateless slice assembly;
  no retry route exists in the current API
- Targeted tests: 202 passed; two stale facade-contract nodes passed on
  failed-node-only rerun
- Static gates: Ruff, format, architecture, complexity, and runtime-state
  passed
- Not run: full API or repository suite
- Metrics: migrated forbidden port types `5 -> 0`; generic facade methods
  `39 -> 34`; opaque generic facade occurrences `146 -> 129`
- Rollback: revert the commits in reverse order per vertical slice
- Remaining risk: non-migrated apparel/model-library/poster operations retain
  the generic facade and must continue to ratchet down

### A8: Completion C1 / C2 / C3

- Baseline SHA: `b17ed266b167c7b4b78149ad4ca87550f62f30ca`
- Commits: `9b9d7391`, `62cd5399`, `8caf30e7`, `77022083`
- Changed files: completion entrypoint, contracts/runtime/execution/runner/
  services/default bindings/outcomes, and focused completion tests
- Invariants: retry, lease, settlement, cancellation, BYOK billing, tool-image,
  and epoch-fencing semantics remain covered
- Confirmed risk: the public runtime was a seven-group dynamic symbol table
  containing `Any`, ORM/SQL/private helpers and a process-default RuntimeSlot
- Implementation: typed command/value/result and seven behavioral services;
  five explicit phase state objects; runner no longer reads RuntimeSlot;
  deterministic capability imports and composition-facing typed runtime
- Targeted tests: 77 passed
- Static gates: Ruff, format, architecture, complexity, and runtime-state
  passed
- Not run: other Worker domains, full Worker suite, or external integration
- Metrics: public runtime/contracts forbidden types `-> 0`; largest state has
  15 fields; largest phase 118 lines; phase parameters at most 2
- Rollback: revert the four commits in reverse order
- Remaining risk: internal `LegacyCompletionAdapter` remains an implementation
  bridge and is scheduled for a real deletion commit in Wave 4

### A9: API / Worker Runtime Containers

- Baseline SHA: `b17ed266b167c7b4b78149ad4ca87550f62f30ca`
- Agent commit: `582925e92499d805bc3e98ef925f5d22769b9716`
  (integrated as `ffd3c35`)
- Lead composition commit: `9361cc5`
- Changed files: typed API/Worker runtime and lifecycle modules, composition
  roots, Poster route lifecycle, and focused lifecycle/startup tests
- Invariants: no mutable global container; resources close once in reverse
  registration order; cleanup failures do not skip later resources;
  repeated/concurrent shutdown is idempotent
- Confirmed risk: API resources were embedded in lifespan and Worker cleanup
  depended on scattered mutable context keys without one typed owner
- Implementation: `ApiRuntime` in `app.state.runtime`; `WorkerRuntime` in arq
  context; typed capability diagnostics; Poster tagging moved out of router
  lifespan; partial startup uses the same lifecycle registry
- Targeted tests: Agent 30 passed; Lead merged run reached 88 passed and three
  stale-fake failures, then only those three node IDs passed
- Static gates: Ruff, format, architecture, complexity, runtime-state, and Web
  architecture passed
- Not run: full API/Worker suites
- Metrics/logging: startup capability matrix and owner/resource cleanup
  failures; runtime-state remained 20 pending Wave 4 owner retirement
- Rollback: revert `9361cc5`, then `ffd3c35`
- Remaining risk: Redis/ARQ/billing/provider/metrics/SSE compatibility owners
  still need removal before the runtime baseline can fall

### Web Agent: Feature Architecture Gate

- Baseline SHA: `b17ed266b167c7b4b78149ad4ca87550f62f30ca`
- Agent commit: `be1fc78f0cdb65f03fdff43418020c5b502a0bb4`
  (integrated as `8bee447`)
- Lead ownership commit: `f24006f`
- Changed files: Web architecture checker/tests and canonical
  `shared/realtime` browser/runtime factories with seven migrated callers
- Invariants: cross-feature imports use public entries; feature/file cycles and
  server-to-browser paths report shortest paths; presentational UI, store, and
  realtime ownership rules fail closed
- Confirmed risk: the prior checker covered layer inversion and SCCs but not
  feature deep imports, browser chains, API side-effect ownership, store
  coupling, or realtime construction ownership
- Implementation: graph/fact analysis and six focused fixture tests; Lead
  rejected a proposed seven-finding baseline and moved every existing runtime
  constructor to `shared/realtime`
- Targeted tests: Agent 15/16 then failed-node-only 1 passed; Lead target set
  initially had two loader dependency failures, then only those two files
  passed with six tests
- Static gates: architecture passed with 552 files, 2113 edges, zero
  baselined findings; complexity, type-check, ESLint, and diff check passed
- Not run: full Web suite or build
- Metrics: zero feature/realtime/browser ownership baseline
- Rollback: revert `f24006f`, then `8bee447`
- Remaining risk: the current tree has no `src/features` directories yet, so
  feature rules are proven by fixtures and will become active on first slice

### Wave 3 Lead Evidence

- Plan: `/tmp/lumen-wave3-plan.json`
- Initial results: `/tmp/lumen-wave3-results.json`
- Base: `b17ed266b167c7b4b78149ad4ca87550f62f30ca`
- Changed files: 65 before failed-node contract fixes
- Matched rules: 9, covering Workflow, Completion, API/Worker runtime,
  Poster, Web feature/realtime, and conservative API/Worker/Web fallbacks
- Commands: 15; full mandatory false
- Initial result: 12 passed, 3 failed
- Passed evidence: API 1532 passed with 2 existing skips; runtime lifecycle 30;
  Poster 68; Workflow 24; Completion 77; all architecture/complexity/
  runtime-state/type-check gates passed
- Web failure: both the full Web command and realtime target command identified
  only `src/lib/sse/leaderElection.test.ts`, whose local TypeScript loader
  lacked the new canonical browser-factory dependency; only that file was
  rerun and passed 2 tests
- Worker failure: collection identified one stale import of removed
  `completion_ports`; after migrating the module to typed services, its
  failed-file run passed 69 of 70 tests, then only
  `test_startup_failure_closes_upstream_clients` was rerun and passed
- No full command was rerun after either failure; the failed file/node
  discipline was preserved
- Governance: Web architecture passed with 552 files, 2113 edges, and zero
  baselined findings; backend complexity remained 31 and runtime-state 20,
  both scheduled for Wave 4 reductions
- Final Wave result: every selected gate and failed node passed

### Wave 4 API Complexity Handoff

- Baseline SHA: `9b0899eee05b80c786bc6b3316d23212cc7babd3`
- Agent commit: `b4bbcf7e37c82de901c857d441f74c2b8964851d`
  (integrated as `5c734a4`)
- Changed files: Auth, user export, task listing, video capability/submission,
  and direct tests
- Invariants: signup transaction and invite semantics, export archive/security,
  task query behavior, video pricing/idempotency/billing/outbox order
- Implementation: typed request/catalog/submission contexts and focused helper
  extraction
- Targeted tests: 173 passed
- Static gate: changed-file Ruff and format passed
- Not run: full API/repository suites
- Metrics: six owned findings to zero; signup `205 -> 36` lines; export nesting
  `7 -> 1`; four parameter findings removed
- Lead correction: removed the Agent's new video `**legacy` resolver in
  `4b19ed0` after migrating callers to typed context/services
- Rollback: revert `4b19ed0`, then `5c734a4`
- Remaining risk: direct non-HTTP task callers now construct typed query values

### Wave 4 Worker Complexity Handoff

- Baseline SHA: `9b0899eee05b80c786bc6b3316d23212cc7babd3`
- Agent commit: `09e19f91e3b3ead9794ba41835d0af2e16a4996a`
  (integrated as `fa7e86d`)
- Changed files: retry/provider selection, completion context/tool/stream,
  context summary, generation diagnostics, memory finalization, image request,
  video parsing, and direct tests
- Invariants: memory lock/commit/rollback and PII ordering, retry defaults,
  billing floors, provider order, summary coverage, request bodies, and video
  error precedence
- Implementation: typed `Unpack` argument contracts, focused contexts/helpers,
  and flattened parsers
- Targeted tests: 340 passed
- Static gate: changed-file Ruff and format passed
- Not run: full Worker/repository suites
- Metrics: 12 owned findings to zero; memory finalization `304 -> 121`;
  reasoning nesting `7 -> 0`; video error nesting `7 -> 1`; nine parameter
  findings removed
- Rollback: revert `fa7e86d`
- Remaining risk: typed `Unpack` entrypoints intentionally expose compact
  runtime signatures while preserving named call contracts

### Wave 4 Worker Facade Handoff

- Baseline SHA: `9b0899eee05b80c786bc6b3316d23212cc7babd3`
- Agent commit: `518ac2da2d2df331ba4dd7dcf74f9a6d6a2909d7`
  (integrated as `c1d3670`)
- Deleted files: Worker completion, video-generation, and upstream facades
- Replacement files: typed leaf entrypoints under existing parts packages
- Invariants: ARQ registration, video cron seconds `15/45`, runtime validation,
  upstream identities, startup validation, and shutdown cleanup
- Targeted tests: 491 passed after dependency setup
- Static gate: Ruff passed; format remediation applied
- Not run: full Worker/repository suites
- Metrics: 34 facade bindings across 24 files to zero
- Rollback: revert `c1d3670`
- Remaining risk: consumers outside this repository must migrate deleted paths

### Wave 4 API Runtime-State Handoff

- Baseline SHA: `9b0899eee05b80c786bc6b3316d23212cc7babd3`
- Agent commit: `11c1fa2460a00e4565086363958a88da24642091`
  (integrated as `24d703a`)
- Changed files: Poster library/workflow locks, admin update/release cleanup,
  prompt runtime, and direct tests
- Invariants: file locking remains authoritative, publication rechecks state,
  cleanup tasks are application-owned and drained, prompt holds are released
- Targeted tests: 143 passed
- Static gate: Ruff passed; format remediation applied
- Not run: full API/repository suites
- Metrics: five runtime-state findings to zero; route mutable lock/client count
  remains zero
- Lead corrections: removed the stateless Poster compatibility lock in
  `4df6c9e`; replaced the prompt-to-main service locator with explicit Telegram
  runtime injection in `1cb2c86`
- Rollback: revert `1cb2c86`, `4df6c9e`, then `24d703a`
- Remaining risk: independently mounting the Telegram router without the prompt
  router lifespan is unsupported

### Wave 4 Lead Evidence

- Plan: `/tmp/lumen-wave4-plan.json`
- Initial results: `/tmp/lumen-wave4-results.json`
- Base: `9b0899eee05b80c786bc6b3316d23212cc7babd3`
- Changed files: 109 before stale-reference fixes
- Matched rules: 10; commands: 14; full mandatory false
- Initial gates: architecture, complexity, runtime state, and changed-file Ruff
  all passed
- Initial passing suites: Image artifacts 43; Canvas/Auth 85; Upload 50;
  Message/Video 160; Poster 68; Completion 78
- API collection failures: 12 files referenced removed Image/Volcano facades;
  only those files were rerun after import migration and passed 115 tests
- Worker full result: 1555 passed with 6 existing skips, then two stale
  old-file path assertions failed; only those two node IDs were rerun and
  passed
- Workflow/runtime target collection failures shared the same removed Image
  facade import and were covered by the failed-file run; the public Workflow
  port contract was then run directly and passed 2 tests
- No failed full command was rerun
- Governance: complexity `31 -> 11`, with stage-one dimensions `2 / 7 / 2`;
  runtime state `20 -> 15`; facade inventory `8 -> 4`; public Workflow and
  Completion forbidden types remain zero; no baseline was expanded
- Facade deletions: API Volcano media/assets, API Images route, Worker
  completion/video/upstream, plus the Completion legacy adapter filename/type
- Remaining facade inventory: Core models/schemas and the two large Worker
  implementation surfaces retain explicit owners and deletion conditions; no
  new callers were added
- Final Wave result: every selected gate and every failed file/node passed

### Final Local Gate

- Release state: `v1.2.77`
- Version sync: `python3 scripts/version.py sync`, `uv lock`, and
  `python3 scripts/version.py check` passed
- Full invocation count: exactly one `bash scripts/test.sh -q`
- Governance: uninstall shell tests, Ruff, architecture, complexity, and
  runtime-state gates passed
- Worker: 1557 passed, 6 existing skips
- API: initial final run reached 1533 passed and 2 existing skips, with two
  stale `.execute` test calls; only those two node IDs were migrated to
  `upsert_project` and passed
- Core: 561 passed
- TGBot: 87 passed
- Image job: 188 passed
- Mock image upstream: 8 passed
- Operations: initial continuation reached 400 passed and 4 existing skips,
  with one stale hard-coded runtime ceiling; only that node was updated to the
  lowered value `15` and passed
- Web: 481 tests passed; layout/UI/architecture/complexity/ESLint gates,
  TypeScript, and production build passed
- Build warning: existing Sentry/Prisma OpenTelemetry dynamic dependency
  warning; build completed successfully
- No second full invocation was run after the failed-node fixes
