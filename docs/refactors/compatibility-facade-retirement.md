# Compatibility Facade Retirement Ledger

Status: active governance record

The machine-readable ledger is
`docs/refactors/compatibility-facade-retirement.json`. Each entry has an owner,
current status, and a concrete retirement condition. `scripts/architecture_audit.py`
uses the ledger to snapshot and protect each facade's public API.

## Rules

1. A compatibility facade may stay only while a real caller or supported
   monkeypatch surface still depends on it.
2. New implementation logic belongs in leaf modules, not in the facade.
3. Public exports cannot change without updating focused compatibility tests
   and explicitly regenerating the runtime-coupling inventory.
4. Retirement requires a repository-wide reference search, caller migration,
   deletion of facade-specific tests, and removal of the ledger entry in the
   same change.
5. `status=active` is debt, not a permanent architecture exemption.

## Review Cadence

Review the ledger after each decomposition wave. Close entries when their
retirement condition is met; do not add a replacement facade at another path
without documenting its owner and exit condition.
