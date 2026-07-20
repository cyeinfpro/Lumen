# Architecture Governance

Status: completed on 2026-07-19

## Goals

- Keep application packages acyclic where compatibility debt has already been removed.
- Prevent lower layers from acquiring new dependencies on route or UI implementation modules.
- Move shared contracts and policies into neutral modules with compatibility facades at old import paths.
- Ratchet existing debt down instead of expanding a permanent allowlist.

## Enforced Dependency Direction

### Python

- `lumen_core` must not import application packages.
- API `services/`, `canvas_services/`, and `workflow_services/` must not add new imports from `routes/`.
- Worker lower-level services and upstream helpers must not add new imports from task entrypoints.
- Application packages must not import another application package directly.
- New package dependency cycles are forbidden.

The gate is:

```bash
uv run python scripts/check_architecture.py
```

`scripts/architecture-baseline.json` is currently empty: no Python cycles or
boundary violations remain grandfathered. Future changes may only keep that
baseline empty; adding a violation or cycle fails the gate.

### Web

The intended top-level direction is:

```text
app -> components -> hooks/store/lib
```

- `lib/`, `store/`, and `hooks/` cannot import `app/` or `components/`.
- `components/` cannot import `app/`.
- Production source files cannot form dependency cycles.
- Compatibility facades may remain in the old higher-level path while the
  implementation lives in the neutral lower-level module.

The gate is:

```bash
cd apps/web
npm run check:architecture
```

Unlike Python, the Web gate starts with zero grandfathered violations and zero
cycles.

## Changes In This Pass

- Extracted provider configuration loading, parsing, and proxy validation from
  `routes/providers.py` into `services/provider_config.py`.
- Extracted the admin model TTL cache into `services/admin_model_cache.py`,
  removing the `admin_models <-> providers` route cycle.
- Moved lightbox, inpaint, and navigation contracts into `src/lib`, leaving
  thin compatibility facades in the prior component paths.
- Moved authenticated query-key and cache-scope policy out of
  `components/QueryProvider.tsx` into `lib/queries/userScope.ts`.
- Moved reusable video option/request lifecycle logic out of `app/video` into
  `lib/video`, and moved the conversation compaction hook into `src/hooks`.
- Split `VideoProviderKind` into a leaf type module, removing the
  `lib/types.ts <-> lib/videoAssetTypes.ts` cycle.
- Wired both architecture gates into the repository test/lint entrypoints.

## Completed Decomposition Order

1. Extract video option lookup from `routes/videos.py` so
   `canvas_services/execution_service.py` no longer imports a route.
2. Move message/video task creation behind application services so
   `services/task_submission.py` depends on service contracts rather than route
   helpers.
3. Move apparel/showcase policy types and constants out of `routes/_*.py` into
   workflow domain modules.
4. Separate canvas ORM entities from the `lumen_core.models` compatibility
   facade to remove the core model cycle.
5. Introduce explicit worker provider/runtime interfaces between BYOK,
   provider-pool, upstream transport, and failover modules.
6. Replace volcano asset task back-imports with a runtime context or injected
   operation interface.

Each follow-up should remove its exact baseline entries in the same change and
must keep existing public import paths working until all callers migrate.
