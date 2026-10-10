# Reference-video poster preparation and safe retry

Isolated implementation, 2026-10-10. No release, deployment, production migration,
provider generation, dependency installation, or change to original retention.

## Contract

POST /api/videos/{video_id}/preparation/retry requires the existing authenticated
owner and CSRF checks. Body:

- expected_source_sha256: lowercase SHA-256, 64 hexadecimal characters
- expected_preparation_revision: strict integer, at least zero
- idempotency_key: 16 to 128 characters

Response is {asset: AssetDescriptor}. Only failed changes to pending. Current
pending/preparing/ready is a no-op. A repeated key returns the latest state of
that same retry even if its worker has already failed again. A new retry after
another failure requires the latest revision and a new key. Foreign/deleted
assets return 404; stale hash/revision or conflicting key returns 409. Errors
use the existing detail.error envelope. This request only updates durable
preparation state and does not invoke ffmpeg, a provider, or billing.

The existing lifecycle loop owns processing. Its per-video child isolates
capacity-driven cancellation; application shutdown still cancels the parent
and waits for the bounded child work to stop.

## Storage and ownership invariants

- Only uploaded-reference records with matching original hashes are claimed.
- Source identity includes owner, video ID, path, size, SHA-256, claim token,
  and lease expiry. Paths must be the exact owned u/{user}/vref/{video} directory;
  traversal, foreign paths and symlink components are rejected.
- Actual original bytes are verified before inspection and again afterward;
  rendering/publication repeat this check. MIME and upload filename never
  choose media metadata or the poster path.
- The original is never modified. Poster identity includes original hash,
  poster-format revision and durable artifact revision. Lease takeover and
  explicit retry reuse this artifact slot while public state revision advances.
- A committed slot reserves the maximum 5 MiB in user quota before rendering.
  Before any installation, the exact rendered checksum/size is committed.
  The file is fsynced and atomically linked without replacing an existing file.
  Only an exact matching manifest can adopt an already-installed artifact.
- Publication checks the current claim and capacity both before and after
  installation. Metadata and poster_storage_key become visible in the same
  ready-state DB commit. A failed or uncertain commit never deletes the source
  or a possibly committed poster.
- A crash after file installation leaves a known, accounted slot. A successor
  validates/adopts that exact file; a stale worker cannot publish its result.
- Slot quota is included in API SQL, upload inventory and core/worker reference
  accounting. A failed slot keeps its conservative reservation for a safe
  explicit retry or existing authorized deletion; retries do not multiply it.
  Existing cleanup recognizes the slot, without changing retention policy.
- Transcode and storage capacity remain held through bounded thread completion
  on cancellation. An interrupted claim remains recoverable after lease expiry.

## Existing media policy reused

- packages/core/lumen_core/volcano_asset_media_types.py:30-31:
  poster at most 5 MiB and 640 pixels on its longest side
- packages/core/lumen_core/volcano_asset_media_transcode.py:523:
  existing poster timeout ceiling of 60 seconds and JPEG quality 3
- apps/api/app/video_reference_probe.py:26,28:
  15-second ffprobe and 256 MiB ffmpeg allocation limit

Reference input dimension/duration/frame/decode limits remain the existing
_validate_source_video policy. The poster uses one frame, one decoder/encoder
thread, one filter thread, no audio/subtitles/data, stripped metadata, bounded
output, and local-only file/pipe protocols. No transport variant is eagerly
prepared by this job.

## Verification

- Initial focused preparation suite: 27 passed in 0.89 seconds, including the
  six original claim/metadata tests; operation 9f14e76f359c4cef93b54233a79308de.
- Targeted ruff and git diff --check passed; operation
  24e11deeed9c4386a9beeb34f9874adc.
- Final focused suite, including child-cancellation, parent-shutdown and real
  HTTP/CSRF tests: 30 passed in 0.89 seconds.
- test_video_preparation_media.py is an opt-in real-media smoke. Set
  LUMEN_PREPARATION_MEDIA_TEST=1 in the approved test window. It creates one
  two-second synthetic clip using existing ffmpeg/ffprobe, verifies inspection
  and poster generation/adoption, and checks unchanged original bytes.
- Real two-second synthetic-media smoke passed: 1 test in 0.76 seconds.
  Final source ruff and git diff --check also passed in the same operation:
  d201f590ce56444f81da9a6c1d94a66a (total command duration 2.748 seconds).
- PostgreSQL multi-process races, real Redis lease-loss integration, full
  regression/architecture gates and browser UX remain owner-level final gates.
