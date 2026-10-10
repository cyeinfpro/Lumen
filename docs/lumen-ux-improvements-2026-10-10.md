# Lumen UX improvements, 2026-10-10

## Scope

This document covers the first six approved UX changes only. The later generation
pipeline, asset-readiness, history, RunPlan, billing-snapshot and large-graph work
is a separate phase and is not claimed complete here.

Base commit: 86c55233c76ec345a91c3a226686c3c84a4c72b0.

## Changes

1. Notifications move their stable portal host into the active modal root.
   Existing background inert/focus isolation remains active. Rendered composer,
   mobile navigation and modal footer rectangles drive placement; ResizeObserver,
   visualViewport and safe areas update it after resizing. Nested modal closure
   moves the same host back without resetting notification action/timer state.
2. Normal Canvas input/empty states use theme surfaces and matching foregrounds.
   Readable summaries use the existing AA muted token; disabled tokens are not
   globally changed. Media stages keep their intentionally dark stage and paired
   media foreground.
3. Real Canvas page coverage switches both themes while retaining the actual
   editor element, draft value, selection and viewport transform. The test also
   checks rendered foreground/background contrast and horizontal overflow.
4. Canvas video thumbnails render only an image poster. No poster yields an honest
   click-to-play placeholder. A video player is mounted only after explicit
   preview activation and is unmounted on close.
5. Media UI distinguishes processing, loading, temporary preview failure and no
   available preview. Preview retry reloads media only; it does not create a new
   generation or charge. A replacement source resets failure/playback state.
   Browser onError is never treated as proof of deletion or missing permission.
6. Node buttons, Inspector, command menu and TopBar consume shared run readiness.
   Diagnostics are fenced by canvas ID, revision and semantic graph contents,
   debounced by 250 ms and share one read-only video-options lookup per batch.
   Temporary preflight failure is recoverable. The authoritative run path still
   validates current graph/capabilities, flushes saves and submits with revision.
   Input changes while awaiting validation/save reject stale run intent.

Canvas nodes use opaque normal surfaces without permanent backdrop blur or
hover-elevated shadows. Selection/focus boundaries, the amber brand and 44px
controls remain. No percentage performance improvement is claimed.

## Safety and unchanged contracts

- No API/Worker/storage/provider/billing implementation is changed in this phase.
- Existing SSE recovery, idempotent saves, draft ACKs, conflict recovery and
  multi-user identity cleanup remain in place.
- Browser tests use local fixture responses and block unintended execution.
  They do not send real generation requests or mutate production data.
- Only existing dependencies and browser engines are used. No package installation.
- The original working tree's pre-existing changes are protected by a full tracked
  file SHA baseline and a binary diff copy before any integration.
- No commit, push, release tag or deployment is authorized by this work.

## Validation status

Validated checkpoint at 09:43 UTC. This report covers Phase A only.

- Final aggregate `npm test`: 1060 passed, 0 failed.
- `npm run type-check` and `npm run type-check:full`: passed.
- `npm run lint`: passed with 0 errors and one pre-existing warning at
  `src/app/settings/privacy/page.tsx:402`.
- `npm run build` (Next.js 16.3.8, webpack): passed.
- New real-page UI cases: 27/27 passed across desktop Chromium, phone Chromium
  and reduced-motion phone WebKit, including an explicit 375px readiness case.
- Existing Canvas durability cases: first aggregate run passed 20/21.
  WebKit pagehide/reload once displayed the remote prompt instead of the saved
  local draft. The unchanged case passed on repeat. Its cause has not been
  established, and it is not classified as a pre-existing issue.
- Screenshot/recheck pass: 12/12. Pixel review nevertheless found that an early
  WebKit notification-after-footer-growth screenshot matched a prior failing
  geometry. Its cause is not established; it is not claimed fixed by a retry.
  The test was strengthened to record 45 continuous animation frames, then
  check again after screenshot capture. One diagnostic pass plus three
  independent repeats all passed: 180/180 frames maintained exactly 12px
  clearance, the close target was hittable in every frame, fonts were loaded
  and toast transforms were none. All three repeat screenshots were identical,
  visibly correct, and retained the same 12px gap after capture.
  Production code was not changed to remove Framer animation on speculation.
  The earlier border-box ResizeObserver change remains covered by unit tests.
- The final added diagnostic test was checked again with targeted ESLint and
  the full TypeScript configuration. No product source changed after the
  successful full unit/lint/build gates.

The browser command was `node node_modules/@playwright/test/cli.js test
--config=playwright.lumen-ux-local.config.ts`, with the three new specs and
`e2e/canvas-durability.spec.ts`, using one worker and fixture-only APIs.
The repeat selected `pagehide and reload|pairs both themes|notifications:`.
Readiness's three original cases deliberately use 1440x1000 in every project;
the separate mobile case explicitly uses 375x812.

Local evidence: `tmp/ux-20261010/unit-final2.log`, `type-check-final.log`,
`type-check-full-final.log`, `lint-final2.log`, `build-final.log`,
`e2e-final.log`, `e2e-screenshot-recheck.log`, and
`screenshot-manifest.json`. Twelve persisted named screenshots are under
`tmp/ux-20261010/screenshots/final/`. They contain only fixture data.
The original 3989 tracked-file SHA baseline matched before integration. The
guarded integration selects 30 product/test/document files and rejects any
unexpected original-tree change or new-file collision. Original dirty version
metadata, lockfiles and unrelated evidence are preserved. The authoritative
integration outcome is `tmp/ux-20261010/integration-result.json`; per-file
before/final hashes are in `integration-manifest.json`, with local backups.
No commit or Git staging is performed.

Continuous-frame evidence and corrected WebKit screenshots are in
`tmp/ux-20261010/webkit-frame-repeat-evidence/`, with a compact
`summary.json`; the corrected PNG SHA-256 is
`6d8abb290461d77d1f126b01185769279aabe9daa84acd6b8162ed03c699d0fb`.
Earlier logs/screenshots remain available rather than being overwritten.
The machine had unrelated high Lightroom load during the final diagnostic
checks, so no performance or timing improvement is inferred from these runs.

Browser environment: installed Node Playwright 1.62.1 with explicit existing
Chromium revision 1243 and WebKit revision 2359 executable paths. These are not
relabeled as the driver's default browser revisions. Real execution determines
compatibility; any unrun or failed case must remain disclosed.

The local validation-only Playwright launch-path configuration and temporary
logs are not part of the product patch.
