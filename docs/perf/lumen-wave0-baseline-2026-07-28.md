# Lumen Wave 0 Perf/Fault Baseline

## Scope

- Work base: `d2b8c259d52edc0a839b695fecc7b7ab2029bbe3`
- Branch: `codex/a8-perf-observability-wave0-20260728`
- Machine: macOS 26.3.1 arm64, 10 logical CPUs
- Runtime: Python 3.14.3, Node 22.22.0, headless Google Chrome
- Raw data: `docs/perf/lumen-wave0-baseline-2026-07-28.json`

This is a Wave 0 characterization baseline. Synthetic fixture results prove the
harness and provide a stable comparison shape; they are not production SLO
evidence.

## Reproduction

```bash
uv sync --all-packages
ulimit -n 4096
uv run python perf/wave0/run.py baseline \
  --iterations 5000 \
  --candidate-counts 10,100,1000 \
  --capacity 4 \
  --output docs/perf/lumen-wave0-baseline-2026-07-28.json
```

The raised shell file descriptor limit avoids the host's default `256` limit
interfering with Chrome and workspace imports. It does not change application
behavior.

## Results

### Realtime duplicate and fanout

| Measurement | Result |
| --- | ---: |
| Same live event delivered on task + user channels | 2 SSE frames |
| Replay-seen event delivered live | 0 SSE frames |
| Fanout channels per task event | 2 |
| Synthetic envelope bytes across fanout | 272 bytes |
| Local decode/format throughput | 165,840 events/s |

The current route deduplicates replay/live overlap but not live/live overlap.
The injected first-channel failure also characterizes different publisher
behavior: API publication stops before the user channel, while Worker retries
the task channel and still reaches the user channel.

Evidence paths:

- `apps/api/app/routes/events.py::_standard_pubsub_events`
- `apps/api/app/sse_publish.py::publish_sse_event`
- `apps/worker/app/sse_publish.py::publish_event`

### Generation queue tick

| Candidates | Redis commands | Enqueued |
| ---: | ---: | ---: |
| 10 | 29 | 8 |
| 100 | 119 | 8 |
| 1000 | 1019 | 8 |

Observed command growth is `1.0` Redis command per additional candidate, driven
by per-candidate `not_before` reads on the current path. The harness executes
`generation_parts.queue.kick_image_queue` with deterministic candidates and a
command-counting Redis adapter.

### Generation RSS entrypoints

| Scenario | Synthetic payload | Peak RSS |
| --- | ---: | ---: |
| 1MP | 20.0 MiB | 52.6 MiB |
| 4K | 126.6 MiB | 159.1 MiB |
| Edit | 112.0 MiB | 144.2 MiB |
| Dual race | 160.0 MiB | 192.4 MiB |

These defaults use touched byte buffers to create low-cost, repeatable memory
shapes. Each scenario has a corresponding
`LUMEN_WAVE0_RSS_<SCENARIO>_COMMAND` gate for a real generation workload; the
external command sampler measures the full process group.

### 1000 asset browser fixture

| Measurement | Result |
| --- | ---: |
| Mounted tiles | 1000 |
| DOM elements | 3009 |
| Images | 1000 |
| JS heap used | 720.1 KiB |
| Encoded network bytes | 228,366 |
| Network requests | 1013 |
| `/binary` requests | 0 |
| Thumb 404 -> preview fallback | 20 |
| First interactive mark | 35.6 ms |
| Long tasks observed | 0 |

The contract fixture contains 5% missing thumbs, 2% thumb 404s with preview
fallback, and 1% pending variants. A real Stream page can replace the fixture
through `LUMEN_WAVE0_ASSET_URL` and `LUMEN_WAVE0_TILE_SELECTOR`.

### Feed/search timing

Status: `gated`.

The current workspace did not provide a running authenticated API or dataset.
Set `LUMEN_WAVE0_FEED_URL` and authentication variables documented in
`perf/wave0/README.md`; the harness then records unfiltered and `q=` search
p50/p95/max latency, status, response bytes, and returned item counts.

## Metrics Mapping

The raw fields provide Wave 0 inputs for:

- `lumen_sse_live_publish_bytes_total`
- `lumen_sse_connection_duplicate_total`
- `lumen_image_scheduler_candidates`
- `lumen_image_scheduler_selected`
- `lumen_image_scheduler_redis_commands`
- `lumen_image_worker_rss_bytes`
- `lumen_asset_mounted_tiles`
- `lumen_asset_feed_query_seconds`

No production metric registration was added in this A8 slice.

## Invariants and Boundaries

- No business behavior, composition, CI, test manifest, ledger, version, or
  existing baseline was modified.
- Current implementation paths are imported for characterization; adapters are
  local to the harness.
- Environment-gated scenarios return `status=gated` with required variables.
- Same-environment JSON comparison is authoritative; absolute synthetic
  milliseconds are not release gates.

## Risks and Rollback

- Synthetic RSS does not include provider SDKs, PIL/pyvips, storage, or network
  buffers. Use real command gates before claiming a memory reduction.
- The browser fixture validates the measurement contract, not current Stream
  virtualization behavior. Use a seeded authenticated page for product proof.
- Feed/search remains unmeasured until an authenticated API and representative
  dataset are supplied.
- Rollback is removal or revert of the independent `perf/wave0` and
  `docs/perf/lumen-wave0-baseline-2026-07-28.*` additions.
