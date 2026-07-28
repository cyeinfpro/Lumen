# Lumen Wave 2 After Evidence

- Date: 2026-07-28
- Before SHA: `98172a52582a552b806ab2fc30306cf3b41f5712`
- After SHA: `6da3419674d964668cb01d17459aec0148289e54`

## Results

| Contract | Before | After | Result |
| --- | ---: | ---: | --- |
| 100-candidate scheduler Redis RTT | 119 | 29 | met |
| Active dispatch revisions after unknown enqueue | 2 | 1 | met |
| Dual-race external lane units | 1 count slot | 2 weighted units | met |
| Permit leaks after release/cancel model | n/a | 0 | met |
| Synthetic staged payload peak RSS | 129,679,360 B | 78,135,296 B | 39.75% lower |

The scheduler result includes one wakeup-coalescing `SET`, one batched `MGET`,
one active-set cleanup, one active-set read, and stable dispatch operations for
the eight selected tasks. Candidate scan RTT no longer grows with candidate
count.

The payload result is a same-host synthetic comparison from `perf/wave2`. The
production path now streams DNS-pinned URL results into owned staging files and
removes the bytes-to-base64 intermediate. No external provider 4K workload was
run locally, so this artifact does not claim a production provider SLO.
