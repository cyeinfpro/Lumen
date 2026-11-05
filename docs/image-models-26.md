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
  Text-to-SVG is documented. Image-to-SVG is an explicit provider-dependent
  experimental request: the current edits request enum omits SVG, although its
  response enum includes it. The UI describes this uncertainty. A real upstream
  rejection remains visible, rather than silently converting a raster image.
- GIF output supports text prompts and static PNG/JPEG/WebP references through
  the native Images endpoint. The latest edits reference documents GIF output
  and requires the selected model's GIF preview access. No invented preview
  parameter is sent. Account entitlement and live provider behavior remain unverified.
- GIF requests use count 1, 2–16 frames (default 9), and 20–2000 ms per frame in
  10 ms increments (default 200). Count 1 and the shared conservative ranges are
  Lumen restrictions, not a claim that all official references agree. GIF frame
  dimensions are positive multiples of 16; 512x512 is valid.
- SVG/GIF edits preserve the original static reference bytes and order.
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
The user-requested SVG edit option is therefore labeled provider-dependent and
experimental. No successful live SVG/GIF model call is claimed.

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
- SVG limits: 10 MiB, 100,000 nodes, depth 64, side <=10,000 and <=64 MP. GIF decode
  limits are <=64 frames and <=64 MP total. These are application safety budgets.
- SVG requests preflight the renderer before any upstream call. Animated GIF
  originals and WebP previews retain timing/loop; thumbnails are static.
- No historical data migration, billing-rate change or credential change is
  part of this feature. Existing per-output billing rules still apply.

## Verification

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

The local machine has pyvips but no native libvips. The existing VPS runtime has
libvips SVG support; final release acceptance must test synthetic native SVG/GIF
processing inside an isolated runtime image without production mounts, network
or model credentials. Capability detection alone is not a rendering test.

The public Responses image-generation schema still listed 2.5 when checked.
2.6 Responses compatibility and 2.6-specific maximum raster side/pixel limits
remain unverified; existing 4K raster presets are unchanged. No paid generation
or credentialed live model probe is included in this validation.

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
