# Runtime reliability audit — 2026-09-21

## Scope and baseline

The audit started from `d3d91da831c885396975b93a744232a370a69f4e` / `v1.2.169`. Existing uncommitted work was preserved before edits and included in the review. Repository inventory, Python AST checks, frontend recovery paths, production container state, bounded 48-hour logs, backup receipts and CI failures were reviewed. Static scanning is not a claim that every possible runtime path has been exercised.

The production baseline had six healthy Lumen containers, no container restarts or OOM flags, and no apparent RAM or disk pressure. No application-source drift was found inside the API, worker or web containers. A fresh API-container process measured approximately 1,880 ms for `import lumen_core`, which also imported SQLAlchemy; an encoder warm-up measured approximately 427 ms, above the API's former 200 ms startup budget. These are single observations, not a load-test percentile or a service-wide speedup measurement.

The bounded API logs contained 24 slow-tokenizer warnings. Backup logs showed retention work inside the writer-quiescence window. A past backup failed because an invalid `previous` deployment link failed root validation; later backups had already recovered before this audit. The root-validation safety check was not weakened. Previous CI also failed the Agent duplicate-reconciliation assertion in all seven configured browser layouts.

## Changes

| Area | Fault or avoidable work | Correction and regression coverage |
| --- | --- | --- |
| Agent recovery | Manual query refetch, snapshot polling and SSE recovery could fetch the same snapshot concurrently. | Per-workspace in-flight sharing for message and active-run GETs; account/epoch, session and pagination keys; independent observer cancellation; last-observer network abort; no stale-response cache. `agentSnapshotReads.test.ts`. |
| Realtime subscriptions | An inline protocol callback was an effect dependency even though Effect Events already supplied current callbacks. Ordinary renders could release and reacquire a subscription. | Keep transport/scope dependencies reactive, but not callback identity. `useSSE.lifecycle.test.ts` checks lease stability, current callbacks and genuine scope/policy changes. |
| Project refresh | Background refresh errors replaced the editor, losing transient UI state. Reconciliation could be retried as though it were a pure read. | Preserve the existing project console and draft, show a non-destructive retry notice, disable duplicate retries, pass cancellation signals, fence account changes and do not automatically replay reconciliation POSTs. `WorkflowRefreshRecovery.test.ts` and `workflowRefresh.test.ts`. |
| Backup administration | Synchronous directory and receipt lookup ran inside async API handlers. | Offload storage traversal while keeping request handling responsive. `test_backup_event_loop.py`. |
| Preset materialization | File reads, hashing, image inspection and durable writes ran in the API event loop. Image metadata was inspected using a second read. | Perform binary work in a thread, inspect the bytes actually copied, keep database work on the event loop, drain an uncancellable writer before cleanup and remove uncommitted files after cancellation or flush failure. `test_preset_materialization_io.py`. |
| Tokenizer startup | The encoder could exceed the startup budget or depend on a runtime download. Non-finite timeout overrides were accepted. | Bake the encoder cache into Python images, verify loading with networking disabled during image build, allow a 2-second API startup warm-up and reject non-finite timeout overrides. Runtime fallback semantics remain sticky. |
| Lightweight core imports | Importing a small core utility eagerly initialized unrelated ORM/provider modules. | Preserve historical package exports through lazy imports without introducing a second module or ORM registry. Register the existing compatibility surface and test module identity and lightweight imports. |
| Backup interruption | Historical retention cleanup unnecessarily prolonged writer downtime and slow-path checks spawned Python for uncommitted legacy entries. | Restore writers and verify readiness before retention, retain the recovery journal until completion, skip obviously uncommitted entries before Python inspection, and preserve restart-only recovery after interruption. `test_backup_signal_safety.py`. |

Tokenizer tests now own separate production runtime instances. A cold-load thread is allowed to outlive its bounded caller, so a later test must not reset another test's live loader. This isolates test state without changing production deadlines or weakening concurrency/fallback assertions.

## Release and acceptance requirements

Run the repository's Python governance and independent backend suites, Agent Runtime checks, frontend unit/lint/type/build checks, and the seven-layout Agent reconciliation browser regression. Retain failure and cancellation receipts rather than describing interrupted runs as successful. Browser navigation timeouts during local compilation do not constitute a passing or failing business assertion.

A production update requires a successful formal version-tag Docker Release, immutable image/source provenance and the supported deployment runner. Verify API readiness, worker health, all six containers, deployed source/version, offline encoder availability, and backup recovery/receipts after the update. Keep the previous release and paired backup available for rollback. No migration, billing formula, upstream request contract, Nginx buffering rule or production user data change is part of this patch.

Automated browser fixtures do not replace manual Safari/device testing or a paid real-provider generation. Those checks must not be claimed without separate execution evidence.
