# Lumen generation and Canvas joint acceptance

Checkpoint: 2026-10-10 17:46 UTC. Candidate release 1.2.179, isolated workspace
520b06121cc249f7b5c67da858426a77, base
86c55233c76ec345a91c3a226686c3c84a4c72b0. The frozen candidate passed the final six-cell scale
acceptance. Original-source promotion and publication remain pending at this checkpoint.

## Implemented scope

- Typed RunPlan preview/start/repair, revision-bound preview invalidation,
  explicit budgets and output choices, immutable owner-bound idempotency,
  acknowledged/ambiguous recovery, and authoritative execution freshness.
- Transactional admission protection against overlapping active or uncertain
  work, including original-intent replay without a new charge. Different nodes
  and tenants retain their intended admission behavior.
- History pages of 30, bounded pagination and response identity fences,
  persistent A/B comparison snapshots, exact historical pinned branches and
  one-step undo without replacing current input configuration.
- Preparation retry, selection freshness, exact planned candidate ordinal in
  worker/API reconciliation, and real local image/edit/video execution chains.
- Low-zoom WebKit compatibility-click protection, real media/keyboard access,
  and conservative viewport recovery for layout undo/redo that would otherwise
  leave the entire restored graph offscreen.

## Real camera defect reproduced and repaired

The existing auto-fit effect ran again whenever activeInteractionCount returned
to zero. For canvases with at most 200 nodes, a completed drag unexpectedly
replaced the user's zoom and translation with an overview. A new real WebKit
regression reproduced scale 1.15 becoming 0.096084 after the move was saved:
operation c06d7878702d4b3b99135f02ac1b447d, original failure and trace preserved
under tmp/lumen-core-ui-joint/autofit-before-artifacts.

Auto-fit now tracks a pending layout identity: initial canvas/viewport,
node count, fullscreen and compact-layout changes. A gesture only defers an
already pending layout request; its completion does not create a new request.
The existing 200-node automatic-fit limit remains. A live interaction check and
request-identity fence protect the delayed callback. Explicit fit commands and
layout-history viewport recovery remain independent.

Ten new pure-state regressions, full type-check and focused ESLint passed in
44ab912a67654c93b95052e077d1d6aa. Eighteen real browser cases passed across desktop
Chromium, phone Chromium and reduced-motion mobile WebKit in
711f39d5d2ae4e0286b4d94a13c1d6f5 (183.6 seconds): initial fit, repeated real
mouse-pointer drags and undo with unchanged camera, keyboard and toolbar
layout undo/redo retaining visible nodes, config-only undo, and recovery/billing
facts. The mobile drag assertion uses a mouse pointer, not native finger drag.

## Existing generation safety evidence

- Four real PostgreSQL concurrency/lock-order cases passed in
  4e66a62db9e44a8b85417d2fb2046de8 using isolated local schemas.
- Final affected backend suites passed without skips in
  93b847d3bdc045f8ae0bdf5e58c45e82: API 216, core 92, worker 36, with PostgreSQL,
  Redis lifetime and local ffmpeg paths enabled.
- Image/edit and video acceptance use actual API routing, admission, persistence,
  dispatcher and production worker code, then actual GET/artifact/wallet checks.
  External provider ports are deterministic local stubs; synthetic authentication,
  SQLite test adaptation and manually driven transport remain explicit substitutes.
  See lumen-stub-chain-acceptance-2026-10-10.md and
  lumen-video-stub-acceptance-2026-10-10.md. No paid provider was invoked.
- RunPlan/History 24 browser cases passed in 6d74f87802204bb790adcfe5782c0b40.
  The wider 75-case run ef336de768ad49e084a8f66d71c260e4 recorded 74 passes and one
  first-layout selection-precondition failure. Trace showed the initial fit moving
  the node between pointer targeting and click. The facts suite now waits for the
  actual non-identity initial viewport and verifies selection before opening the
  inspector; all nine cases passed in e707548799a746a3a5f09ad843f7481d and again in
  the post-camera-fix 18-case run. Original failures are preserved, not relabelled.
- Final touch guard source passed all four original-overview WebKit/direct-media
  cases in 5caf52200315465e88509806817e2565 before the camera change. The same four
  cases passed again after the final camera change in
  573168c3f3554323a6cde818ba3bf63e, 17:20:11–17:21:13 UTC.

## Final frozen product Web gate

Operation 4f0e9ea2f75948fab96da7f714f0bf1e completed successfully in 114.4 seconds:
1,183 Web unit tests with zero skips, full type-check, all architecture/UI/layout
and ESLint gates, production build, and git diff --check. The pre-existing
privacy-page navigation advisory remains one warning, zero errors. Logs are
preserved as tmp/lumen-core-ui-joint/autofit-web-{tests,types,lint,build}.log;
build output is isolated under autofit-build-preserved. No product source was
modified between this gate, the final touch rerun and the final six-cell sampler.

## Scale and release status

The performance report preserves each script/product cohort and the runner's
actual terminal status. Ten operation rounds and five loads are observations,
not a fabricated performance SLA. Earlier 1,000-node raw self-check completion
followed by a 240-second runner timeout remains failed as a whole test. The final
cohort uses a 360-second observation budget, unchanged functional assertions,
real undo-based per-round position restoration outside timing, and separate
Playwright terminal evidence. Native touch selection is measured; mouse-pointer
pan/drag must not be described as native finger pan or pinch.

The initial-load click race is a retained boundary: these recovery-facts checks
start after actual initial viewport layout, not during its first two frames.
The repaired drag-completion camera jump is a separate demonstrated defect.

Final operation 43d91a31e0a7431e8994ba070698dc68 passed all six Chromium/WebKit
100/500/1,000-node cases from 17:23:06 to 17:41:54 UTC (1,128.25 seconds).
Independent Playwright terminal results confirm 6 passed, with 30 loads, 660 timed
actions, 60 exact drag-undo restorations and 60 visible layout-undo checks;
page errors and prohibited requests were both zero. All six final screenshots
were visually inspected. The 1,034 source-file entries were identical before and
after. Their path-sorted, compact canonical JSON SHA-256 is
d1a451e496a9c6578db579811e39efc111a8b852e116660707068f67be254fd3;
snapshot wrapper files have different timestamps and therefore different hashes.
See lumen-canvas-scale-acceptance-2026-10-10.md for distributions, environment
pressure and preserved prior failures. These development-server observations
are not a production performance certification; the maximum observed Chromium
Long Task was 851 ms.

Original 51 dirty entries remain protected; six unrelated audit artifacts are excluded.
Source integration and release must independently pass their remaining gates.
See lumen-release-scope-2026-10-10.md for the reviewed governance prerequisite,
exact staging rules and the existing tag-triggered formal release requirements.

## Original-source verification and browser entrypoint correction

The protected original source was CAS-integrated with exactly 230 reviewed
staged paths; the six unrelated audit artifacts remained unchanged and unstaged.
Operation 8d9e7fea9ad64cc0a756cf219e3ca950 then passed actual-source version sync/check,
repository integrity, full Ruff, the real local image and video stub chains,
1,183 Web unit tests, type-check, lint and production build. Every source and
staged blob matched the accepted manifest after testing. Commit
496fb20abd9f486f4d6f56cbbce0cdc0e2fe788d was normally pushed to main.

Inspection during the first main CI identified a collection integration defect: its ordinary seven
Chromium projects also discovered the dedicated scale/media and native-touch
specs. The former requires an explicit local video fixture; the latter requires
touch capability absent from desktop projects. The default configuration now
excludes only those two dedicated files, while their own configuration explicitly
opts back in. Per-project ignore rules retain the existing Agent live-test policy.
Three configuration-contract tests prevent inherited exclusions from silently
removing dedicated acceptance or ordinary regressions.

Operation 132fdf57089a4bef98d67e3c2f8b13a1 passed 1,186 Web tests, full type-check,
lint and real Playwright collection checks. The ordinary collection changed
from 882 to 833 cases: exactly 49 dedicated project mappings were removed,
with every ordinary case unchanged. Dedicated scale and touch still collect
exactly six and four cases. Product source, fixture data, browser assertions,
round counts and effective dedicated workloads were not changed. The earlier
six-cell timings remain observations of the unchanged product, not a fresh
measurement under a different test script. Formal release still requires the
corrected commit's remote CI and the tag-triggered publication to finish.
