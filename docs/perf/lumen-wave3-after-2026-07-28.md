# Lumen Wave 3 Asset Browser After Evidence

Status: `pending_product_integration`

A0 must replace this placeholder by rerunning the Wave 3 harness after the A5
API and A6 Web implementation commits are integrated.

```bash
export LUMEN_WAVE3_ASSET_URL='<authenticated-stream-url>'
export LUMEN_WAVE3_BROWSER_HEADERS_JSON='{"Authorization":"Bearer ..."}'
node perf/wave3/run.mjs suite \
  --output docs/perf/lumen-wave3-after-2026-07-28.json
```

The final report must use the same 1000-item distribution, browser, viewport,
network profile, and fixed thresholds as the characterization. It must not
claim production SLOs from the synthetic target oracle.

Required product evidence:

- zero grid `/binary` requests;
- no failed thumb remaining in `srcSet`;
- page-20 result found by a server `q` request without loading pages 2-19;
- desktop mounted tiles at most 160 and mobile at most 80;
- prewarm queue at most 32 with cancellation and timeout release;
- zero hover-triggered display generation;
- representative top-bottom-top scroll and lightbox open/close continuity;
- same-host forced-GC heap and task-duration comparison.
