# Lumen Wave 2 Generation Perf/Fault Harness

This harness is independent from production code, the test manifest, and the
execution ledger. It separates current implementation characterization from
fixed target oracles so an unmet baseline remains visible instead of being
raised to make the run green.

## Run the full characterization

```bash
uv run python perf/wave2/run.py suite \
  --candidate-counts 10,100 \
  --capacity 4 \
  --payload-mib 12 \
  --output /tmp/lumen-wave2.json
```

The suite covers:

- one scheduler tick over 100 mixed queued candidates, including Redis command
  and logical round-trip counts;
- an ARQ enqueue that is accepted and then returns an unknown result;
- deterministic weighted admission for 8 x 1MP, 2 x 4K, 2 x multi-reference
  edit, and 1 x dual-race task;
- cancel and lease-lost permit cleanup oracles;
- a 4K URL `bytes -> base64 -> bytes` live-memory shape and staged-file target.

Current production paths are imported for scheduler and enqueue
characterization. ResourceDemand and staged payload are local deterministic
oracles until the shared production contracts exist.

## Focused runs

```bash
uv run python perf/wave2/run.py scheduler
uv run python perf/wave2/run.py enqueue-unknown
uv run python perf/wave2/run.py resources
uv run python perf/wave2/run.py payload
```

## Real 4K before/after RSS

The default payload scenario is synthetic and labeled as such. Use commands
that exercise the same 4K generation fixture on the same host to collect real
process-group RSS:

```bash
export LUMEN_WAVE2_4K_BEFORE_COMMAND='...'
export LUMEN_WAVE2_4K_AFTER_COMMAND='...'
uv run python perf/wave2/run.py payload
```

The command sampler records exit status, elapsed time, peak process-group RSS,
and bounded stdout/stderr tails. It does not pass silently when the commands
are absent; the result is `status=gated`.

## Compare two revisions

Run `suite` in each checkout, then compare the JSON artifacts:

```bash
uv run python perf/wave2/run.py compare \
  --before /tmp/lumen-wave2-before.json \
  --after /tmp/lumen-wave2-after.json
```

Fixed source thresholds:

- at most 8 candidate-scan Redis round trips for the 100-candidate smoke;
- candidate-scan round-trip growth at most 0.05 per additional candidate;
- at most one active dispatch revision per attempt after enqueue unknown;
- active weighted and external-lane units never exceed configured budgets;
- staged payload logical peak target is at least 30% below the legacy shape.

The thresholds are not calculated from current measurements. Synthetic target
success validates the harness oracle, not production readiness.
