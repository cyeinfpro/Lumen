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
| F-01 | Confirmed | Canvas checks idempotency before and after a per-canvas row lock, then flushes `CanvasRun` without advisory serialization or `IntegrityError` winner recovery. |
| F-02 | Confirmed | Copy fallback propagates `_copy_exclusive` `FileExistsError` instead of resolving an identical destination winner. |
| F-03 | Confirmed | Metadata page loading does not classify a disappeared metadata file as a normal tombstone. |
| F-04 | Confirmed | `_copy_exclusive` creates the destination fd before opening source and can lose ownership of the raw fd on source-open failure. |
| F-05 | Confirmed | Reconcile sweep exceptions only increment `deferred`; no error code, structured log, or failure metric is emitted. |
| F-06 | Confirmed | Poster API background tagging synchronously reads and base64-encodes the original file, creates clients per attempt, and uses a route-owned semaphore. |
| F-07 | Confirmed | `useSSE` registers a rejecting recovery wrapper even when no real adapter exists; runtime awaits every registered adapter. |
| F-08 | Confirmed | Signup, BYOK signup, and login enrich the response with runtime settings after commit and cookie setup without a non-fatal fallback. |
| F-09 | Confirmed | Completion ports expose extensive `Any`, ORM/SQL/private helpers and RuntimeSlot state; internal capability imports use broad `except Exception`. |
| F-10 | Confirmed | Outbox delivery performs Redis/SSE I/O inside the `FOR UPDATE SKIP LOCKED` transaction. |
| F-11 | Confirmed | Message and task post-commit publishers are awaited serially with separate two-second budgets. |
| F-12 | Confirmed | Video submission performs provider, media snapshot, validation, and pricing work before checking the unique-key winner. |
| F-13 | Confirmed | Artifact staging dispatches every chunk through `asyncio.to_thread`; upload storage reservation is `max_bytes * 5`. |

## Wave Checklist

### Wave 0

- [x] Impact test manifest
- [x] Impact planner with reverse dependencies and full-gate escalation
- [x] Resource-aware test plan runner
- [x] Targeted Web test runner
- [x] Lead review
- [x] Wave impact plan passed

### Wave 1

- [ ] Image F-02/F-03/F-04/F-05
- [ ] Canvas/Auth F-01/F-08
- [ ] Poster F-06
- [ ] Web SSE F-07
- [ ] Lead review and ownership audit
- [ ] Wave impact plan passed

### Wave 2

- [ ] Outbox F-10
- [ ] Message/Video F-11/F-12
- [ ] Upload F-13
- [ ] CI impact batching
- [ ] Lead review and ownership audit
- [ ] Wave impact plan passed

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
