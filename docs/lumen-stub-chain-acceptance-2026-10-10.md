# Lumen disposable API / dispatcher / worker acceptance

Date: 2026-10-10. Isolated workspace: 520b06121cc249f7b5c67da858426a77.

## Result

The expanded local harness passed in 15.4 seconds (operation
db12dce2fb374b1e8ff10a0cd570938e). It performs real Canvas HTTP routing, plan
preview/pricing/admission, Generation and outbox persistence, plan dispatch,
Redis/ARQ dispatch identity handling, the production generation runner, artifact
commit, wallet transactions, worker Canvas reconciliation and GET serialization.

Evidence: tmp/lumen-core-ui-joint/stub-chain-xu79cwfu/evidence.json.
SHA-256: b311570e4068bfc15ebcb73000fff0357072ac51de2aa54100cb87786d232156.
The directory also preserves SQLite rows, generated artifacts, provider-call
journal, worker logs and Redis shutdown log. Earlier failed runs are preserved.

## Final shared-harness regression

After the video harness introduced the shared SQLite UTC codec, the complete
image/edit chain was rerun against the final candidate on 2026-10-10 at
16:12:57–16:15:04 UTC as part of operation 1ca7cc84a09048c189c7e18469e9ff19.
It passed with nine provider-port calls, the same 300-micro success-chain debit,
and unchanged unknown replay state. Evidence:
tmp/lumen-core-ui-joint/stub-chain-eft3pult/evidence.json, SHA-256
f809723598820a1671d3d4ab02bb3bc3aede788e207c708461ee8fbe4a6337f2.
The same serial gate passed 1,173 Web unit tests, type-check, lint and production
build. These Web checks do not replace the real backend assertions below.

## Actual assertions

- A two-candidate image node feeds an image-edit node through explicitly chosen
  candidate 2. Exactly three Generation rows, three artifacts and three wallet
  settlements result, totaling 300 CNY micro-units in the synthetic wallet.
- The downstream Generation references the exact selected upstream image ID.
  Every GET output SHA equals its durable Image row and actual stored bytes.
- Concurrent dispatch producers share one durable ARQ identity. Duplicate
  delivery, original plan replay and GET repair leave task/artifact counts,
  wallet balance, holds and transaction count unchanged.
- Confirmed no-cost failure requires additional admission budget when the
  original budget is exhausted. Replaying the same repair creates one new
  attempt only; the original failed attempt remains in history.
- Fail-fast repair restarts exactly the failed root and its blocked descendant.
  An unrelated blocked branch retains its original execution ID and no task.
- An ambiguous provider submission remains non-retryable despite a terminal
  failure projection. Fresh preview and repair return 409. Replaying the original
  intent, duplicate worker delivery and GET do not add a task or charge.
- A different synthetic owner receives 404 for document and intent lookup.
- Across all scenarios: nine provider-port invocations, nine Generation rows,
  six committed images; final synthetic wallet balance 99,400 of 100,000, zero
  holds. Thirty successful HTTP responses plus two expected 404, two 409 and one
  422. Both API and worker processes assert zero TCP connection attempts.

## Defect discovered and fixed

The real two-candidate chain exposed an existing batch auto-selection mismatch:
dispatcher used candidate 2, but worker and API read repair selected output 0.
That made the downstream result fail the semantic-freshness auto-selection
check even though the plan completed correctly.

New plan snapshots capture planned_output_ordinal. A shared core function maps
that exact ordinal to the stored output index. Missing, malformed or duplicated
planned candidates fail closed; partial results cannot substitute another
candidate. Legacy/single-node first-available behavior remains unchanged.
Both worker and API retain their existing definition/input freshness checks,
unlocked-selection requirement and revision compare-and-swap.

Focused regressions: core/API 26 passed and worker 23 passed, including candidate
2, compact partial results, missing candidates and locked-selection protection.
Final affected-suite verification passed at 14:43 UTC, operation
93b847d3bdc045f8ae0bdf5e58c45e82: API 216, core 92, worker 36, no skips.
The API run explicitly enabled disposable PostgreSQL admission/lock-order,
Redis expiry/child-lifetime and ffmpeg media tests. Ruff, global complexity
budget and git diff --check passed. Logs: tmp/lumen-core-ui-joint/phase2-final-
{api,core,worker,static}.log. These are the affected Canvas/media suites, not a
claim that every Python test in the repository ran.

## Reproducibility and boundaries

Entry point: scripts/lumen_canvas_stub_harness.py, with the existing repository
Python environment and existing redis-server on PATH. Supporting scripts are
lumen_canvas_stub_api.py and lumen_canvas_stub_worker.py.

Every run creates a new local SQLite database and artifact directory. A copied
SQLite test schema adapts only PostgreSQL empty ARRAY defaults. It does not
replace PostgreSQL concurrency tests. Redis uses a unique Unix socket, port 0,
no persistence, and is terminated by its owning parent. The successful log
confirms SIGTERM, socket removal and normal exit. Worker timeouts terminate and
reap only their owned subprocess.

Explicit substitutes are synthetic HTTP authentication/CSRF, locally driven
outbox/ARQ transport instead of a continuously running ARQ daemon, and an
in-process provider plus provider-reservation port. The provider emits small
deterministic local PNGs or controlled errors. No provider network, production
database, credentials, dependency installation, commit, publication or deployment
is involved. This is an image-generation/edit chain; it does not claim a real
external-provider or video-provider end-to-end run.
