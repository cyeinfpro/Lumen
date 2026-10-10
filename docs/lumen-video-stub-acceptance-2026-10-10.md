# Lumen disposable video lifecycle acceptance

2026-10-10, isolated workspace 520b06121cc249f7b5c67da858426a77.

## Result

Passed in 5.9 seconds, operation a16c51efa38f4c2689ef7f3bfe06bfc1,
15:10:08–15:10:14 UTC. Entry point: python scripts/lumen_canvas_stub_harness.py --video.
Uses existing Python, Redis and ffmpeg only. The short functional run overlapped
other-project functional tests by coordination; it is not a performance sample.

Evidence: tmp/lumen-core-ui-joint/stub-chain-ocfx3_gu/video-evidence.json
SHA-256: e5a2699b59f0bd40282709e324d9c1e67dd6f7b83b2a5a89a48f3b7921ff74a9.
The directory preserves SQLite state, provider journal, actual MP4/poster,
worker output and Redis shutdown log.

## Real path and assertions

- Actual Canvas HTTP create, plan capability/pricing preview, admission, durable
  VideoGeneration/outbox, plan dispatch, production video submission and polling,
  lease/submit receipt, artifact ownership fence, ffmpeg validation/poster,
  local storage, wallet settlement, Canvas reconciliation and HTTP GET.
- The accepted video is 1280×720, 5,000 ms. Its SHA matches the stored Video row,
  GET descriptor and stored bytes; its generated poster exists. Automatic Canvas
  selection selects that exact output.
- Duplicate original plan admission, worker submit delivery, poll delivery and
  GET produce one VideoGeneration, one Video and one settlement. The synthetic
  wallet is 99,900 with zero holds after the successful 100-micro-unit result.
- A second synthetic provider submission loses acknowledgement. Its persisted
  status is submit_unknown, no provider task ID is invented, and the Canvas run
  remains running without outputs. New preview and explicit repair both return
  409; original intent replay and duplicate worker deliveries do not create a
  third task or another provider call, artifact, hold or settlement.
- Final snapshot: two VideoGeneration rows, one Video, three wallet transactions
  (two admission holds and one success settlement), available balance 99,800,
  hold 100. The uncertain task retains its original hold; the test does not
  pretend it succeeded, refund it, or settle its unknown cost as proven free.
- Across both scenarios there are exactly two provider submit calls, one poll
  and one download. Another synthetic owner gets 404. API and every worker child
  assert zero TCP connection attempts. All Redis traffic uses the owned Unix
  socket, port 0; normal SIGTERM/socket removal/exit and no owned process residue
  were verified after completion.

## Explicit substitutes and test corrections

Synthetic HTTP authentication/CSRF and locally driven outbox/ARQ scheduling are
used. The only video-provider adapter is an in-process subclass of the existing
fake adapter: submit/poll return deterministic local records and download returns
an existing generated MP4 file. This is not a live paid-provider integration.

SQLite uses a copied schema adapting PostgreSQL ARRAY defaults. In addition,
SQLite loses timezone information from timezone-aware DateTime columns, so a
harness-only load/refresh adapter restores UTC for those columns. It rejects a
non-SQLite engine. No production deadline, retry, billing or worker logic is
changed. Separate real PostgreSQL concurrency gates remain necessary and passed
in the affected backend suite.

Earlier attempts are retained: the first fixture used an unsupported action
field and was correctly rejected with 422; the second exposed the SQLite codec
mismatch before provider submission. They are not reported as passed runs.

The image/edit stub is independently documented in
lumen-stub-chain-acceptance-2026-10-10.md. Large-canvas browser performance and
release publication remain separate gates.
