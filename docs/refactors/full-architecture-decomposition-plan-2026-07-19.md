# Full Architecture Decomposition Plan

Status: completed
Baseline date: 2026-07-19

## Objective

Finish the repository-wide engineering and architecture governance campaign,
not only another partial extraction.

## Exit Criteria

1. `scripts/architecture-baseline.json` has no cycles and no boundary
   violations.
2. `scripts/complexity-baseline.json` has no oversized production files.
3. Python and Web complexity debt is removed or reduced to the configured
   maximum of 15 without raising either baseline.
4. No production Python/TypeScript source file exceeds 1,500 lines.
5. Existing public import paths remain available through thin compatibility
   facades until all internal callers migrate.
6. Full Python, Web, image-job, shell, architecture, complexity, version, and
   build gates pass.

## Current Baseline

- 0 production files above 1,500 lines.
- 0 Python complexity entries.
- 0 API lower-layer imports from route modules.
- 0 Python dependency-cycle groups.
- Web architecture: zero forbidden edges and zero cycles.

The existing uncommitted first-pass governance work is part of this campaign
and must be preserved.

## Wave 1: Remove Architecture Debt

### A. Core model contracts

Ownership:

- `packages/core/lumen_core/models.py`
- `packages/core/lumen_core/canvas_models.py`
- `packages/core/lumen_core/schemas.py`
- new `model_entities/` and `schema_models/` modules
- directly related core tests

Deliverables:

- Remove the `models <-> canvas_models` cycle.
- Keep `models.py` and `schemas.py` as explicit compatibility facades.
- Bring both facades below 1,500 lines.

### B. API video and submission boundaries

Ownership:

- `apps/api/app/routes/videos.py`
- `apps/api/app/canvas_services/execution_service.py`
- new `apps/api/app/services/video/` modules
- directly related video/canvas tests

Deliverables:

- Remove `canvas_services -> routes.videos`.
- Move option lookup, submission primitives, and reusable serializers into
  services.
- Bring `videos.py` below 1,500 lines.

### C. API workflow policy boundaries

Ownership:

- `apps/api/app/workflow_services/`
- route policy modules named `_apparel_*` and `_showcase_*`
- new workflow domain/policy modules
- directly related workflow tests

Deliverables:

- Remove every workflow-service import from `app.routes`.
- Move policy constants, shot types, templates, and library limits into neutral
  workflow modules.
- Preserve route-level compatibility exports.

### D. Worker provider runtime

Ownership:

- `apps/worker/app/upstream.py`
- `apps/worker/app/upstream_parts/`
- `apps/worker/app/provider_pool.py`
- `apps/worker/app/byok_runtime.py`
- related worker upstream/provider tests

Deliverables:

- Remove the BYOK/provider/upstream cycle through leaf contracts and injected
  runtime callbacks.
- Bring `upstream.py` and `provider_pool.py` below 1,500 lines.

### E. Worker volcano task runtime

Ownership:

- `apps/worker/app/tasks/volcano_assets.py`
- `volcano_asset_actions.py`
- `volcano_asset_create.py`
- `volcano_asset_dispatch.py`
- `volcano_assets_parts/`
- related volcano asset tests

Deliverables:

- Remove the four-module task cycle.
- Replace facade back-imports with an explicit runtime context.
- Keep every production module below 1,500 lines.

## Wave 2: Split Every Remaining Oversized File

Run in parallel batches with disjoint ownership:

1. API billing and storyboard routes.
2. API messages, conversations, tasks, and conversation cleanup.
3. API workflows, poster styles, apparel scene planning, prompts, and admin.
4. Worker generation and completion runners.
5. Worker video generation, context summary, and video upstream.
6. Web chat store, API client, and query facade.
7. Web video/admin/settings/provider/billing/lightbox surfaces.
8. `image-job/app.py`.

Each worker must:

- Move pure contracts and selectors first.
- Keep old paths as thin facades.
- Add or update focused tests.
- Reduce all touched complexity debt.
- Avoid editing architecture/complexity baselines.

## Wave 3: Complexity And Dead-Code Closure

1. Run both complexity scanners.
2. Partition remaining violations by non-overlapping file ownership.
3. Extract decision tables, reducers, serializers, and orchestration stages.
4. Delete confirmed dead compatibility exports after repository-wide reference
   checks.
5. Regenerate baselines only to remove entries; never increase allowances.

## Integration Ownership

The main thread owns:

- `scripts/architecture-baseline.json`
- `scripts/complexity-baseline.json`
- architecture/complexity gate implementation
- shared plan and governance docs
- conflict resolution and compatibility review
- final full-suite validation

Workers must not revert existing uncommitted work and must not edit files
outside their declared ownership unless the main thread explicitly expands it.

## Validation

```bash
uv run ruff check packages/core apps/api apps/worker apps/tgbot image-job tests
uv run ruff format --check packages/core apps/api apps/worker apps/tgbot image-job tests
uv run python scripts/check_architecture.py
uv run python scripts/check_complexity.py
uv run pytest packages/core/tests
uv run pytest apps/api/tests
uv run pytest apps/worker/tests
uv run pytest apps/tgbot/tests
uv run pytest image-job/tests tests
cd apps/web
npm test
npm run lint
npm run type-check
npm run build
```

Final checks also include `python3 scripts/version.py check` and
`git diff --check`.
