# Large File Decomposition Plan

Status: completed for the 3,000-line production-file gate
Audit baseline: 2026-07-11, after native desktop retirement and reliability cleanup

## Final Execution Snapshot

The cleanup completed the mandatory facade decomposition. No production source
file remains above 3,000 lines.

| Original file | Before | Final | Final placement |
|---|---:|---:|---|
| `apps/api/app/routes/workflows.py` | 11,596 | 2,855 | `workflow_services/`, `routes/workflow_routes/` |
| `apps/worker/app/upstream.py` | 8,090 | 2,489 | `app/upstream_parts/`, `upstream_image_requests.py` |
| `apps/worker/app/tasks/generation.py` | 6,686 | 2,922 | `tasks/generation_parts/`, `app/image_artifacts.py` |
| `apps/web/src/store/useChatStore.ts` | 5,537 | 2,891 | `store/chat/` |
| `apps/worker/app/tasks/completion.py` | 4,689 | 2,986 | `tasks/completion_parts/`, `app/image_artifacts.py` |
| `apps/web/src/lib/apiClient.ts` | 4,321 | 2,563 | `lib/api/` |
| `apps/web/src/app/video/page.tsx` | 4,048 | 2,397 | video domain, lifecycle, task and UI modules |
| `apps/api/app/routes/billing.py` | 3,333 | 2,933 | `services/billing/` |
| `image-job/app.py` | 3,305 | 2,198 | runtime config, persistence, payload and artifact modules |
| `apps/api/app/routes/poster_styles.py` | 3,056 | 2,633 | `services/poster_styles/`, workflow sync service |
| `scripts/lib.sh` | 2,940 | 1,902 | `scripts/lib/*.sh` |

New extraction modules are kept below 800 lines where practical. The explicit
exceptions are still below the 1,500-line source budget:
`workflow_routes/model_library.py` (1,450),
`workflow_routes/poster.py` (1,432),
`workflow_services/library_sync_operation.py` (1,055),
`image-job/job_persistence.py` (949),
`completion_parts/context_loading.py` (928), and
`scripts/lib/runtime.sh` (843).

The second pass also removed seven unreachable Web files, 21 unused API client
wrappers, 19 duplicate default exports, the native desktop release workflow,
and the remaining desktop runtime/packaging surface. No production source file
is above 3,000 lines; the largest is `apps/api/app/routes/videos.py` at 2,999.

The remaining 2,000-3,000 line files stay in the inventory below as optional
follow-up work. They no longer block this cleanup because each has one domain,
passes its owning complexity gate, and is below the repository exit threshold.

## Goals

- Reduce files that combine routing, persistence, orchestration, serialization, and UI state.
- Preserve public imports and runtime behavior while modules move.
- Delete confirmed dead code and duplicates before creating new abstractions.
- Make each extraction independently reviewable and reversible.
- Add characterization tests before moving transaction, billing, queue, or streaming code.

## Guardrails

1. Keep compatibility facades at existing import paths until all callers migrate.
2. Move pure types, parsers, serializers, and selectors before stateful services.
3. Do not move a database commit, Redis lease, billing settlement, or SSE publish boundary without a dedicated regression test.
4. Keep new production modules below 800 lines where practical. Route modules should target 500 lines; individual orchestration functions should target 250 lines.
5. Split one domain per change. Do not combine decomposition with behavior changes unless a characterization test first proves the old behavior.
6. Run the owning domain tests plus Ruff/type-check/lint and `git diff --check` after every extraction.

## Inventory And Target Placement

| Priority | Current file | LOC | Target placement | Risk |
|---:|---|---:|---|---|
| 1 | `apps/api/app/routes/workflows.py` | 11,596 -> 10,933 | `app/workflow_services/` and `routes/workflow_routes/` | High |
| 2 | `apps/worker/app/upstream.py` | 8,090 -> 7,948 | `app/upstream_parts/` | High |
| 3 | `apps/worker/app/tasks/generation.py` | 6,686 -> 6,456 | `tasks/generation_parts/` | High |
| 4 | `apps/web/src/store/useChatStore.ts` | 5,537 -> 4,486 | `store/chat/` | High |
| 5 | `apps/worker/app/tasks/completion.py` | 4,689 | `tasks/completion_parts/` | High |
| 6 | `apps/web/src/lib/apiClient.ts` | 4,321 -> 3,966 | `lib/api/` domain modules | High |
| 7 | `apps/web/src/app/video/page.tsx` | 4,048 | `video/_hooks`, `_components`, `_lib` | High |
| 8 | `apps/api/app/routes/billing.py` | 3,333 | `services/billing/`, `routes/billing_routes/` | High |
| 9 | `image-job/app.py` | 3,305 -> 2,986 | `image_job/` package plus ASGI facade | High |
| 10 | `apps/api/app/routes/poster_styles.py` | 3,056 | `services/poster_styles/` | Medium-high |
| 11 | `scripts/lib.sh` | 2,940 | `scripts/lib/*.sh` with a sourcing facade | High |
| 12 | `apps/api/app/routes/videos.py` | 2,936 | `services/video/`, `routes/video_routes/` | High |
| 13 | `packages/core/lumen_core/schemas.py` | 2,752 | `schema_models/` | High |
| 14 | `apps/api/app/routes/storyboards.py` | 2,643 | `services/storyboards/`, route modules | High |
| 15 | `apps/web/src/app/admin/_panels/SettingsPanel.tsx` | 2,606 | settings catalog, editor hook, controls, health | Medium |
| 16 | `apps/web/src/lib/queries.ts` | 2,542 | `lib/queries/` domain modules | Medium |
| 17 | `apps/api/app/routes/conversations.py` | 2,529 | `services/conversations/` | High |
| 18 | `apps/api/app/routes/messages.py` | 2,518 | `services/messages/` | High |
| 19 | `apps/api/app/routes/_apparel_scene_planner.py` | 2,338 | `services/apparel_scene_planner/` | Medium-high |
| 20 | `scripts/install.sh` | 2,291 | `scripts/install/` with an entrypoint facade | High |
| 21 | `packages/core/lumen_core/models.py` | 2,244 | `model_entities/` | High |
| 22 | `apps/web/src/app/admin/_panels/ProvidersPanel.tsx` | 2,227 | provider catalog, editor, health, credentials | Medium |
| 23 | `apps/api/app/routes/admin.py` | 2,157 | `routes/admin_routes/` and admin services | High |
| 24 | `apps/worker/app/tasks/context_summary.py` | 2,127 | `tasks/context_summary_parts/` | Medium-high |
| 25 | `apps/worker/app/video_upstream.py` | 2,106 | `app/video_upstream_parts/` | Medium-high |

Line counts include the reliability fixes in this worktree. Test files are
excluded from the placement priority even when they are larger than production
modules. A separate filesystem scan found no production source file above 1 MB;
the only workspace files above that threshold were generated mypy caches.

## Completed In This Cleanup

- Retired the native desktop application, release workflow, migrations, runtime
  bridges, packaging scripts, desktop-only tests, dependencies, and docs.
- Extracted `video-request-lifecycle.ts` from the video page for request epochs,
  abort handling, object URL ownership, and temporary URL expiry.
- Extracted the shared chat generation render signature used by desktop and
  mobile canvases.
- Extracted chat request identity and conversation/user fencing helpers into
  `store/chat/requestGuards.ts`, with direct tests. The store now guards delayed
  history hydration, SSE completion, polling, regeneration, upscale, reroll,
  inpaint, and active-task restoration against stale cross-conversation writes.
  The same facade now delegates browser image compression to
  `store/chat/imageUpload.ts`, terminal-safe history/poll reconciliation to
  `store/chat/messageReconciliation.ts`, buffered completion deltas to
  `store/chat/completionStreamPatches.ts`, and inactive Base64 cleanup to
  `store/chat/base64Eviction.ts`. Image parameter normalization now lives in
  `store/chat/imageParams.ts`; Wire/SSE payload coercion, structured attachment
  roles, size parsing, timestamp conversion, and billing metadata now live in
  `store/chat/payload.ts`. Backend message adaptation, assistant/tool/memory
  coercion, and tool-call merging now live in `store/chat/messageAdapters.ts`;
  completion lifecycle event reduction now lives in
  `store/chat/completionEvents.ts`. Direct tests cover every extracted module.
  `useChatStore.ts` dropped from 5,537 to 4,486 lines, and the complexity gate
  now reports thirteen removed violations without raising any allowance.
- Extracted the pure showcase template labels, scene requirements, render/pose
  directions, composition, and framing policy into
  `routes/_showcase_template_policy.py`. Model age, height, gender, diversity,
  regional-style, and compact direction policy now lives in
  `routes/_showcase_model_policy.py`. Cursor, request-key, storage-path,
  datetime, collection-coercion, and bounded GPT-5.5 reference serialization
  now lives in `workflow_services/serialization.py`. `workflows.py` keeps
  direct compatibility imports while dropping from 11,596 to 10,933 lines;
  focused tests guard template policy, model policy, cursor round trips,
  sanitization, and reference-size limits.
- Extracted deterministic image request normalization, transparent-matte
  shaping, image-job payloads, Responses body construction, retry cache
  busting, inpaint wrapping, and size parsing into
  `apps/worker/app/upstream_image_requests.py`. `upstream.py` keeps call-time
  policy/hooks and thin monkeypatch-compatible facades, including the retry
  ContextVar and response validator. The facade dropped from 8,090 to 7,948
  lines, with 136 focused upstream tests passing in the main worktree.
- Extracted generated-image decoding, metadata validation, alpha inspection,
  blurhashing, PIL/libvips variant creation, and image artifact dataclasses into
  `apps/worker/app/image_artifacts.py`. `generation.py` keeps identity-compatible
  aliases plus late-bound variant wrappers for existing monkeypatch and process
  pool behavior, while `completion.py` reuses the shared leaf helpers directly.
  The generation facade dropped from 6,686 to 6,456 lines; 74 focused tests,
  Ruff, mypy, and process-pool pickling checks pass.
- Extracted poster-style library contracts, labels, filters, CRUD, preset sync,
  generation jobs, and auto-tag requests into `lib/api/posterStyles.ts`.
  `apiClient.ts` preserves its named export surface through a compatibility
  re-export and continues to use the shared `apiFetch` implementation. The
  facade dropped from 4,321 to 3,966 lines.
- Extracted bounded request parsing into `image-job/request_bodies.py` and
  pinned-download SSRF protection into `image-job/image_url_security.py`.
  `image-job/app.py` dropped from 3,305 to 2,986 lines, while the installer now
  copies every runtime module and the existing monkeypatch surface remains
  compatible.
- Extracted `StoryboardMediaFrame.tsx` from `StoryboardPages.tsx`, replacing
  four duplicated image/empty-state branches with one authenticated dynamic
  image component. The page dropped from 1,102 to 1,078 lines and no longer
  emits raw `<img>` lint warnings.
- Moved release manifest verification into `scripts/release_manifest_guard.py`
  and privileged update/restore execution into constrained root runners.
- Isolated poster and apparel library lease helpers while adding bounded
  traversal, bounded downloads, atomic indexes, and cross-process locking.
- Characterized large-file behavior with workflow, billing, video, update,
  storage, task lease, outbox, and URL-security regression tests.

## Phase 0: Characterize And Delete

- Keep the completed native desktop deletion separate from responsive Web components whose names contain `Desktop`.
- Remove repository-dead private functions and their imports.
- Consolidate exact duplicates:
  - generation render signatures in desktop/mobile chat;
  - API video reference atomic writes;
  - workflow/poster-style file writes and MIME helpers;
  - worker response-text parsing through `lumen_core.vision_tagging`;
  - image-job URL security through a shared package or vendored parity tests.
- Add tests for currently fragile behavior:
  - task terminal-state monotonicity;
  - composer draft preservation;
  - settings save revisions;
  - workflow pagination;
  - billing credential-specific usage;
  - video cancellation event semantics;
  - update/storage request serialization.

## Phase 1: Pure Modules

### Web chat store

Create:

- `store/chat/types.ts`
- `store/chat/adapters.ts`
- `store/chat/history.ts`
- `store/chat/sseReducer.ts`
- `store/chat/generationSlice.ts`
- `store/chat/composerSlice.ts`
- `store/chat/asyncActions.ts`
- `store/chat/createChatStore.ts`
- `store/chat/index.ts`

Move normalization and pure reducers first. Keep `useChatStore.ts` as the compatibility barrel until selector and action identity tests pass.

### Web API and queries

Extend `lib/api/` with:

- `auth.ts`
- `conversations.ts`
- `tasks.ts`
- `workflows.ts`
- `video.ts`
- `admin.ts`
- `billing.ts`
- `memory.ts`
- `posterStyles.ts`
- `stream.ts`

Split `queries.ts` into matching domains after API exports stabilize. Query keys move first and retain a single canonical definition.

### Core schemas and models

Create domain files under `schema_models/` and `model_entities/`. Existing `schemas.py` and `models.py` remain explicit re-export facades. Add OpenAPI and SQLAlchemy metadata snapshots before moving classes.

## Phase 2: Stateful Services

### API workflows

Create:

- `workflow_services/output_sync.py`
- `workflow_services/showcase_preflight.py`
- `workflow_services/library_sync.py`
- `workflow_services/poster_sync.py`
- `workflow_services/serialization.py`
- `routes/workflow_routes/apparel.py`
- `routes/workflow_routes/library.py`
- `routes/workflow_routes/poster.py`

Extraction order: serializers, file/library helpers, output synchronization, preflight, then route groups. Preserve router order and private test exports during migration.

### Worker upstream

Create:

- `upstream_parts/types.py`
- `upstream_parts/clients.py`
- `upstream_parts/sse.py`
- `upstream_parts/responses.py`
- `upstream_parts/direct_images.py`
- `upstream_parts/image_job.py`
- `upstream_parts/dual_race.py`
- `upstream_parts/dispatch.py`

Move parsers and request builders first. Move dispatch and ContextVar-sensitive code last. Preserve monkeypatch targets in `upstream.py`.

### Generation and completion runners

Create:

- `tasks/generation_parts/{lease,queue,references,postprocess,persistence,workflow_hooks,retry_state,runner}.py`
- `tasks/completion_parts/{context,tool_state,tool_images,stream,persistence,billing,runner}.py`

First extract shared image artifact helpers into `app/image_artifacts.py`. Do not split the main runners until lease, retry epoch, DB commit, outbox, SSE, and billing boundary tests exist.

### Billing

Create route modules for user views, pricing, redemptions, wallet administration, and overview. Create service modules for redemption, pricing import, and wallet queries. Move read-only conversions first; move monetary mutations last.

## Phase 3: Product Surfaces

### Video

Move pure reference codecs, pricing, and prompt enhancement into `_lib/`. Then create hooks for options, generation, history, and SSE. Extract task drawer, preview dialog, composer, and history components last.

### Settings

Split into:

- `settings/catalog.ts`
- `settings/useSettingsEditor.ts`
- `settings/SettingControls.tsx`
- `settings/SettingsHealth.tsx`
- `settings/selectors.ts`

The editor hook must use revision-aware saves so edits made during a request are not cleared by an older response.

### Image job

Create `image_job/{config,security,db,schemas,extract,upstream,worker,api}.py`; leave `app.py` as a small ASGI entrypoint. Preserve standalone deployment and add parity tests before replacing its URL security implementation.

### Shell operations

Split `scripts/lib.sh` into logging, environment, locks, runtime, releases, Compose, update channel, and safety modules. Update self-update file lists before sourcing new files. Validate every shell file with `bash -n` and shellcheck.

## Refactor-Blocking Bugs

The blockers found in the baseline audit have been resolved. Keep their
regression tests in place before moving the owning code:

| Blocker | Status | Guard |
|---|---|---|
| Showcase jobs were not durable across API restarts | Resolved | transactional outbox, reconciler, DLQ tests |
| Workflow detail GET wrote data and locked rows | Resolved | read-only route tests |
| Conversation compaction held a DB transaction across an upstream call | Resolved | lock/transaction boundary tests |
| Workflow pagination returned no usable cursor | Resolved | cursor round-trip tests |
| Billing views aggregated the wrong ledger dimensions | Resolved | credential-window ledger tests and migration `0043` |
| Video reference uploads buffered bodies and raced quota checks | Resolved | streaming size/quota tests |
| Update, restore, and storage requests crossed the privilege boundary unsafely | Resolved | constrained request protocol and runner tests |
| Image-job URL validation allowed rebinding and unbounded downloads | Resolved | DNS pinning, redirect, body, and response budget tests |
| Bulk deletion released wallet holds before active workers stopped | Resolved | queued-vs-running cancellation tests |
| Deleted generation images could truncate inspiration-feed pagination | Resolved | visible-image `EXISTS` filter and empty-page cursor test |

## Exit Criteria

- No production file remains above 3,000 lines except a documented temporary facade.
- No route handler performs unrelated domain orchestration.
- Existing public imports continue to resolve during migration.
- Full Python, Web, image-job, TgBot, version, shell syntax, and build checks pass.
- Complexity baselines decrease with each phase; they are never raised to hide regressions.
