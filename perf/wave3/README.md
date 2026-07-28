# Lumen Wave 3 Asset Browser Harness

This harness owns only Wave 3 performance evidence. It uses the repository's
existing direct Chromium DevTools Protocol approach, so it does not add a
Playwright dependency or modify product/test infrastructure.

## Run

```bash
node --test perf/wave3/model.test.mjs
uv run pytest -q perf/wave3/test_run.py
node perf/wave3/run.mjs suite \
  --output /tmp/lumen-wave3.json
```

The deterministic fixture covers:

- 1000 assets with exact 5% missing-thumb, 2% thumb-404/preview-ready, and 1%
  all-variants-pending distributions;
- a search target at item 975, on page 20 with page size 50;
- two fast top-bottom-top scroll cycles;
- lightbox open/close followed by continued interaction;
- a 500-candidate prewarm sweep;
- desktop plus mobile Save-Data/3g Chromium sessions;
- DOM, forced-GC heap, network, long-task, fallback, search, and prewarm
  diagnostics.

The suite reports a frozen current-model fixture and fixed target oracles. A
passing target oracle validates the harness, not production acceptance.

## Real Product After

After A0 integrates the API and Web Wave 3 implementation, run the same
generic browser collector against the authenticated Stream URL:

```bash
export LUMEN_WAVE3_ASSET_URL='http://127.0.0.1:3000/stream'
export LUMEN_WAVE3_BROWSER_HEADERS_JSON='{"Authorization":"Bearer ..."}'
node perf/wave3/run.mjs suite \
  --output docs/perf/lumen-wave3-after-2026-07-28.json
```

For full action coverage, the target page or an A0-owned test adapter should
expose `window.__wave3Actions` and `window.__wave3Metrics` with the same
contract as the fixture. Without those hooks, generic DOM/network collection
still runs and action-dependent evidence is explicitly gated.

Set `LUMEN_WAVE3_CHROME` if Chrome/Chromium is not installed in a standard
location. Browser headers are used for the session but are never copied into
the result.

## Fixed Thresholds

- desktop mounted tiles: at most 160;
- mobile mounted tiles: at most 80;
- queued prewarm jobs: at most 32, with active image work tracked separately;
- grid `/binary` requests: zero;
- hover-triggered display requests: zero;
- page-20 server search: result found with one page loaded before search.

Heap and first-interactive deltas are recorded for same-host comparison. They
are not treated as production SLO proof by the synthetic fixture.
