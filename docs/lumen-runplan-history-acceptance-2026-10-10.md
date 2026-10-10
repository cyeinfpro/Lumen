# Lumen RunPlan and History joint acceptance — 2026-10-10

## Implemented in the isolated joint workspace

RunPlan has typed preview/admission/repair APIs and UI, explicit integer admission budget and output-index choices, revision/full-graph/form invalidation, save-and-repreview before admission, and capability/price hash revalidation. Unknown submissions retain a durable semantic key plus the immutable original request. Recovery queries the owner-scoped intent endpoint; only an explicit action can replay that same body/key. Identity epochs fence responses. SSE/snapshot updates refresh a selected run without overwriting a newer preview or unresolved intent.

The backend rejects overlapping active or uncertain executions while holding the existing user/canvas transaction locks. It checks owner-task recovery instead of trusting a terminal projection alone. Other nodes and tenants remain independent. Repair exposes only latest, confirmed failed attempts with no usable output and surfaces incomplete fail-fast node sets.

History now loads 30 entries per page, fences identity/node/cursor responses, retains immutable A/B snapshots, and reconstructs an exact historical draft branch with pinned execution/output provenance. One transaction undo removes the branch without changing current inputs or executing anything. Authoritative freshness is discarded when graph/revision/selection context changes; preparation retry binds saved source hash and revision and fences double clicks. See lumen-history-review-2026-10-10.md for bounds and detailed branch semantics.

## Verified results

- Isolated PostgreSQL schema: 4 passed, including concurrent same-node/different-key admission, exact-key replay, different-node and tenant independence, and existing lock-order regression. Test schema removed by its own teardown; no user tables modified.
- Joint canvas API suite: 202 passed, 5 conditional skips. Four PostgreSQL cases passed in the separate run above; one media-tool conditional remains skipped.
- Web unit suite: 1148 passed.
- Web typecheck, lint and production build passed. Lint retains one pre-existing privacy/page.tsx warning. Build tree preserved under tmp/lumen-core-ui-joint/runplan-build-preserved.
- Focused Python lint, frontend lint, theme governance and complexity checks passed during development.
- 24 browser operations passed in 2.2 minutes: desktop Chromium light, phone Chromium dark, and phone WebKit reduced motion. Cases include preview invalidation/repricing, double clicks, lost acknowledgement, offline reload and exact-key replay, repair filtering, paginated history retry, A/B isolation, exact pin/literal branch undo, and preparation retry.
- Desktop and both phone RunPlan screenshots inspected. Modal content scrolls inside safe-area bounds with persistent footer and shared Select primitives. Screenshots and logs live in tmp/lumen-core-ui-joint/runplan-history-browser and runplan-history-browser.log.

## Evidence operations

PG: 4e66a62db9e44a8b85417d2fb2046de8.
API: 9dd7be65e047478c96bc3d306e680fde.
Final Web unit/typecheck/lint/build: 83b6e59b6ed645b399d70a81b8a0f811.
Final browser: 6d74f87802204bb790adcfe5782c0b40.

## Explicitly unfinished

The browser cases use local API fixtures. They do not establish a real API → dispatcher → worker → GET end-to-end result. A disposable local provider stub harness is still required. Actual 100/500/1000-node browser distributions and operation coverage are also still required. The previous 39 browser fixtures were not rerun in this stage. No paid provider was used, and no deployment, commit, push or original-source integration has occurred. This is an accepted isolated functional stage with remaining work, not final product acceptance.

The prior accepted 171-file checkpoint remains intact. A new source manifest records this stage separately; original-source promotion requires coordinated SHA comparison.
