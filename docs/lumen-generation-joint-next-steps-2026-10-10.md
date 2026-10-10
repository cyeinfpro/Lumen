# Lumen generation: remaining joint delivery

Date: 2026-10-10. Workspace: 520b06121cc249f7b5c67da858426a77.
The sections below retain the original implementation and acceptance requirements.
They describe the earlier checkpoint, not the latest release status.

## Progress checkpoint: 2026-10-10 16:05 UTC

- RunPlan typed APIs, revision-invalidated preview, explicit budget/output identity,
  stable intent recovery and backend active/unknown admission guards are implemented.
  History pagination, identity fences, A/B snapshots and exact historical branching
  are implemented. The combined earlier UI acceptance passed 24 real-browser cases;
  final regression must include the later viewport/touch changes below.
- Targeted backend verification passed 216 API, 92 core and 36 worker tests with
  PostgreSQL, Redis and ffmpeg gates enabled (no skips in that run). Image/edit and
  video API-to-dispatcher-to-worker-to-GET stub harnesses passed. See
  [image/edit evidence](lumen-stub-chain-acceptance-2026-10-10.md) and
  [video evidence](lumen-video-stub-acceptance-2026-10-10.md) for exact boundaries.
- The second recorded scale matrix passed Chromium at 100/500/1000 nodes and failed
  WebKit at all three overview-touch selections. Raw results remain preserved.
  Real touch tracing identified WebKit compatibility-click retargeting from a
  1.54-pixel header grip to the neighboring image preview. A guarded touch-origin
  fix passed the 100-node case and direct image touch/mouse/keyboard activation;
  the 500/1000 targeted rerun stopped during view preparation, before tapping.
  Full acceptance remains pending; zooming first is not considered that bug's fix.
- Layout undo exposed a separate empty-viewport defect. Conditional geometry-only
  recovery now accommodates the inspector closing when history clears selection,
  while fencing newer graph/selection, camera movement and browser resize.
  Desktop keyboard undo/redo and config-only camera preservation passed two browser
  cases. Toolbar/mobile paths and the frozen full matrix still need the final gate.
- Governance/manifest tests passed 39 cases, all referenced Python regression cases
  passed 34 parameterized cases, and the referenced frontend files plus viewport
  geometry tests passed 35 cases. These are scoped gates, not the full repository CI.
- Original source remains unchanged: all 51 protected dirty-file hashes matched,
  source HEAD is unchanged and the index is empty. Later authorization covers
  selective integration, commit/push and the existing versioned release workflow.
  See [release scope](lumen-release-scope-2026-10-10.md). No release has been made.

The original requirements follow unchanged.

## Integrated baseline

Base HEAD is 86c55233c76ec345a91c3a226686c3c84a4c72b0. All 51 current
source dirty/untracked files, including accepted Phase A, were preserved.
Core 92 plus UI 39 yields 125 unique candidate files; six overlaps are identical.
Original source status, binary diff and dirty bytes were verified unchanged.

Accepted core manifest:
ea69c280ea8acfe4267d1a5ae3e85174b976d130ede6af23d5494baa42a9dc11.
Accepted B1/B2a manifest:
e4ec2af1ab5b15d110318a5491b47aaf65645be569e50e1942de50499d2f4842.
Copies and source snapshots are under tmp/lumen-core-ui-joint.

## RunPlan UI

Introduce typed preview/run/retry-failed clients and a pure client state machine,
then compose existing toolbar/inspector controls without changing single-node flow.

- Explicit scope: selection, upstream target, or all runnable nodes. Show exact
  authoritative targets; never infer reuse from matching prompts.
- Save draft through existing revision fences before preview. Any graph, scope,
  capability/price, output index, reuse, budget or policy change invalidates it.
  An old response cannot enable Run.
- Require an explicit integer CNY micro-unit admission budget within JavaScript's
  safe integer range. It is an admission estimate, not a final-bill guarantee.
  Unknown pricing never becomes zero.
- Reuse exact execution/output identities. Require candidate choice for
  multi-output dependencies; partial results cannot shift output indexes.
- Submit exact plan_hash/document_revision plus stable idempotency body/key.
  Double clicks, transient failures and lost acknowledgements reuse that intent.
  Ambiguity triggers a durable-state query, never automatic fresh paid work.
- Retry only selected latest confirmed failures with explicit additional budget.
  Surface fail-fast missing_node_ids rather than silently broadening retry.
  Unknown/active/success/partial output remains non-retryable.
- Reuse global SSE and fallback snapshots; cancellation is a request, never a
  promise of immediate provider cancellation or zero cost.

Required tests: double click, offline/lost acknowledgement, stale preview,
revision conflict, changed capability/price, unknown/partial outcome, overflow,
insufficient budget, and repaired fail-fast branch alongside unrelated blocked
work. Verify exact request bodies and durable task count using provider stubs.

## History, comparison and historical branching

The inspector currently renders recent_executions only. Add on-demand owner-
scoped history pages (limit 30, opaque cursors), identity/canvas/node query
keys and abort/generation fences. Use bounded previous/next pages, not unbounded
DOM or eager history downloads.

A/B comparisons preserve immutable execution selections across pages and clear
on identity/canvas/node changes. Compare saved config/input snapshots, exact
execution/output/asset bindings, hashes and processor_version. Missing legacy
snapshots are incomplete; bounded comparisons report truncation.

Use optional authoritative execution_freshness without mutating historical
tasks or billing. A saved projection cannot claim freshness for unsaved edits.

Historical branching is an undoable draft graph edit only. Reconstruct exact
saved text/config/assets and pin generated bindings to saved execution/output.
If provenance, source node or compatible ports are missing, block the action.
Never substitute current graph inputs or active results. Keep output selection
on its existing compare-and-swap endpoint.

Required tests: page replay, delayed old-node/account response, failed-page retry
without losing A/B selections, provenance differences, legacy unknown state,
selection CAS conflict, missing pinned source and undo with zero execute calls.

## Real large-canvas browser coverage

Existing projection benchmarks and 100/500/1000-node core cases are synthetic.
Add real deterministic browser fixtures at those sizes with representative
image/video descriptors. Record browser/version, viewport, node/edge/media
counts, cold/warm state, machine load and raw sample distributions.

Measure initial readiness, select/drag/pan/zoom, inspector opening, output
selection, snapshot merge and SSE bursts. Assert draft/selection/viewport
preservation, bounded hidden-tab requests and media decoder/network behavior.
Video loads only after click and unmounts on close. Include desktop Chromium,
mobile WebKit and reduced motion. Do not turn CPU-only measurements into a
claim about browser speed or provider throughput.

## Joint backend acceptance and release boundary

Initial joint verification passed 81 Canvas core, 183 targeted API, 27 worker
Canvas and 1117 Web unit tests plus TypeScript type-check. Three API skips in
that window were two explicitly gated local PostgreSQL lock-order tests and
one separately scheduled ffmpeg media smoke. This is not full-repository Python.

A separate disposable combined API/dispatcher/worker/GET harness with provider
stubs must establish durable admission/recovery, no duplicate tasks and exact
retry closure. Recheck PostgreSQL keyset/lock ordering and Redis TTL/reassignment/
OS-process reaping in a coordinated local window. Preserve failures and cleanup
evidence. Paid providers and production databases are unnecessary.

No dependency installation, commit, push, tag, release or deployment is part of
this checkpoint.

## 2026-10-10 17:47 UTC candidate completion

The subsequently authorized implementation and local acceptance are complete:
RunPlan/History and admission safety, actual local image/edit/video stub chains,
low-zoom touch protection, layout-history visibility, and the reproduced
drag-completion camera jump fix. The final frozen source passed 1,183 Web unit
tests, type-check/lint/build, 18 affected browser regressions, four direct-touch
cases and all six Chromium/WebKit 100/500/1,000-node operation samples. Earlier
failed cohorts remain preserved. See lumen-final-joint-acceptance-2026-10-10.md
and lumen-canvas-scale-acceptance-2026-10-10.md for evidence and limitations.

Remaining release work is original-source compare-and-swap integration against
the protected 51-entry baseline, exact allowlist staging, actual-source checks,
and the separately authorized commit/push/tag-triggered release workflow. Six
unrelated audit artifacts must remain unchanged and unstaged. This checkpoint
does not claim source promotion, successful publication or production deployment.
