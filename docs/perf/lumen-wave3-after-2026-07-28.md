# Lumen Wave 3 Asset Browser After Evidence

Status: `measured`

Source HEAD: `e3e662af9a595700362e1d788224ad0fb63661f5`

```bash
export LUMEN_WAVE3_ASSET_URL='http://127.0.0.1:13000/stream'
export LUMEN_WAVE3_BROWSER_HEADERS_JSON='{"Cookie":"<redacted>"}'
node perf/wave3/run.mjs suite \
  --output docs/perf/lumen-wave3-after-2026-07-28.json
```

The authenticated product run used an isolated local PostgreSQL database,
Redis port, storage root, and admin session. It seeded 1000 feed items with
the same fixed distribution as the characterization: 920 ready, 50 missing
thumb, 20 thumb rows whose files return 404 while preview is ready, and 10
pending all-variant items. No existing user or production data was touched.

## Authenticated Product Result

- Query cache loaded all 1000 items while mounted tiles remained between 20
  and 39, below the desktop budget of 160.
- Grid and total `/binary` requests were zero. Explicit lightbox open upgraded
  through one successful `display2048` request.
- Hover-triggered `display2048` requests were zero; the one display request was
  caused by explicit lightbox open.
- The page-20 search target was returned by one server `q` request after only
  the initial page had loaded.
- Failed thumb URLs remaining in `srcSet`: zero. Repeated failed-thumb
  requests: zero.
- Two top-bottom-top cycles completed, followed by lightbox open/close and
  continued interaction.
- Forced-GC heap growth was 9.01%, below the fixed +20% characterization
  threshold. First interactive was 69.9 ms on this host.

## Queue And Weak-Network Evidence

The component-owned scheduler intentionally exposes no process-global browser
hook, so product-page queue depth is reported as `gated`, not guessed.
Independent evidence covers that private state:

- the real Chromium target oracle measured desktop queue depth 32/32 and
  mobile Save-Data/3g queue depth 1/32;
- the focused scheduler contract swept 500 candidates, kept queue depth at
  32, cancelled unstarted hover work, and released a timed-out active slot;
- the mobile target oracle skipped all 500 low-priority weak-network prewarms.

The synthetic target oracle validates the harness and thresholds. Only the
authenticated `realProductAfter` scenario is product-page evidence; none of
these measurements are presented as production SLOs.

Rollback removes the independent harness/report files or reverts the Wave 3
product commits. The smoke database, Redis instance, and temporary storage are
isolated runtime fixtures and require no application data migration.
