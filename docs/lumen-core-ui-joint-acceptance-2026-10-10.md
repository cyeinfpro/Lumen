# Lumen core + B1/B2a joint candidate acceptance

Date: 2026-10-10, 13:03 UTC.
Workspace: 520b06121cc249f7b5c67da858426a77.
Base HEAD: 86c55233c76ec345a91c3a226686c3c84a4c72b0.

## Result and scope

The independently reviewed core and accepted B1/B2a UI are jointly integrated
and verified in this new isolated workspace. This is not deployment, promotion
into the original checkout, completed RunPlan/history UI, or full end-to-end
provider acceptance.

Core review found and fixed an additional fail-fast repair bug: an unrelated
blocked branch caused a newly repaired root to be blocked again. Only real
failed/partial_failed/canceled/expired steps now trigger global fail-fast.
Blocked dependencies remain blocked; unselected branches are not restarted.
Pure and persisted SQLite retry-to-dispatch regressions passed.

## Protected integration

- Original checkout's 51 dirty/untracked files, including accepted Phase A and
  existing version 1.2.179 edits, were preserved in the new candidate.
- Core 92 + UI 39: six byte-identical overlaps, 125 unique candidate files.
- Total copied source inventory: 169 files. No source conflict was overwritten.
- Before and after tests, all 169 bytes/hashes matched the integration manifest.
- Original checkout status, tracked binary diff and 51 dirty file hashes stayed
  unchanged. Core and UI acceptance workspaces were not modified by integration.
- No new dependency or browser installation, commit, push, tag or deployment.

Original acceptance manifests are preserved under tmp/lumen-core-ui-joint:
core-accepted-manifest.json and ui-accepted-manifest.json.
The initial integration-manifest.json has SHA-256:
479bd39c71c6f1198653913a6604eca9ae1936979ebdefa9c682167bf4654b6d.
It records source baselines and every phase's before/after hashes.

## Verification

Operation 5bf4af45d4c241bfb3bdabbb50392c6a, 45.545 seconds:
- Canvas core: 81 passed.
- Targeted API: 183 passed, 3 intentionally skipped.
- Canvas worker: 27 passed.
- All Web unit tests: 1117 passed.
- npm run type-check: passed.

The three API skips are two real PostgreSQL lock-order barrier parameter cases
requiring LUMEN_LOCAL_PG_TEST=1 and one explicitly separately scheduled ffmpeg
media smoke. PG/Redis/media stress was not rerun in this joint window. Earlier
core evidence is retained; neither SQLite nor fixtures substitute for those gates.

Operation dfa1ffc62dbc453dbb265a1d4a6b36da, 160.918 seconds:
- Full Web lint passed, with the pre-existing privacy/page.tsx:402 warning.
- Production Web build passed in the isolated .next-joint-build directory.
- Browser startup then failed for all 39 cases because the default Playwright
  revisions 1234/2336 were absent. No application test assertions ran in that
  first browser attempt. Its logs/traces were retained.

The prior UI acceptance document identified the existing installed Chromium
1243 and WebKit2359, used through its verified local executable-path config.
No revision directory was renamed and no dependency was installed.

Operation 72b0d3f6df3e42cbb9a1aebf8129e4dd, 180.516 seconds:
- 33 browser cases passed before the configured global suite limit.
- Remaining six WebKit cases did not finish/run; the operation was not green.
- This was suite timeout, not a failed product assertion.

Operation ca44524faa2e4492a01e66ce502eb0ff, 45.480 seconds:
- Only the six remaining WebKit cases were run; all six passed.
- Combined distinct coverage is 39/39, not a claim that the earlier timeout passed.

Browser coverage uses the actual React/Canvas UI with fixture-backed requests:
desktop light Chromium, touch/mobile dark Chromium, mobile reduced-motion
WebKit. It covers recovery and billing honesty, all unknown-submit run guards,
read-only queries, asset preparation, global SSE gaps/duplicate/cross-tab
recovery, readiness races, theme/draft/viewport preservation and click-only
video playback. It does not exercise a real combined backend or paid provider.

Desktop billing, mobile unknown state and WebKit unknown state screenshots were
visually checked. The final evidence manifest inventories 36 logs/screenshots:
tmp/lumen-core-ui-joint/verification-evidence-manifest.json
SHA-256: 19a3e0d33b157c23368eb1c5cc135f280173ba0fd58b4c8d7d9001f11b955414.

Final git diff --check passed. Port 3127 had no listener after test completion.
The generated .next-joint-build tree was reversibly moved to
 tmp/lumen-core-ui-joint/build-preserved after testing, with its BUILD_ID bytes
verified unchanged, so subsequent ESLint does not scan generated bundles.
No ignore rule or governance threshold was changed.

## Next work

See lumen-generation-joint-next-steps-2026-10-10.md for implementable contracts
and tests. Remaining: typed RunPlan preview/start/repair control state machine;
bounded history pagination/comparison and exact pinned draft branching;
visible freshness and preparation retry; real 100/500/1000-node browser
measurements; and disposable joint API/dispatcher/worker/GET recovery testing.
Full repository Python and release gates remain outside this acceptance.
