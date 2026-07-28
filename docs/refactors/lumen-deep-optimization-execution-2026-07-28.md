# Lumen Deep Optimization Execution Ledger

Source plan:
`Lumen-deep-optimization-plan-2026-07-28.md`

This file records implementation status and evidence. It does not replace or
reinterpret the source plan.

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
| F-09 | Confirmed | Completion ports expose extensive `Any`, ORM/SQL/private helpers and RuntimeSlot state; internal capability imports use broad `except Exception`. |
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

- [ ] Workflow typed vertical slices
- [ ] Completion runtime C1/C2/C3
- [ ] API/Worker runtime container
- [ ] Web feature boundary gate
- [ ] Lead review and ownership audit
- [ ] Wave impact plan passed

### Wave 4

- [ ] Compatibility facades retired
- [ ] Complexity baseline lowered
- [ ] Runtime state baseline lowered
- [ ] Public port type audit passed
- [ ] Final impact plan passed
- [ ] Final `bash scripts/test.sh -q` passed
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
