# Lumen Wave 3 Asset Browser Characterization

## Evidence Scope

- Work base: `e103a96afc763cf6ad5fff36b0483f3c8ac16e35`
- Evidence time: `2026-07-28T23:59:57+08:00`
- Browser: local Google Chrome headless via the existing direct CDP pattern
- Raw artifact:
  `docs/perf/lumen-wave3-characterization-2026-07-28.json`
- Harness: `perf/wave3/`

This slice changes no product code, test manifest, execution ledger, version,
or release state. The default fixture is synthetic and deterministic. It
characterizes the confirmed current model and proves that fixed target oracles
can distinguish bounded behavior. It is not production SLO evidence.

The real integrated Stream run remains explicitly gated until A0 has merged
the A5/A6 product implementation and can provide an authenticated target URL.

## Reproduction

```bash
node --test perf/wave3/model.test.mjs
uv run pytest -q perf/wave3/test_run.py
node perf/wave3/run.mjs suite \
  --output /tmp/lumen-wave3.json
```

After product integration:

```bash
export LUMEN_WAVE3_ASSET_URL='http://127.0.0.1:3000/stream'
export LUMEN_WAVE3_BROWSER_HEADERS_JSON='{"Authorization":"Bearer ..."}'
node perf/wave3/run.mjs suite \
  --output docs/perf/lumen-wave3-after-2026-07-28.json
```

## Deterministic Fixture

The fixture contains exactly 1000 assets with page size 50:

| Scenario | Count | Share |
| --- | ---: | ---: |
| Missing thumb, preview ready | 50 | 5% |
| Thumb 404, preview ready | 20 | 2% |
| All grid variants pending | 10 | 1% |
| Normal ready item | 920 | 92% |

The search token `page20-target` exists only on `asset-975`, which is on page
20. Each browser scenario performs two top-bottom-top scroll cycles, a
500-candidate prewarm sweep, lightbox open/close, and the page-20 search.

## Fixed Thresholds

- desktop mounted tiles: at most 160;
- mobile mounted tiles: at most 80;
- queued prewarm jobs: at most 32;
- grid `/binary` requests: zero;
- hover-triggered display requests: zero;
- page-20 search result found with one page loaded before server search;
- failed thumb removed from the remaining `srcSet`.

These constants are source-owned and are not calculated from the baseline.

## Frozen Current-Model Characterization

The legacy fixture intentionally mirrors the confirmed pre-Wave-3 behavior.

| Measurement | Observed | Fixed target | Status |
| --- | ---: | ---: | --- |
| Maximum mounted tiles | 1000 | <= 160 | not met |
| Prewarm queue depth | 497 | <= 32 | not met |
| Grid `/binary` requests | 10 | 0 | not met |
| Hover display requests | 500 | 0 | not met |
| Pages loaded before page-20 search | 20 | 1 | not met |
| Server search requests | 0 | 1 | not met |
| Bad thumb present in `srcSet` observations | 54 | 0 | not met |
| Browser task duration | 4.50 s | informational | measured |

The bad-`srcSet` observation count can exceed the 20 failing assets because the
unbounded fixture remounts/reselects candidates during the scroll workload.
It is a defect signal, not a production retry-rate estimate.

Forced-GC final heap was 10.21% below the initial sample because the final
search reduces the rendered result to one item. That value must not be used as
a memory improvement claim.

## Target Oracle Results

### Desktop

| Measurement | Observed | Fixed target | Status |
| --- | ---: | ---: | --- |
| Maximum mounted tiles | 150 | <= 160 | met |
| Scroll sample mounted range | 80-150 | bounded | met |
| Prewarm queue depth | 32 | <= 32 | met |
| Cancelled queued prewarms | 32 | > 0 | observed |
| Capacity drops | 465 | bounded queue | observed |
| Image timeout cleanup | 2 | > 0 injected | observed |
| Grid `/binary` requests | 0 | 0 | met |
| Hover display requests | 0 | 0 | met |
| Bad thumb retained in `srcSet` | 0 | 0 | met |
| Pages loaded before server search | 1 | 1 | met |
| Server search requests | 1 | 1 | met |
| Forced-GC heap growth | 3.40% | <= 20% | observed |
| Browser task duration | 1.30 s | informational | measured |

### Mobile Save-Data / 3g

The session overrides `navigator.connection.saveData=true`, reports
`effectiveType=3g`, sends `Save-Data: on`, and uses CDP network emulation.

| Measurement | Observed | Fixed target | Status |
| --- | ---: | ---: | --- |
| Maximum mounted tiles | 44 | <= 80 | met |
| Scroll sample mounted range | 26-44 | bounded | met |
| Prewarm queue depth | 1 | <= 32 | met |
| Weak-network prewarm skips | 500 | expected | observed |
| Grid `/binary` requests | 0 | 0 | met |
| Hover display requests | 0 | 0 | met |
| Page-20 server search | found from 1 loaded page | required | met |
| Forced-GC heap growth | 4.51% | <= 20% | observed |

The 3g first-interactive value is network-emulation-specific and cannot be
compared directly with the unthrottled desktop fixture.

## Invariants

- The 1000-item distribution and page-20 target are deterministic.
- Mounted DOM and prewarm acceptance use fixed upper bounds.
- Active image work is tracked separately from queued prewarm work.
- Queue cancellation, capacity drop, timeout release, and weak-network skip
  are observable.
- Grid requests and hover actions never use display/original URLs in the
  target oracle.
- Server search is identified from the actual browser network log.
- A failed thumb is removed from both active source selection and `srcSet`.
- Real-product evidence remains gated; fixture success is not production SLO
  proof.
- No production code or cross-agent owner file is modified by A8.

## Files And Tests

Owned files:

- `perf/wave3/model.mjs`
- `perf/wave3/model.test.mjs`
- `perf/wave3/asset_fixture.html`
- `perf/wave3/browser_assets.mjs`
- `perf/wave3/run.mjs`
- `perf/wave3/test_run.py`
- `perf/wave3/README.md`
- `docs/perf/lumen-wave3-characterization-2026-07-28.{md,json}`
- `docs/perf/lumen-wave3-after-2026-07-28.{md,json}`

Validation:

```text
node --check perf/wave3/model.mjs
node --check perf/wave3/browser_assets.mjs
node --check perf/wave3/run.mjs
node --test perf/wave3/model.test.mjs
uv run ruff check perf/wave3
uv run ruff format --check perf/wave3
uv run pytest -q perf/wave3/test_run.py
node perf/wave3/run.mjs suite ...
git diff --check -- perf/wave3 docs/perf/lumen-wave3-*
```

## Metrics And Observability

The JSON records:

- mounted tile maximum and scroll sample range;
- DOM nodes, image count, forced-GC heap, layout/recalc counts, task duration,
  and long tasks;
- request/response/failure/encoded-byte counts plus binary/display/search
  request counts;
- source fallback and bad-`srcSet` observations;
- prewarm queue maximum, active maximum, drops, cancellations, timeouts, and
  weak-network skips;
- loaded-page count and result IDs for server search.

## Risks And Rollback

- The fixture uses 1-pixel GIF responses; it measures control flow, DOM
  bounds, scheduling, and browser request behavior, not decoded 4K memory.
- CDP task duration and first-interactive values vary by host and browser
  version. Compare revisions only on the same environment.
- The real Stream page may require A0-owned diagnostics/test-adapter wiring for
  action-level prewarm and search evidence. Missing hooks are reported as
  gated, not silently accepted.
- Rollback is a single revert or removal of the owned harness/report files. No
  schema, cache, database, or runtime state cleanup is required.
