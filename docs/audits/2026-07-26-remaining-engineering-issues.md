# Lumen Remaining Engineering Issues Audit

Date: 2026-07-26

## Scope

This document records the remaining issues found during the architecture and engineering review.

The latest reviewed baseline was:

- Commit: `5f0bf31de03162f0a4cd43cd740136577ffea23c`
- Focus areas:
  - module decomposition
  - runtime coupling
  - workflow architecture
  - Worker runtime lifecycle
  - upload/recovery reliability

## Executive Summary

The repository has completed significant physical decomposition work, but several areas still contain transitional architecture.

Current state:

- Static dependency direction: good
- File size governance: good
- Runtime ownership: partially migrated
- Domain boundaries: incomplete
- Compatibility layers: still active

The next phase should continue migration instead of adding more facade layers.

---

# P0/P1 Remaining Risks

## 1. Workflow architecture is only partially migrated

### Problem

A new workflow application/domain design exists, but production routes still depend heavily on legacy workflow services and compatibility exports.

Risks:

- business rules remain split between routes and services;
- difficult to test workflows independently;
- future changes require touching multiple layers.

Recommended migration:

1. Move submit/query/cancel/reconcile into application services.
2. Keep routes as HTTP adapters only.
3. Remove compatibility exports after all callers migrate.

---

## 2. Generation Ports are explicit but still too large

### Problem

The previous global dependency container was replaced by explicit ports, but some ports still behave like a dependency bag.

Risks:

- weak ownership boundaries;
- difficult mocking;
- unclear lifecycle responsibility.

Recommended split:

- GenerationDomainPorts
- GenerationPersistencePorts
- GenerationQueuePorts
- GenerationBillingPorts
- GenerationEventPorts
- GenerationProviderPorts

---

## 3. Completion runtime has similar dependency aggregation risk

The Completion runtime should follow the same decomposition pattern:

- context
- tools
- persistence
- upstream
- billing
- events
- retry

Avoid recreating a single large runtime object.

---

# Medium Priority Issues

## 4. Compatibility facades need retirement tracking

Compatibility exports are useful during migration, but every facade should have:

- owner module;
- migration status;
- removal condition;
- test coverage.

Do not allow permanent compatibility layers.

---

## 5. Runtime state ownership needs continuous enforcement

All module-level state should have:

- explicit owner;
- lifecycle hooks;
- shutdown behavior;
- concurrency model.

New runtime state should fail CI unless registered.

---

## 6. Workflow tests should stop mocking private route exports

Tests should patch application/service ownership instead of route-local compatibility symbols.

Preferred:

```python
workflow_application.submit
```

Avoid:

```python
routes.workflows._private_helper
```

---

# Recommended Execution Order

## Phase 1

Complete Workflow Application migration:

- route adapters only;
- remove route business logic;
- remove legacy exports.

## Phase 2

Split Generation and Completion runtime contracts.

## Phase 3

Delete compatibility facades after repository-wide reference scan.

## Phase 4

Run full governance gates:

```bash
uv run ruff check .
uv run python scripts/check_architecture.py
uv run python scripts/check_complexity.py
uv run python scripts/module_runtime_state_audit.py
uv run pytest
cd apps/web && npm test && npm run lint && npm run type-check && npm run build
```

---

# Final Assessment

The architecture work is directionally correct.

The project has moved from implicit coupling to explicit boundaries, but the final step is removing transitional abstractions and making ownership boundaries permanent.
