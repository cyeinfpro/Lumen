# Lumen history and preparation review checkpoint

Date: 2026-10-10. Workspace: `520b06121cc249f7b5c67da858426a77`.
This is an implementation checkpoint, not browser acceptance or release approval.

## Implemented

- On-demand execution history requests use limit 30 and opaque cursors.
  The active page is bounded to 30 rows; previous cursor metadata is capped at
  100 pages. Identity epoch, canvas, node and cursor form the query key.
  Abort/generation fences reject delayed responses.
- Immutable A/B execution snapshots persist across paging and page retries.
  Changing identity, canvas or node clears selections. Comparisons include
  saved config/input/output identity, hashes and processor version; missing
  legacy snapshots and bounded/truncated comparisons are explicitly labelled.
- Historical branching reconstructs saved config and literal text/assets.
  Generated inputs pin the saved execution and exact output index.
  Missing source nodes, provenance, hashes, output indexes or compatible ports
  block the action. It creates only draft graph operations, in one undoable
  transaction. Current inputs and active output selections are never substituted.
- Historical merged text with leading/trailing whitespace is reconstructed
  through a literal plus a no-trim merge, preserving the resolved text.
- Saved execution freshness is suppressed for local edits/revision mismatch,
  older selection merges, and successful output-selection acknowledgements.
  Acknowledgement callbacks also recheck the original identity epoch.
  Workspace autosave integration clears old freshness on saved graph updates.
- Video preparation retries are separate from paid generation. The request
  carries saved source SHA-256, preparation revision and stable semantic
  idempotency key. Unknown provenance or nonfailed states are rejected.

## Verification completed

1. A combined focused run passed **86 tests**, including existing store,
   document merge, semantic POST callers, query refresh and generation
   normalization tests, plus new history/pinned-branch/request cases.
   CodePier operation: `e1dd09667a824c02ada63a81255b1d73`.
2. After the final selection-acknowledgement freshness fix, its **8 request
   tests passed**, including the two new success/identity-transition cases.
   This is a focused recheck, not another full 86-test run.
   CodePier operation: `c3afb76e00824eee9c5406888cbcb9c5`.
3. Focused ESLint passed, including the new browser spec and final query fix.
4. Theme scan of the touched inspector/history UI found no prohibited dark
   utilities. `git diff --check` passed before the final narrow query/test/doc
   changes and is rechecked for this checkpoint.
5. An initial TypeScript check passed. Later joint type-check, whole-project
   lint/build and real-browser results belong to the parent acceptance window.
6. The new history functions passed the repository complexity check; the check
   at that time reported only a separate RunPlan reducer violation.

## Real-browser fixture prepared, not executed here

`apps/web/e2e/canvas-history-review.spec.ts` uses `installAgentFixture` and
the existing `playwright.lumen-ux-local.config.ts`. The existing configuration
covers desktop Chromium/light, phone Chromium/dark, and phone WebKit/reduced
motion. No browser/build or dependency installation was started for this spec.

Three real React scenarios are prepared:
- Bounded paging, failed-page retry, immutable cross-page A/B, and node change.
- Exact saved config/text/asset/pin mutations, one undo, zero execute requests.
- Preparation double-click fencing, exact hash/revision/idempotency request,
  zero generation requests, and no video decoder/binary request.

These are fixture-backed UI assertions, not production/provider performance
or a claim that the browsers already passed.

## File inventory

New:
- `apps/web/src/lib/api/canvasHistory.ts`
- `apps/web/src/lib/canvas/executionHistory.ts`
- `apps/web/src/lib/canvas/historicalBranch.ts`
- `apps/web/src/components/ui/canvas/CanvasHistoryPanel.tsx`
- `apps/web/src/components/ui/canvas/CanvasPreparationStatus.tsx`
- `apps/web/src/lib/canvas/executionHistory.test.ts`
- `apps/web/__tests__/canvas-history-requests.test.mjs`
- `apps/web/e2e/canvas-history-review.spec.ts`
- This checkpoint document

Modified within history scope:
- `apps/web/src/lib/canvas/types.ts`
- `apps/web/src/lib/canvas/store-types.ts`
- `apps/web/src/lib/canvas/store.ts`
- `apps/web/src/lib/canvas/documentMerge.ts`
- `apps/web/src/lib/api/canvases.ts` (optional freshness/output normalization)
- `apps/web/src/lib/queries/canvases.ts` (selection identity/acknowledgement fence)
- `apps/web/src/components/ui/canvas/CanvasInspector.tsx`
- `apps/web/src/components/ui/canvas/CanvasInspectorExecutionHistory.tsx`
- `apps/web/src/lib/api/semanticPostCallers.test.ts`
- `apps/web/__tests__/canvas-generation-normalization.test.mjs`

Parent-owned integration:
- `CanvasWorkspace.tsx` clears freshness after autosave; history worker did not
  edit this file.

## Remaining acceptance

- Run the prepared UI scenarios in the coordinated desktop/mobile WebKit window.
- Recheck final integrated TypeScript, lint, complexity, build and broader tests.
- History browsing deliberately caps the current session at 100 pages and the
  UI reports that limit. Very old records beyond that window are not reachable
  in the current implementation.
- No remaining known safety blocker was found in the reviewed branch, identity,
  CAS or preparation paths after the final acknowledgement fix. Browser
  execution remains necessary to establish the real mounted UI behavior.

No commit, push, tag, release, deployment, production write or paid provider
execution is included in this checkpoint.
