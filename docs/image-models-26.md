# GPT Image 2.6 integration

Active models are `gpt-image-2.6-flare` and `gpt-image-2.6-sunburst`.
New API requests default to Flare. The composer retains its existing Sunburst
preference, upgraded to 2.6. Both accept auto/low/medium/high/xhigh/max quality.

## Generation and editing

- Raster generation/editing retains PNG, JPEG, WebP and native transparency.
- Independent `complexity`: low/medium/high. Omission preserves the provider's
  documented high default; it is separate from quality and Responses reasoning.
- The quick preset selects Flare + low complexity and preserves rendering quality.
  Faster output is a model capability, not a six-second guarantee or speed API flag.
- SVG output preserves original UTF-8 vector bytes and creates raster previews.
  Native text-to-SVG is documented. Native SVG edits are rejected before an
  upstream call: the official edits contract excludes SVG and the live gateway
  tests rejected both multipart and JSON edits. A separate vision-model workflow
  must be explicitly selected; it never silently replaces an Image 2.6 request.
- GIF output supports text prompts and static PNG/JPEG/WebP references through
  the native Images endpoint. The latest edits reference documents GIF output
  and requires the selected model's GIF preview access. No invented preview
  parameter is sent. The separate live report verified GIF edits for its tested
  gateway and credential; this does not establish every Lumen account's entitlement.
- GIF requests use count 1, 2–16 frames (default 9), and 20–2000 ms per frame in
  10 ms increments (default 200). Count 1 and the shared conservative ranges are
  Lumen restrictions, not a claim that all official references agree. GIF frame
  dimensions are positive multiples of 16; 512x512 is valid.
- GIF edits preserve the original static reference bytes and order.
  SVG or GIF originals are not accepted as static edit inputs. Mask and markup
  combinations with SVG/GIF output are currently rejected explicitly by Lumen,
  never discarded. Extract a static frame for editing an existing GIF.
- Native output is checked after both generation and editing. Raster substitutions
  for SVG/GIF, or a single-frame GIF, fail clearly with accepted-result-unknown
  handling, without an automatic second potentially billable request.
- SVG/GIF requests omit streaming, partial images, output compression and prompt
  rewriting. Specialized controls use the Images API route. An image-jobs-only
  configuration reports incompatibility instead of silently dropping controls.
- Markup editing uses a separate transparent PNG layer, not a flattened source
  or inpainting mask. The first original remains the edit target and other
  references retain order. The layer must match the first image after EXIF
  orientation, be <=50 MiB and <=16,777,216 pixels. Mask and markup are exclusive.
  Upload purpose `image_markup` preserves original PNG bytes.
- Multi-round raster editing reuses the preceding output as the next source.
  Consistency is a model capability; no fictional consistency toggle is added.

## SVG author configuration

The top-level image model (Flare or Sunburst) creates the reference image.
The separate `svg.model` selects the model that authors the vector document.
The Images generations reference requires this field when the provider enables
SVG model selection; when selection is disabled, the entire `svg` object must
be omitted. An omitted object is therefore not a universal default that works
for every account.

Use an author model ID explicitly supported by the selected provider/account.
The public reference accepts a nonempty string but does not enumerate supported
author IDs or define a default. A configured Responses model is not evidence of
SVG author availability, and Lumen must not silently reuse one or invent an ID.
A separate [live capability report](image26-live-capability-report-2026-11-05.md)
subsequently verified `gpt-6.1-sol` as an SVG author for
`https://flux.infpro.me/v1`: cases s03 and s06 returned valid SVG with Flare and
Sunburst respectively, plus separate `svg_usage`. This is evidence for the tested
gateway and credential, not a universal provider default or proof inferred from
`/models`. A recommendation must not silently replace a user's explicit choice.

The optional `svg.reasoning.effort` uses the author's supported reasoning level,
independently of image `complexity`; omission uses the author's default.
Do not send an `auto` effort value.

Author model selection applies to generations. Native SVG edits fail before
an upstream call whether or not an explicit `svg` author object is present.
Non-SVG requests must not carry SVG author controls. The independent vision-model
workflow uses a text Completion contract, not an Images API `svg` object.

SVG usage can include both reference-image generation and the author's tokens.
Configuring an ID does not prove entitlement, a successful live request, or a
fixed total cost. Parameter errors must not trigger an automatic second billable
request with a different model.

## GIF frame dimensions

The official guide accepts `size="auto"` or a `WIDTHxHEIGHT` string whose two
positive dimensions are multiples of 16. Size describes each finished frame,
not the whole animation. For example, 768x432 satisfies this alignment rule;
512x512 is an application default, not the only documented size.

Dimensions, frame count and number of animations share the model's request
pixel budget. The current GIF guide and model pages do not give a numerical
GIF-specific maximum; ordinary raster 4K constraints must not be presented as
proven GIF limits. Lumen additionally enforces its existing local safety limits:
each side <=10,000, <=64 million total frame pixels, and custom frame ratios
between 1:3 and 3:1. These are application restrictions, not an upstream guarantee
that every otherwise-valid size will be accepted.

The composer exposes automatic and custom frame sizing. Custom width/height
inputs must preserve the selected dimensions through submission and reroll;
automatic mode must reach the Images API as `size="auto"` rather than being
silently converted to a 512-based preset. Basic invalid dimensions are rejected
before submission, while provider-specific budget errors remain visible.

## Explicit vision-model SVG reconstruction

This is a separate Completion workflow, not Image 2.6 native editing or lossless
vector tracing. It uses the configured standard vision-chat model and text-token
pricing. The report demonstrates one simple icon with `gpt-6.1-sol` on the tested
gateway; it does not guarantee other models or complex-scene fidelity.

The explicit one-shot request uses `intent="vision_qa"` and
`chat_params.artifact_format="svg"`, with one owned static PNG/JPEG/WebP, fast mode
and tools disabled. Only the current prompt and source image are sent, without
ordinary chat-history, memory or tool context. Source bytes, image dimensions,
tokens, SVG bytes/elements and output dimensions are bounded. Invalid input is
rejected before a model call; no SVG/GIF input is silently rasterized.

Completed text and usage are persisted through the existing Completion checkpoint
before SVG validation, raster-preview rendering and private storage. Output is a
non-tool artifact with explicit `vision_svg` provenance; it must not acquire
image-generation-tool usage or Image 2.6 fixed-image charges. Missing or inexact
usage follows the existing unknown/reconciliation path, never a guessed rate or
automatic replacement model call.

A successful text call can still have `invalid` or `processing_failed` artifact
status. The user is told that no SVG was delivered and that completed text usage
still counts. Raw output is escaped private text, never executable markup.
Local repair uses the saved response identity and text, does not call the model
or charge again, and preserves ownership, cancellation and execution fencing.
Only a deliberately new generation may create another paid model request.

The UI explicitly requests at most 16,000 output tokens and discloses actual
input/output text-token billing; an API request that omits the limit defaults to
2,048. A local retry may fail again: a fresh Completion GET and history refresh
re-enable local repair, without a model call. Verified originals download through
the existing authenticated blob-download helper, with failure feedback.

The implementation and final UI passed the pre-release checks recorded below
and in the live report. Publication and deployment are established separately
by the exact release manifest and production verification receipts.

## Local tools for existing GIFs

These tools operate locally and do not call an image model:

- Explicit GIF import, owner-only access, and recent imported/derived GIF history.
  Ordinary static model-input upload still rejects GIFs.
- Speed adjustment from 0.1x to 10x by changing frame delays. The tool preserves
  animation pixels, frame order and loop settings. It rejects missing/zero delays
  or an unrepresentable target delay; GIF timing is rounded to 10 ms. Some players
  clamp very short delays, so their observed playback speed may differ.
- Frame selection, PNG preview/download, and replacement with a same-size static
  PNG. Replacement creates a new GIF linked to its source, preserving the source
  and unaffected frames. Repeated saves use an idempotency key.
- The user can edit the downloaded PNG with Lumen's existing raster editor or
  another tool, then upload the resulting PNG to replace that frame. This is not
  a native GIF-input AI editing API or an automatic AI-edit-and-refill workflow.
- Replacement uses a GIF palette of at most 256 colors. Alpha below 128 becomes
  transparent and other pixels become opaque. Rare palette cases that cannot
  preserve unaffected frames are rejected rather than degrading them silently.
- Import/processing limits: <=32 MiB, <=64 decoded frames and <=64 MP in total.
  Processing runs under existing capacity leases in an isolated child process,
  with bounded upload hashing, existing durable upload staging, ownership checks,
  active-user/CSRF checks and safe error mapping.

## Official contract differences

Checked on 2026-11-05. The ChatGPT product announcement is not an API contract.

The Images edit reference documents GIF output, 2–16 frames, default 9, and
`frame_duration_ms` default 200 with minimum 20, 10 ms encoding granularity and
encoded maximum 655,350 ms. The generation guide still says GIF is not supported
by edits, and gives 2–64 frames with 20–2000 ms duration. Lumen uses the conservative
shared request range. Local decoding retains its separate 64-frame safety limit.

The SVG guide explicitly limits SVG output to generations and says edits do not
support SVG output or SVG input. The current edits request enum lists
PNG/JPEG/WebP/GIF but not SVG. Its response
enum and complexity prose mention SVG; that is not proof of a request contract.
The initial integration exposed an experimental native SVG edit option; the
report-driven correction removes that path. The live capability report confirmed
native SVG edits were rejected by the tested gateway, while GIF edits succeeded
for its tested credential. Those results do not establish entitlement for every
Lumen account or replace validation of this code's own request flow.

No public GIF-input, existing-GIF retiming, or frame-index editing contract was
found. The local tools above provide those mechanical operations independently.

## Compatibility and safety

- Image 2/2.5 are removed from new-request choices. Historical records and saved
  graphs remain intact. New rerolls and Canvas executions upgrade retired IDs
  to the corresponding 2.6 family; Image 2 maps to Flare.
- Previously admitted worker/sidecar tasks retain their model and idempotency
  identity. New optional controls do not alter ordinary request fingerprints.
- SVG validation rejects scripts, events, foreignObject, embedded images, styles,
  external references, DTD/entities and processing instructions. Basic shapes,
  paths, text, gradients, clips and masks are supported. SVG originals download
  as attachments with CSP sandbox and nosniff, never inline HTML.
- Native SVG limits: 10 MiB, 100,000 nodes, depth 64, side <=10,000 and <=64 MP.
  The separate vision workflow is stricter: 1 MiB, 10,000 elements, side <=8,192
  and <=16 MP; its source is <=20 MiB and <=16 MP. GIF decode limits are <=64 frames
  and <=64 MP total. These are application safety budgets.
- SVG requests preflight the renderer before any upstream call. Animated GIF
  originals and WebP previews retain timing/loop; thumbnails are static.
- No historical data migration, billing-rate change or credential change is
  part of this feature. Native image output retains existing per-output billing.
  Explicit vision SVG uses actual Completion text-token pricing, not image-tool
  charges; incomplete usage is held for reconciliation rather than guessed.

## Historical integration verification

Unit/request-capture tests cover active-model selection, legacy replay, schema
validation, control propagation, markup ownership and PNG preservation, native
edit multipart fidelity, actual returned-format validation, SVG safety, animation
preservation, local GIF editing, upload idempotency and safe SVG downloads.
Mock calls do not establish live model availability, preview entitlement or
temporal quality.

Pre-release local full runs passed 1,079 core tests, 387 image-job tests,
8 mock-upstream tests, 190 Telegram-bot tests and 1,212 frontend unit tests.
The final GIF parser refactor separately passed all 74 GIF safety/editing tests.
Focused checks and browser flows are rerun after subsequent UI changes; these
counts do not substitute for CI on the exact published commit.

The API full run had 2,591 passes, 35 skips and one child-process timeout; that
unchanged test passed in isolation. The worker run had 2,559 passes, 9 skips and
two timing-sensitive failures that passed unchanged in isolation. Three other
worker agent-wire tests were excluded after local Node IPC failures; the runtime
also rejected a TMPDIR override. They are not counted as passed. Local ops,
dead-code and repository-inventory checks timed out and remain incomplete.
The native SVG rendering test is skipped locally because libvips is unavailable.
These environment limits must remain visible until exact-commit CI and the VPS
runtime checks establish their respective outcomes.

Four initial markup/model-selection browser cases passed in isolated desktop
and mobile-sized system Chrome contexts. They cover EXIF-oriented transparent
markup upload, cancellation and repeated confirmation. Local-GIF browser tests
also exercise import, retime, frame replacement, repeated clicks and reopening.
Mobile Chrome is not Safari; Safari/WebKit remains unverified.

The local machine has pyvips but no native libvips. Native processing was therefore
verified separately in isolated VPS runtime containers, as recorded below and in
the live report's implementation ledger. Capability detection alone was not
counted as a rendering test.

The public Responses image-generation schema still listed 2.5 when checked.
2.6 Responses compatibility and 2.6-specific maximum raster side/pixel limits
remain unverified; existing 4K raster presets are unchanged. No paid generation
or credentialed live model probe was included in these code-validation runs.
The later capability report records its separate, already-completed live tests.

## Published runtime verification

On 2026-11-05, v1.2.183's exact-commit full CI passed, including the local
agent-wire, dead-code and inventory checks that had been blocked in the initial
local runs. A subsequent isolated VPS test found that libvips could not
auto-detect the validator's namespace-prefixed SVG. v1.2.184 fixes both rendering
branches by explicitly selecting the SVG loader after validation.

The v1.2.184 exact-commit full CI also passed: 1,212 frontend unit tests,
802 browser tests (87 skipped), 2,581 worker tests (5 skipped), 2,615 API tests
(12 skipped), 1,078 core tests (1 skipped), 1,024 ops tests (5 skipped),
190 Telegram-bot tests, 387 image-job tests and 8 mock-upstream tests. These
results belong to v1.2.184, not to later uncommitted SVG author or GIF sizing work.

The v1.2.184 release pipeline passed, and its unmodified immutable ARM64 Worker
image passed 30 synthetic SVG/GIF checks plus a direct SVG rasterization check
on the VPS, without production mounts, network access or model credentials.
The production update completed with a new paired PostgreSQL/Redis backup,
preserved the v1.2.181 rollback release, and passed health and image-identity
checks. These are processing and deployment checks, not a live model API test.

A real v1.2.184 SVG generation subsequently failed with HTTP 400
`missing_required_parameter`: the persisted request omitted `svg`, while
the provider required `svg.model`. This exposed a request-contract gap despite
the passing native rendering tests. Author configuration and transport-capture
regressions address that separate gap; a supported author ID and successful
live API behavior must not be inferred from synthetic results.

## v1.2.185 pre-release validation snapshot

The [live capability report](image26-live-capability-report-2026-11-05.md) maps all
eight recommendations to implemented behavior, test evidence and limits.
Final frontend validation passed 1,231 unit tests, type checking, complete lint
(including architecture/complexity), a full production build and 26 desktop/mobile
Chrome cases with standard 45-second test limits and zero retries. Actual screenshot
inspection caught and fixed narrow-screen raw-SVG overflow. Original SVG download
bytes, failed local repair followed by fresh GET and successful second local repair,
GIF timing/loop diagnostics and author advice were exercised. Temporary test
configuration was removed and the optimized TypeScript configuration was restored
with semantic/SHA guards, followed by another passing type check.

Native-contract checks passed core 172, API 17 and worker 142 tests (one local
libvips skip). The separate Completion workflow passed worker 365 and API 61 tests;
combined changed-test checks then passed core 68, API 91 and worker 117. These
suites overlap and must not be summed as unique coverage. Final scoped Ruff and
diff checks passed. An independent review found and fixed an inexact-token fallback
that could charge estimated usage; it now enters reconciliation without charge.

A network-disabled, read-only, non-root VPS container based on the immutable
v1.2.184 Worker runtime, with only the final vision module/checkpoint overlays,
passed 38 checks using real libvips 8.16.1 and LocalStorage. Original bytes, three
previews, four-file persistence, unsafe SVG rejection, stable identities, orphan-file
adoption and terminal local repair were verified. Database/session/event boundaries
were in-memory substitutes, not PostgreSQL concurrency or production model tests.
No new paid model call, production change or dependency install was performed
during this implementation-validation phase. The user subsequently authorized the
v1.2.185 release and deployment after final image verification. Safari and real
PostgreSQL concurrent integration remain unverified; this snapshot does not replace
exact-commit CI, final published-container checks or the deployment receipt.

## Sources

- https://developers.openai.com/api/docs/models/gpt-image-2.6-flare
- https://developers.openai.com/api/docs/models/gpt-image-2.6-sunburst
- https://developers.openai.com/api/reference/resources/images/methods/edit
- https://developers.openai.com/api/reference/python/resources/images/methods/edit
- https://developers.openai.com/api/reference/resources/images/methods/generate
- https://developers.openai.com/api/docs/guides/image-generation#generate-svg-output
- https://developers.openai.com/api/docs/guides/image-generation#generate-animated-gif-output
- https://developers.openai.com/api/docs/guides/image-generation#guide-an-edit-with-markup
- https://developers.openai.com/api/docs/guides/image-generation#set-image-complexity
- https://developers.openai.com/api/docs/guides/image-prompting#refine-an-image-across-turns
- https://openai.com/index/introducing-images-2-6/
