# Lumen CI recovery acceptance — 2026-10-11

## Scope and release state

This supplements the 2026-10-10 joint acceptance. The inspected source and remote main were `9cee548ac1034140e3ceaf902b28db0af79d8640`. The four previously accepted final UX files were staged but not committed. Six excluded original audit files remain protected.

The isolated candidate has passed the joint validation recorded below and is accepted for guarded source promotion. This document does not assert a completed commit, publication, or deployment. Main-image workflow success is not a formal version release.

## Exact failed CI

CI run `38075226040`, frontend job `114280822984`, reported 33 failed, 1 flaky, 80 skipped, and 719 passed. Failures were real browser assertions, not established storage-budget failures.

- Project/prompt deletion tests used a dialog-wide alert locator that matched both the retained inline error and the intentionally dialog-owned notification. Narrowed to the inline paragraph and strengthened exact error text; retry/cancel/draft assertions remain.
- Tablet inspector fixtures treated only widths below 768 as overlays, but the workbench docks its inspector at 1200. Helpers now exercise the actual compact inspector through 1199.
- The no-touch phone fixture used tap. It now selects real tap or click according to the project's input capability.
- Canvas pending/error early returns omitted mobile session recovery controls. A shared load-state shell retains the existing recovery control and safe-area spacing.
- A short landscape Agent composer could leave less than a usable notification height. Its textarea now caps growth only under the short-landscape media condition, retaining internal scrolling and the full draft.
- The landscape camera delta occurred while the pointer was still down at the viewport edge. Geometry: viewport y=101, height=162, grip center y=123.65; translation changed by 1.87px during the drag and was stable at pointer-up. Central-drag setup now uses a real Space-pan gesture and keeps exact camera equality assertions; a separate edge-auto-pan completion/undo regression preserves intentional edge movement.
- Studio's native disclosure has no custom Space handler. The fixture now waits for the default conversation response, the live responsive shell, and the hydrated/enabled welcome control before the unchanged Space/Enter/Space assertions. Responsive remount timing is a source-supported hypothesis, not a proven browser-engine defect.

## Recorded local evidence

- Canvas recovery before fix: `3a18118fdb7c44c89ec0233ff7eca623`, two exact pending-shell failures.
- Recovery controls after fix: four 320/375 degraded/unauthorized cases passed within `18b3ab0d16ac49f78983e3cf5ae129e6`; that combined operation still failed the preserved landscape camera assertion.
- Camera geometry extraction: `a686a246bdcc45afbc4ce54a0c470407`.
- Central-drag and edge-completion/undo landscape regressions: `e7e4e112b6674d6d9cf062dcc4e40384`, 2 passed.
- Canvas contracts: `3a15548adee54146a6d9a0d4740eab60`, 22 passed.
- Notification initial correction: `0758068ace824006adefee3735cce523`, 4 passed. The final extra 8px clearance and full-row containment assertion require the later joint matrix.
- Notification final static gate: `356586530dd343eba76a1ec6c8c66853`, 8 unit tests, targeted lint, and diff checks passed.
- Studio readiness static gate: `c30dca264ad246fe896658d49c922b3c`.

The 119-case seven-viewport matrix `0717eeb00fbe4b8b9700cf5ab68f84cb` finished in 16.6 minutes: 107 passed, 2 failed, 10 existing mobile-only matrix skips. The two failures were the strengthened real auth-request assertion. All 1069 recorded source entries stayed identical throughout the run (`ac91fe7049cf415b9927000b26da7341`; inventory SHA `959badcbe99c106cdf9717cc085f7145e589006e7b190e6ff971d2179e1ae057`). The final 264px composer allowance and complete-toast containment passed across all seven viewports; the 700×320 screenshot was visually inspected.

## Additional identity recovery defect and verified correction

Initial auth failure enters fail-closed state and resets the me query. This clears query data/error while retaining the handled failure. The bootstrap guard then incorrectly treated this state as never initialized and suppressed both manual and automatic revalidation. The observed three requests were transport attempts (initial plus two retries), not three recoveries.

- The original hook was tested through a real QueryClient/QueryObserver and transport. After a genuine reset/rerender, manual and automatic recovery each failed with 3 requests instead of 4. Offline, hidden, public-route, and unauthorized fences passed. Red operation: `e1b2053d310a418499ad0c495d8dab33`.
- The guard now recognizes the already-handled initial failure while preserving fail-closed identity, terminal/visibility/offline/public/in-flight fences. Seventeen identity tests, lint, type-check, and diff-check passed: `c0efa7e727b04265a5d66255cb96b6e6`.
- Real browser operation `48684e19301f40f28dd56b3e977e2d3c` then proved new GET requests but exposed focus loss because the recovery control unmounted during revalidation. Its two failed focus assertions are retained.
- Explicitly requested recovery now keeps the same focusable control mounted with polite busy feedback and an aria-disabled action guard. Initial/background verification remains quiet; no automatic spinner, realtime outage banner, or duplicate recovery action was introduced. Seven presentation/security tests, lint, and type-check passed: `7faa1b132d384b79bb91d475fb050909`.
- Final browser operation `b8d9dc9142b4483cbc2fafee06508c6b`: 10 passed, 2 existing mobile-only skips. It covers real Video/Canvas auth retries with focus retained, unauthorized login routing, Agent idle/session-switch recovery, and unchanged native Studio Space/Enter behavior on 320, 375, and no-touch keyboard phone profiles.

These are successive, separately identified test runs, not a claim that the earlier 119-case run was wholly green on the final source. Final full frontend operation `09c38d61d5144b329c2ad14b0795d743` passed 1196/1196 tests with zero skips, type-check, the complete lint gates, and the production build. The older render-mock contract was updated to model the newly stateful recovery control and additionally asserts busy feedback and blocked duplicate actions; its original mobile/desktop assertions remain.

The first aggregate attempt `5903f6185b034125859f65bd172db0bf` stopped with local ENOSPC before type-check/lint/build. No cache, source, screenshot, log, or release artifact was deleted. Work resumed only after available space recovered, with per-stage capacity checks. The subsequent legacy-mock failure `0ee9c184e7a64d9eb0e5587571fbc3e1` is also retained. The successful final gate had 13.5–18.7GiB available at its recorded stage checks. Exact source verification and the new commit\'s complete CI remain required before formal release.

## Evidence boundaries

The six-cell scale cohort 7 remains evidence for its recorded source hash and environment. This follow-up does not claim a new 100/500/1000 performance cohort or a performance SLA. Earlier failed samples and logs are retained.

The source promotion must compare the full accepted file inventory, existing staged blobs and modes, excluded audit hashes, branch/HEAD, and exact final staged allowlist. A source-exclusive write window is required; filesystem read/replace checks do not claim an operating-system transaction against arbitrary concurrent editors. Unknown operation outcomes require durable-state and full-file/index inspection, never blind replay.
