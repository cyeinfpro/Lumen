# Lumen generation core: isolated implementation checkpoint

Date: 2026-10-10. Base: 86c55233c76ec345a91c3a226686c3c84a4c72b0.

This work is a separate backend/core patch. It does not include a release,
deployment, production migration, new dependency, or live provider generation.
The existing Canvas graph schema remains V1; all response additions are optional
for old clients.

## Implemented response contracts

### Authoritative assets

Canvas document responses add `assets: AssetDescriptor[]`. Identity is
`kind + asset_id`, with `source_sha256` as the original-content fence. A locator
is not an identity and must never be persisted as one.

- `schema_version: 1`
- `asset_id: string`, `kind: "image" | "video"`
- `source_sha256: string`, `mime: string`, `size_bytes: number`
- `width, height, duration_ms: number | null`
- `preparation_state: "pending" | "preparing" | "ready" | "failed" | "unavailable"`
- `preparation_revision: number | null`, `updated_at: ISO timestamp | null`
- `locators: {original: string, preview: string, thumb: string | null}`

Locators are existing authenticated same-origin API routes. Image preview uses
preview1024 and thumbnail uses thumb256. Video preview currently uses its binary
route; thumbnail exists only when a poster is actually recorded. No storage
paths, transport tokens, signed URLs, or credentials are exposed.

Metadata comes from owner-scoped database records. Deleted, foreign, and missing
assets are omitted. Original graph config remains an ID reference. A failed
browser preview is independent of original asset readiness.

For the same original hash, a client can discard an older video
preparation_revision. Requests still need a local response-generation fence.
There is not yet an asset-specific SSE event. Snapshot refresh is the current
delivery mechanism.

### Task recovery and cost details

Image tasks now resolve their actual Generation owner, matching video task detail.
The existing `status` remains unchanged. `task.recovery` adds:

- `state`: unavailable, submission_unknown, cancel_requested, saving_artifact,
  queued, running, reconciling, or an actual terminal status
- `can_query`, `can_cancel`, `can_generate_new`
- `automatic_resubmit: false`

An unknown submission cannot be treated as a fresh retry. Cancellation is still
a request and follows existing billing/cancel behavior. These flags do not create
new action endpoints and do not promise an artifact-save retry operation.

`task.billing` reports CNY micro-units from existing task pricing and wallet
records. Missing estimate, reserve, or actual amount stays null. Settlement
refund balance deltas are not mistaken for service cost. Only exact task-owned
ledger references are read. No hold, charge, release, or settlement is performed.

Execution responses and run detail include a task aggregate with nullable totals,
known subtotals, and task_count. Prefer these explicit billing objects over old
legacy run counters, which may have been initialized to zero by older versions.

### History and incremental replay

- GET /canvases/{canvas_id}/nodes/{node_id}/history?cursor=...&limit=30
  returns `items, next_cursor`, ordered by created_at and ID descending.
  Includes immutable config/input snapshots and semantic hashes for comparison.
  Cursor pagination is owner/canvas/node scoped.
- GET /canvases/{canvas_id}/runs/{run_id}/event-batch?after_seq=0&limit=100
  returns `items, after_seq, next_after_seq, last_event_seq, has_more,
  snapshot_required`. Gaps and future cursors require a fresh snapshot; consumers
  must not silently advance across them.

The old events route is preserved. API single-node, plan creation/admission/repair,
and read-repair commits now publish the existing global user-channel notice only
after the transaction commits. Worker reconciliation publishes only after its
owned transaction returns. Notice failure is bounded and cannot roll back a task
or cause regeneration. Stable IDs and seq are the only public notice payload.
Repeated progress snapshots coalesce into durable run events. Worker reconciliation
still runs at the existing seconds 10/40 cadence; this is not a claim of faster
provider progress. The frontend's 2-second fallback is retained until joint SSE
integration is verified.

History input snapshots preserve bindings, source_execution_id, output_index,
asset ID/hash, and the text actually consumed. A historical branch must use those
identities rather than silently copying current graph inputs.

Canvas snapshots additionally return execution_freshness keyed by execution ID:
state fresh/stale/unknown and reason node_removed/definition_changed/inputs_changed/
upstream_changed/snapshot_unavailable (null when fresh), plus stale_node_ids.
This is a read-only projection, not a task status mutation. Current semantic
definitions and input bindings are compared on graph edits and selection changes;
follow-active descendants inherit stale, pinned boundaries stay stable. Position
changes do not invalidate work. Missing legacy snapshots remain unknown.
Request-local graph/input/selection indexes avoid rebuilding the graph per
execution.

## Durable video metadata preparation

New reference uploads persist pending preparation in existing metadata JSON.
An API lifecycle loop claims bounded batches. It verifies original size/hash and
uses the existing 15-second ffprobe policy and transcode-capacity lease. Results
publish under a claim token, expiry, owner, original hash/path, and deletion
fence. An interrupted claim is recoverable after lease expiry. Failed inspection
is durable and does not invent valid dimensions.

Poster derivation and explicit failed-only retry are now implemented. See
lumen-video-preparation-2026-10-10.md for the quota-slot, checksum manifest,
lease-loss, claim/revision fences, atomic installation, and validation evidence.
The route is POST /videos/{video_id}/preparation/retry, with CSRF, owner checks,
expected_source_sha256, expected_preparation_revision, and idempotency_key.
The response is {asset: AssetDescriptor}; active preparation is never restarted.

Original bytes and retention are unchanged. The service does not automatically
backfill old uploaded assets. Existing generation-time transport preparation
remains authoritative; no duplicate transport cache is introduced.

## Persistent RunPlan admission and recovery

The new pure core compiler provides:

- immutable, tenant/canvas/revision-scoped plans with canonical SHA-256
- single, upstream, selection, and all target computation
- explicit output reuse and exact pinned/boundary references
- independent-branch continuation and fail-fast planning
- unknown-submit waiting without resubmission
- retry eligibility limited to confirmed failed/expired steps
- known/unknown costs and an optional budget field
- integrity-checked restoration of a persisted plan

Single-node submission stores its plan in run summary JSON without changing its
existing request flow. Batch endpoints are now implemented:
- POST /canvases/{canvas_id}/plans/preview
- POST /canvases/{canvas_id}/plans/run
- POST /canvases/{canvas_id}/runs/{run_id}/retry-failed

Preview/run accept document_revision, kind upstream/selection/all, target_node_ids,
reuse_outputs (exact execution/output choice), output_indices, budget_micro,
failure_policy continue_independent/fail_fast, and auto_select_on_success.
Run also requires the preview plan_hash and an idempotency_key matching the HTTP
Idempotency-Key header. Retry accepts execution_ids, additional_budget_micro and
the same key/header rule. These write endpoints require CSRF and active owner
identity. No new database columns or production migration are required.

Plans persist their immutable graph version, semantic SHA-256, target/dependency
order, exact boundary references, explicit output choice, model/capability and
admission estimate. Position-only edits do not change node semantic identity.
Hidden-in-run frames are excluded from automatic targeting. Equal prompts never
trigger implicit output reuse. Multi-candidate dependencies require an explicit
output_index; a partial result blocks descendants rather than shifting indexes.

An API-lifecycle dispatcher rotates bounded batches and admits ready steps through
the existing image/video task adapters, original outbox and original billing.
The pending execution, task, outbox, task hold and admission counter commit
together. SQL row locks/status predicates fence competing API instances. A crash
before commit leaves the step pending; a lost commit acknowledgement is checked
against its durable task before any failure projection. Unknown provider submit
states are never re-enqueued. Independent branches continue; failed descendants
are blocked. Restart resumes the same persisted plan, not the latest draft.

Retry checks the canonical Generation/VideoGeneration owners as well as Canvas
status, rejects uncertain/active/successful/partial-output work, and creates new
attempts only for explicitly failed steps plus blocked descendants. Existing
successes and active tasks remain. Changed pricing/model capability requires a
new plan. Repair has its own durable idempotency record and explicit extra budget.

Budget means admission estimates, not a guarantee that final provider settlement
cannot exceed the estimate. Existing billing remains the sole wallet writer.
Preview does not freeze funds. It uses configured real pricing, per-image rate
rounding and the existing video hold estimator, never substitutes unknown with
zero. Dispatch re-quotes and also checks the actual admitted task estimate before
its original transaction commits. Missing pricing, BYOK batch mode, unprepared
required assets and invalid static inputs are rejected before any branch admits.
Old video uploads without trusted metadata may therefore require preparation
before a batch; the existing single-node path is unchanged.

GET /canvases/capabilities now provides a versioned public image capability
catalog and existing dynamic video options. Video batch preview requires an
explicit model; original single-node automatic model selection is preserved.
Execution fingerprints include safe effective model/capability snapshots.
No supplier credentials are exposed.

## Verification at the 10:59 UTC checkpoint

Operation cadd0428456c41cb9d61f9164616f948:
- 232 related API tests passed in 7.58s (Canvas, metadata/posters, storage lifecycle,
  inventory bounds, reference variants, and lifecycle cleanup).
- 27 Canvas worker tests passed in 0.93s.
- 72 Canvas core tests passed in 0.74s, including 100/500/1000-node plan correctness.
  These are synthetic correctness checks, not a claimed performance multiplier.

Operation dee6484018a040b39fde09aebbe8c38f:
- After simplifying duplicate public/private wrappers, full complexity,
  architecture, facade inventory, runtime-state and diff checks passed.
- Related poster/reference API regression: 36 passed in 0.92s.
- Core volcano asset regression: 30 passed in 1.74s.
- Worker volcano asset regression: 74 passed in 8.36s. The operation completed
  successfully in 28.77s; a subsequent process check found no Lumen test residue.

Poster owner operation d201f590ce56444f81da9a6c1d94a66a separately verified
30 pure cases and one real two-second synthetic video; no provider was invoked.

Earlier failures are preserved in tmp/generation-core-20261010:
an incorrect lifecycle test filename prevented one API collection; new freshness
fixtures initially omitted the resolver's normalized order=0; compatibility
checks caught a partial graph projection and a mock owner without identity.
The production projections now retain unknown/omit unidentifiable progress
instead of breaking old responses. Formatting exposed the reference module's
hard line ceiling; duplicate wrappers were replaced by one public implementation
and compatibility aliases. No test assertions or governance baselines were relaxed.
Ruff passed after removal of an unused import. The only subsequent production
change consolidated equivalent reference helpers and was rechecked by the 36 API
and 74 worker storage tests. Candidate hashes are in the adjacent manifest.

## Remaining delivery work and limits

- Frontend integration, historical branching/parameter compare, batch controls,
  joint SSE interruption/cross-tab checks and true large-canvas browser validation.
- PostgreSQL multi-process contention/deadlock and real Redis lease-loss stress.
  SQLite restart/idempotency tests are not proof of PostgreSQL concurrency.
- Full repository Python suite and combined release gates were not run.
- No real paid provider calls, production changes, deployment, commit or push.
  This isolated backend checkpoint is not a claim of completed end-to-end UI rollout.

## Isolation correction, 10:10 UTC

On task resumption, an expired orchestration variable omitted the workspace ID
from one newly created file write. Only the new, untracked plan_pricing.py landed
in the original repository. The error was reported and corrected before use.
Recovery operation 0f8845e7c2a148919fbfff2aaeeaa1e8 succeeded: it required SHA
ce195dc71cf7b90921798b2310524fd98e6125222a26a2e48030d9bb434bb957,
verified no API source imports referenced the new module, moved it into this
isolated workspace, then verified identical SHA and absence at the source.
The original repository's full tracked binary diff was byte-identical before
and after recovery, and its status entries were identical except for removal
of that newly created file. Existing user changes and Phase A files were preserved.
All subsequent project calls specify the workspace ID literally.


At 10:22 UTC an erroneous empty-content placeholder write to run_event_service.py
was denied before execution. It was not retried. A subsequent read confirmed the
original 806-byte file and SHA
3950559eb96dc076876cb55c348ab3b7ecb0a488695290e7f1a04bc8f1a31a00.
The legitimate two-line after-commit enqueue integration used an exact-SHA,
non-empty edit (operation f2fca458f0b24270a1b24f6e46d5fb81), preserving the original
event sequencing function. Later script edits parse the complete candidate
Python source before replacing exclusive files.

## Review fixes, 11:42 UTC checkpoint

The earlier 75-file manifest is superseded. Six review findings were addressed:
1. Core task_outcome_knowledge is now shared by recovery projection and the
   failed-step retry guard. Terminal expired video submit_unknown errors,
   image result-unknown codes (including NO_IMAGE_RETURNED), and current-epoch
   image dispatch-without-response receipts stay unknown. Video history cannot
   make an unconfirmed current canceled delivery safe. Unknown owners are rejected
   before repair attempts, budget mutation or admission.
2. Background video submission resolves public URLs only from trusted stored/site
   configuration or PUBLIC_BASE_URL. No synthetic Host or request is invented.
   Inline-capable image references retain their existing optional URL behavior.
3. Preview and submission share provider reference/count/aspect validation.
   Known video durations and required background public base are checked during
   whole-plan preview, before any branch is admitted. Quote inputs are transient,
   owner-scoped descriptors, not new persisted graph fields or paid calls.
4. GET repair commits per execution, including unchanged progress. Keyset scans
   use immutable created_at/id and still scan beyond unrepairable candidates.
   No Run lock survives into another execution or another page.
5. Media holds use the bounded shared capacity guard and conservative renewal
   start timestamps. Lease loss signals the media thread, kills its subprocess,
   and waits for OS reaping before releasing the hold. Canceled ffprobe/ffmpeg
   work cannot continue merely because its asyncio wrapper was canceled.
6. Failed-step repair restarts only selected failures and their recoverable
   blocked dependency closure. Fail-fast requests missing other failed branches
   reject before mutation and return missing_node_ids.

Verification:
- First regression 7712430331f24b6e83cdf9c9c5fb3da1: API 213 passed / one existing
  paging test failed; core 80 and worker 27 passed. The old paging assertion was
  retained and the implementation corrected.
- 22803166435a4a9c857ac447971b0b56: old paging regression passed; two real local
  PostgreSQL dual-session barrier tests passed (Canvas/Run and next-page
  Execution/Run). Each used and removed its own unique disposable schema.
- 1b67adab72da487d91cf6d6fd7d2785f: three real disposable Redis TTL-expiry and
  reacquisition tests passed. Old-owner renewal false/error/hang was injected;
  a separate live client acquired the slot only after the old child was reaped.
- 4a552d002f1c4c11b63f9aa88477f4dd: final API 219 passed in 11.81s, including the
  PG barriers and stronger Redis cases whose OS child ignores SIGTERM; worker
  27 passed, Ruff and diff checks passed. The command mistakenly named nonexistent
  core/governance files, so those stages did not run in that operation.
- ae1c97645ae641dca468b66dff284e83: corrected actual commands passed core 80,
  full complexity and architecture gates. Process check found no test Redis or
  media residue; the two explicitly recorded PostgreSQL schemas were absent.

These are real database/lease/OS-process tests with deterministic injected
failure and lock schedules. They are not a full concurrent dispatcher + actual
worker + HTTP GET end-to-end run, production contention stress, a paid-provider
verification, or a full repository Python pass. Existing SQLite dispatch,
restart/idempotency and ownership tests are retained. Independent re-review and
joint UI integration remain required before delivery.

At 11:35 a broad marker-to-EOF replacement command for read_repair.py was denied
before execution. It was not retried. Full-file read verified all 583 lines and
SHA a7adb31a82ea4662a5964ea6ca7c2ad4aa65627a47bb2b251c7a3a7a4334c212.
The correction instead used a unique, exact-SHA edit with backup, operation
a7cf5dd918424f8fabe06d3193df2508, preserving all unrelated functions. A canceled
final-test connector response was checked against durable operations before one
same-idempotency retry; no duplicate test run was started.

## Independent re-review, 12:27 UTC

The six prior P1 corrections were independently traced in source, including
canonical owner/current-epoch receipt checks before repair mutation, configured
background public URLs and whole-plan static media validation, per-execution
GET repair commits with immutable keyset scanning, cancellation propagation to
OS media process kill/reap, and the selected-failure repair closure.

One additional real recovery defect was found: after fail-fast blocked an
independent branch, repairing a selected failed root left that branch blocked
as intended, but plan_ready_nodes treated this old blocked consequence as a new
global failure. It immediately blocked the freshly repaired root again.
Operation 8119b0303ec840678b687185ab783a9b preserved the failing pure-function
reproduction in tmp/generation-core-20261010/independent-failfast-before.log:
restart=('a',), no missing failures, then ready=(), blocked=('a',), finished=True.

The fix makes only actual failed/partial_failed/canceled/expired work trigger
global fail-fast. Existing blocked nodes still block their dependencies and
remain outside the selected repair closure. New regressions verify both pure
scheduling and the SQLite persisted retry-to-dispatch path, including one
admitted task only and unchanged independent blocked execution identity.

Verification after the fix:
- 01824d3d6db6484aba300cbd8d093c4c: Canvas core 81 passed in 0.77s; focused API
  dispatch/retry/video-preflight 27 passed in 1.29s; changed-file Ruff passed.
  Its first complexity invocation could not locate ruff in PATH and failed
  closed; architecture/diff were not run by that initial command.
- 076589c5166e48f1bef3c9cbf4d38c23: using the existing environment's PATH,
  full complexity and architecture gates plus git diff --check passed.
- No dependencies were installed. No provider, production, commit, push, release
  or deployment action was performed. The PostgreSQL/Redis tests were reviewed
  but not rerun in this pass; their previous evidence and test files remain.

Core is eligible for joint UI integration after the refreshed candidate hashes
are verified. This is not acceptance of actual UI integration, an end-to-end
provider run, or the full repository test suite.
