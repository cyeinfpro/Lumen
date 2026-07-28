# Lumen Wave 0 Perf/Fault Harness

This harness is intentionally independent from CI, the test manifest,
composition roots, and business code. It imports current implementation paths
for characterization and emits comparison-friendly JSON.

## Quick baseline

```bash
uv sync --all-packages
ulimit -n 4096
uv run python perf/wave0/run.py baseline \
  --output /tmp/lumen-wave0-baseline.json
```

The default run covers:

- API/Worker realtime fanout and publish-failure injection;
- replay/live and live/live duplicate characterization;
- generation queue tick candidate, Redis command, and enqueue counts;
- low-cost synthetic RSS shapes for 1MP, 4K, edit, and dual race;
- a 1000-asset headless Chrome fixture with DOM, heap, network, and long-task
  collection;
- explicit feed/search environment gating.

## Real workload gates

Replace synthetic RSS shapes with real commands:

```bash
export LUMEN_WAVE0_RSS_1MP_COMMAND='...'
export LUMEN_WAVE0_RSS_4K_COMMAND='...'
export LUMEN_WAVE0_RSS_EDIT_COMMAND='...'
export LUMEN_WAVE0_RSS_DUAL_RACE_COMMAND='...'
uv run python perf/wave0/run.py rss
```

Measure an authenticated feed and server-side search:

```bash
export LUMEN_WAVE0_FEED_URL='http://127.0.0.1:8000/api/generations/feed'
export LUMEN_WAVE0_AUTHORIZATION='Bearer ...'
export LUMEN_WAVE0_SEARCH_QUERY='known item on a later page'
uv run python perf/wave0/run.py feed
```

Measure the synthetic browser contract fixture:

```bash
node perf/wave0/browser_assets.mjs
```

Measure a real Stream page instead:

```bash
export LUMEN_WAVE0_ASSET_URL='http://127.0.0.1:3000/stream'
export LUMEN_WAVE0_TILE_SELECTOR='[data-generation-id]'
export LUMEN_WAVE0_BROWSER_HEADERS_JSON='{"Authorization":"Bearer ..."}'
uv run python perf/wave0/run.py browser
```

Set `LUMEN_WAVE0_CHROME` when Chrome/Chromium is not installed in a standard
location. Header values are used only for the browser session and are not
included in JSON output.

## Interpretation

Synthetic results validate the harness and provide a stable comparison shape;
they are not production SLO evidence. Real RSS, authenticated feed/search, and
real Stream measurements must be compared on the same machine, browser,
dataset, and network conditions.
