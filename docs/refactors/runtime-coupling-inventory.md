# Runtime Coupling Inventory

Status: ratcheted baseline

The machine-readable inventory is
`docs/refactors/runtime-coupling-inventory.json`. It records coupling that a
normal static import graph misses:

- dynamic imports and file-based module loading;
- writes, deletes, and mutating calls against `sys.modules`;
- imports of private names across module boundaries;
- module-level mutable containers and explicit `global` statements;
- public API exports for compatibility facades in the retirement ledger.

The baseline is not an allowlist for new code. `scripts/check_architecture.py`
fails when a new finding appears or when a facade public API changes. Removing
an entry is always allowed; updating the baseline requires explicit review:

```bash
uv run python scripts/architecture_audit.py --update-baseline
uv run python scripts/check_architecture.py
```

Prefer injected contracts, leaf modules, instance-owned state, and explicit
imports. Dynamic facade lookups should be retired with their corresponding
ledger entry.

## Delivery Matrix

| ID | Delivered control |
| --- | --- |
| GOV-001 | Machine-readable runtime-coupling inventory with category counts |
| GOV-002 | Compatibility-facade ownership and retirement ledger |
| GOV-003 | Ratcheted architecture and complexity baselines wired into existing gates |
| I-001 | Dynamic import and file-loader reporting |
| I-002 | `sys.modules` mutation reporting |
| I-003 | Private cross-module import reporting |
| I-004 | Module mutable state and explicit `global` reporting |
| I-005 | Compatibility-facade public API manifest verification |
| I-006 | Function length, parameter count, and nesting complexity dimensions |
| I-007 | Modular update entrypoint, phase contract, atomic journal, and failpoints |
| I-008 | Resume, rollback, failpoint, shell syntax, and gate regression tests |

The updater keeps `scripts/update.sh` as the public entrypoint. Its implementation
lives in `scripts/update/`, and the journal records operation identity, phase
attempts, completed phases, selected rollback context, terminal status, and
failure metadata. `LUMEN_UPDATE_FAILPOINT` accepts `before:<phase>`,
`after:<phase>`, `<phase>:before`, `<phase>:after`, or a bare phase name.
`LUMEN_UPDATE_RESUME=1` reuses a failed or interrupted journal operation.

### Update Module Boundaries

- `update/release/`: release resolution, manifest verification, immutable
  source acquisition, self-update, staging, activation, and image pulls.
- `update/backup/`: restore-point verification, preflight, storage/infra
  checks, and Alembic migration.
- `update/services/`: atomic switch, service restart/traffic shift, and health.
- `update/recovery/`: transactional snapshots, rollback/recovery helpers, and
  cleanup.
- `update/runner.sh`: module loading, locks/traps, resume-aware phase ordering,
  and terminal status only.

Every updater shell file must remain below 600 lines. The complexity gate scans
`scripts/update.sh` and `scripts/update/**/*.sh` recursively with a maximum of
599 lines, independently from the 1,500-line Python/TypeScript source limit.
