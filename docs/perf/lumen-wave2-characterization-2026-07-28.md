# Lumen Wave 2 Generation Perf/Fault Characterization

## Evidence Scope

- Work base: `98172a52582a552b806ab2fc30306cf3b41f5712`
- Evidence time: `2026-07-28T23:30:00+08:00`
- Runtime: Python 3.12.13 on macOS arm64, 10 logical CPUs
- Raw artifact: `docs/perf/lumen-wave2-characterization-2026-07-28.json`
- Harness: `perf/wave2/run.py`

The raw baseline was run in a detached temporary worktree at the exact work
base. Other Agents' concurrent production changes in the shared worktree were
not included in this evidence.

This is a characterization and target-oracle harness. Current production
failures remain `not_met`; synthetic target success is not presented as
production acceptance.

## Reproduction

```bash
uv run python perf/wave2/run.py suite \
  --candidate-counts 10,100 \
  --capacity 4 \
  --payload-mib 12 \
  --output /tmp/lumen-wave2.json
```

To compare revisions, run the suite in each checkout:

```bash
uv run python perf/wave2/run.py compare \
  --before /tmp/lumen-wave2-before.json \
  --after /tmp/lumen-wave2-after.json
```

## Baseline Metrics

### 100 mixed queued scheduler tick

The fixture uses six queue lanes and selects eight tasks at capacity four.

| Candidates | Total Redis commands | Candidate-scan RTT | Enqueued |
| ---: | ---: | ---: | ---: |
| 10 | 29 | 13 | 8 |
| 100 | 119 | 103 | 8 |

Candidate-scan RTT growth is `1.0` per additional candidate. The fixed harness
target is at most 8 scan RTT for 100 candidates and at most `0.05` growth per
candidate. Baseline status: `not_met`.

The total command count is retained separately because stable dispatch may add
a fixed amount of work per selected task. Acceptance is based on candidate
scan growth, not on hiding required enqueue/dispatch commands.

### Enqueue result unknown

The fake ARQ adapter accepts the first enqueue and then raises a timeout. At the
baseline, the enqueue dedupe key is removed, so the immediate second kicker
produces another accepted call:

| Measurement | Baseline |
| --- | ---: |
| Accepted active calls for one attempt | 2 |
| Duplicate active dispatch revisions | 1 |
| Dedupe present after unknown result | false |

Fixed target: at most one active dispatch revision per attempt. Baseline
status: `not_met`.

### Mixed ResourceDemand oracle

The deterministic workload contains exactly:

- 8 x 1MP, demand 3 units each;
- 2 x 4K, demand 6 units each;
- 2 x 1536px multi-reference edit with 96 MiB references, demand 8 units each;
- 1 x dual race, demand 4 units with 2 external-lane units.

Configured budgets are 14 global weighted units, 4 external-lane units, and 10
per-user units.

| Measurement | Result |
| --- | ---: |
| Peak active weighted units | 12 / 14 |
| Peak external-lane units | 4 / 4 |
| Completed tasks | 13 / 13 |
| Maximum wait | 15 ticks |
| Permit leaks after cancel/lease lost | 0 |
| Idempotent second release | 0 units |

The oracle holds dual-race external units through loser grace and releases
cancel/lease-lost permits idempotently. Oracle status: `met`.

### 4K URL bytes-to-base64 and staged target

The synthetic shape uses a 3840 x 2160 image and a 12 MiB compressed payload.

| Measurement | Legacy shape | Staged target |
| --- | ---: | ---: |
| Source/staged bytes | 12.0 MiB | 12.0 MiB |
| Base64 characters | 16.0 MiB | none |
| Base64 expansion | 1.3333x | none |
| Logical peak live bytes | 91.89 MiB | 44.64 MiB |
| Measured child peak RSS | 123.67 MiB | 74.55 MiB |

The staged target reduces logical peak live bytes by `51.42%` and measured
synthetic peak RSS by `39.72%`, above the fixed 30% target. The temporary
staging directory is removed.

This proves the harness target shape only. Real production 4K RSS remains
`gated` until both commands are provided on the same host and workload:

```bash
export LUMEN_WAVE2_4K_BEFORE_COMMAND='...'
export LUMEN_WAVE2_4K_AFTER_COMMAND='...'
uv run python perf/wave2/run.py payload
```

## Invariants

- Current production characterization is emitted even when acceptance fails.
- Fixed thresholds are constants in the harness, not values derived from the
  measured baseline.
- Scheduler scan growth is separated from fixed per-selected-task dispatch
  work.
- Enqueue unknown is injected after acceptance, not before the command.
- ResourceDemand covers pixels, references, edit postprocess, outputs, and
  dual-race external lanes.
- Active weighted units and external lanes never exceed configured budgets.
- Cancel and lease-lost release are idempotent and leave zero modeled permits.
- Synthetic payload and resource results are explicitly labeled.
- Real 4K before/after comparison requires two successful external commands.
- No production code, test manifest, execution ledger, version, or existing
  Wave 0 baseline is modified by this slice.

## Files And Tests

Owned files:

- `perf/wave2/run.py`
- `perf/wave2/test_run.py`
- `perf/wave2/README.md`
- `docs/perf/lumen-wave2-characterization-2026-07-28.json`
- `docs/perf/lumen-wave2-characterization-2026-07-28.md`

Validation:

```text
uv run ruff check perf/wave2
uv run ruff format --check perf/wave2
uv run pytest -q perf/wave2/test_run.py
uv run python perf/wave2/run.py suite ...
git diff --check -- perf/wave2 docs/perf/lumen-wave2-*
```

## Risks And Rollback

- Synthetic RSS excludes provider SDK, network buffers, PIL/pyvips behavior,
  object storage, and image-job process memory.
- Absolute elapsed times are informational and are not release gates.
- The local fake Redis implements the queue/dispatch contract needed by the
  harness; a future incompatible facade should update only the adapter, not the
  fixed acceptance thresholds.
- Rollback is removal or revert of the five owned files listed above. No
  production schema or runtime state requires cleanup.
