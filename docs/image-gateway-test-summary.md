# Image Gateway Test Summary

> **2026-04-23 更新**：在 direct `/v1/images/generations` 路径上重新实测，`3840×2160` 4K 横图返回 `200` 并拿到正确尺寸的图（样本：`flux-4k-test.png`）。Lumen 项目当前链路正是 direct image path（`apps/worker/app/upstream.py`），因此不再把“1.57M 像素预算”作为项目内硬限制——参见 `docs/4k-support-upgrade-plan.md` 与 `docs/DESIGN.md §7.2`。本摘要其余观察结论保留原样，供对照历史。

Test date: 2026-04-22 to 2026-04-23

Test target:

- Base URL: `https://api.example.com/v1`
- Generation endpoint: `POST /images/generations`
- Edit endpoint: `POST /images/edits`
- Responses endpoint: `POST /responses`
- Model: `gpt-image-2`
- Auth: standard `Authorization: Bearer $OPENAI_API_KEY` header

## Executive Summary

This gateway has two different image access patterns:

- Image API compatibility layer:
  `/images/generations` and `/images/edits`
- Responses tool layer:
  `/responses` with `tools: [{ "type": "image_generation", ... }]`

The two layers do not behave the same.

Observed behavior:

- `size` is not reliably honored
- `n > 1` is not reliably honored
- `size: "auto"` plus an aspect-ratio instruction in the prompt works better than explicit large sizes
- `POST /images/edits` appears to be a no-op in this environment
- `POST /images/generations` with multipart `image=@reference` also appears to be a no-op in this environment
- `POST /responses` with `image_generation` does real image-to-image editing
- `POST /responses` with `image_generation` also does real text-to-image generation
- `POST /responses` can follow aspect ratio guidance for image-to-image when `size: "auto"` is used
- `POST /responses` can honor fixed sizes such as `1536x1024`
- `tool_choice: "auto"` on `/responses` is not fully stable in this environment; `tool_choice: "required"` is more reliable
- The gateway appears to operate with a much smaller effective pixel budget than the official `gpt-image-2` maximum
- The effective gateway pixel budget is approximately `1.57M` pixels based on successful outputs

Conclusion:

- Use `/responses` plus `image_generation` as the primary path for both text-to-image and image-to-image
- `/images/generations` is usable for simple text-to-image, but it is not the preferred integration path after testing
- `/images/edits` is not trustworthy on this gateway
- `generations + image=@reference` is not trustworthy on this gateway
- Not suitable, based on current tests, to rely on `/images/edits`
- Not suitable, based on current tests, to rely on `/images/generations` with `image=@reference`

## Official Model Constraints vs Gateway Behavior

According to the official OpenAI `gpt-image-2` documentation, valid sizes are governed by constraints, not by a short fixed whitelist.

Official constraints:

- Width and height must both be multiples of `16`
- Longest edge must be `<= 3840`
- Aspect ratio must not exceed `3:1`
- Total pixels must be between `655,360` and `8,294,400`

Official note:

- The model supports many valid resolutions, not just a few named presets

Gateway-specific observation:

- This gateway rejects large official-valid sizes on the `/responses` path with:
  `Requested resolution exceeds the current pixel budget`
- The effective gateway pixel budget appears to be about `1.57M` pixels
- Successful outputs cluster around about `1.57M` pixels, for example:
  - `1536 x 1024 = 1,572,864`
  - `1915 x 821 ≈ 1,572,215`
  - `1535 x 1024 = 1,571,840`
  - `1122 x 1402 = 1,573,044`

Finding:

- The official model supports much larger pixel counts than this gateway currently allows
- For this gateway, it is more useful to think in terms of:
  aspect ratio + effective pixel budget + 16-aligned dimensions

## Test Results

### 1. Basic text-to-image generation works

Request shape:

```json
{
  "model": "gpt-image-2",
  "prompt": "A cute small kitten sitting by a window, realistic photography, soft natural light",
  "size": "1024x1024",
  "quality": "low"
}
```

Result:

- HTTP status: `200`
- Output file: `output/imagegen/flux-kitten.png`
- File size: about `2.0 MB`
- Actual image size: `1122 x 1402`

Finding:

- The gateway can generate images
- The returned size did not match the requested `1024x1024`

### 2. Explicit long edge `3840` was accepted, but not honored

Request shape:

```json
{
  "model": "gpt-image-2",
  "prompt": "A cute small kitten sitting by a window, realistic photography, soft natural light",
  "size": "2736x3840",
  "quality": "low"
}
```

Result:

- HTTP status: `200`
- Output file: `output/imagegen/flux-kitten-3840.png`
- Actual image size: `1535 x 1024`
- Returned `revised_prompt`: `阳光下的可爱小猫`

Finding:

- The gateway accepted the request
- The returned image did not respect the requested size
- Prompt rewriting may be happening on the gateway side

### 3. Requesting `n=2` returned only 1 image

Request shape:

```json
{
  "model": "gpt-image-2",
  "prompt": "A cute small kitten sitting by a window, realistic photography, soft natural light",
  "size": "1024x1024",
  "quality": "low",
  "n": 2
}
```

Result:

- HTTP status: `200`
- Requested images: `2`
- Returned items: `1`
- Output file: `output/imagegen/flux-kitten-n2/kitten-1.png`
- Actual image size: `1402 x 1122`
- Returned `revised_prompt`: `窗外绿意中的猫咪`

Finding:

- `n > 1` is not reliable on this gateway
- If multiple images are needed, it is safer to issue multiple single-image requests yourself

### 4. `size: "auto"` plus prompt-level aspect ratio works well

Request shape:

```json
{
  "model": "gpt-image-2",
  "prompt": "A cute small kitten by a window, ultra-wide cinematic composition, strict 21:9 aspect ratio, panoramic framing, realistic photography, soft natural light",
  "size": "auto",
  "quality": "low"
}
```

Result:

- HTTP status: `200`
- Output file: `output/imagegen/flux-kitten-21x9-auto.png`
- Actual image size: `1915 x 821`
- Actual ratio: about `2.3325`
- Target `21:9` ratio: about `2.3333`
- Returned `revised_prompt`: `阳光下的小猫休息时光`

Finding:

- This gateway follows prompt-level aspect-ratio instructions better than explicit size instructions
- For ratio-sensitive generation, `size: "auto"` plus a strong ratio instruction is the best tested approach so far

## Image API Compatibility Layer: Image-to-Image / Reference Tests

### 5. `POST /images/edits` returned the original image unchanged

Input image:

- `output/imagegen/flux-kitten-21x9-auto.png`

Goal:

- Convert the image into a flat illustration style while preserving the same composition

Multiple variants were tested:

- multipart field name `image[]`
- multipart field name `image`
- standard `curl -F image=@...` request to `/images/edits`
- `imagegen` CLI fallback using `client.images.edit(...)`

Outputs:

- `output/imagegen/flux-kitten-21x9-flat.png`
- `output/imagegen/flux-kitten-21x9-flat-v2.png`
- `output/imagegen/test-img2img/output.png`
- `output/imagegen/imagegen-cli-img2img.png`
- `output/imagegen/flux-endpoint-edits-output.png`

Observed behavior:

- All tested variants returned HTTP `200`
- All tested variants returned `b64_json`
- All tested outputs had the exact same SHA-256 hash as the input image

SHA-256:

```text
97e50d15a46fc388683b5269103336e92e241e981d59859834c16f136d48c024
```

Finding:

- `POST /images/edits` appears to be a no-op on this gateway in the tested setup
- It accepts the request shape but returns the original image unchanged
- This behavior was reproduced across multiple clients, so the issue does not appear to be caused by a specific curl payload shape

### 6. `POST /images/generations` with multipart `image=@reference` also returned the original image unchanged

Request pattern:

- Endpoint: `POST /images/generations`
- Content type: `multipart/form-data`
- Included reference file under field name `image`

Goal:

- Use the uploaded image as a reference and generate a new flat illustration variant

Output:

- `output/imagegen/flux-kitten-ref-generate.png`

Observed behavior:

- HTTP status: `200`
- Returned `b64_json`
- Output SHA-256 was identical to the reference image
- Output size remained `1915 x 821`

Finding:

- `generations + image=@reference` also appears to be a no-op on this gateway
- The request is accepted, but the uploaded image is effectively just echoed back

## Responses Tool Layer Tests

### 7. `POST /responses` with `image_generation` successfully performed image-to-image editing

Request pattern:

- Endpoint: `POST /responses`
- Model: `gpt-5.4`
- Tool: `image_generation`
- Action: `edit`
- Stream mode enabled
- Inputs:
  one `input_text` item and one `input_image` item encoded as a data URL

Representative payload shape:

```json
{
  "model": "gpt-5.4",
  "input": [
    {
      "role": "user",
      "content": [
        { "type": "input_text", "text": "Edit this image into a flat illustration style." },
        { "type": "input_image", "image_url": "data:image/png;base64,..." }
      ]
    }
  ],
  "tools": [
    {
      "type": "image_generation",
      "action": "edit",
      "output_format": "png",
      "size": "1536x1024"
    }
  ],
  "tool_choice": "auto",
  "stream": true,
  "store": false
}
```

Observed behavior:

- HTTP status: `200`
- SSE stream included `response.image_generation_call.partial_image`
- Output file: `output/imagegen/test-responses-img2img/output.png`
- Actual image size: `1536 x 1024`
- Output SHA-256 was different from the input SHA-256

Finding:

- `/responses` plus `image_generation` is the real working image-to-image path on this gateway
- This path behaves differently from `/images/edits`

### 8. `POST /responses` can follow a `21:9` aspect ratio for image-to-image edits

Request pattern:

- Endpoint: `POST /responses`
- Tool action: `edit`
- Tool size: `auto`
- Prompt explicitly required `strict 21:9 ultra-wide cinematic composition`

Successful output:

- `output/imagegen/test-responses-img2img-21x9-forced/output.png`
- Actual image size: `1915 x 821`
- Actual ratio: about `2.3325`
- Target `21:9` ratio: about `2.3333`

Finding:

- On the `/responses` path, `size: "auto"` plus a strong ratio instruction can preserve the desired aspect ratio for image-to-image
- This is a ratio-control result, not an exact-pixel-dimensions guarantee

### 9. `POST /responses` also works for text-to-image generation

Request pattern:

- Endpoint: `POST /responses`
- Model: `gpt-5.4`
- Tool action: `generate`
- Tool size: `1536x1024`
- No input image
- `tool_choice: "required"`

Observed behavior:

- HTTP status: `200`
- SSE stream included `response.image_generation_call.partial_image`
- Output file: `output/imagegen/test-responses-generate-fixed-size/output-from-body.png`
- Actual image size: `1536 x 1024`

Finding:

- `/responses` plus `image_generation(action=generate)` is a real working text-to-image path on this gateway
- Fixed size `1536x1024` was honored exactly in this tested case

### 10. Large fixed size `2736x3840` was rejected on `/responses`

Request pattern:

- Endpoint: `POST /responses`
- Tool action: `generate`
- Tool size: `2736x3840`
- `tool_choice: "required"`

Observed behavior:

- HTTP status: `400`
- Error message:
  `Invalid size '2736x3840'. Requested resolution exceeds the current pixel budget.`

Finding:

- The `/responses` path enforces a gateway-specific pixel budget
- This budget is far below the official `gpt-image-2` maximum

### 11. `/responses` is usable, but not perfectly stable under `tool_choice: "auto"`

Observed behavior across retries:

- Some requests returned `rate_limit_error: Concurrency limit exceeded for account, please retry later`
- At least one retry degraded into a normal text response instead of calling the image tool
- For the successful ratio-controlled edit, `tool_choice: "required"` was more reliable than `tool_choice: "auto"`

Finding:

- If you need image editing specifically, do not rely on `tool_choice: "auto"` on this gateway
- Prefer `tool_choice: "required"` for more predictable tool invocation

## Practical Recommendations

### Use this gateway for

- Text-to-image through `/responses` with `image_generation`
- Image-to-image through `/responses` with `image_generation`
- Prompt-driven aspect ratios using `size: "auto"`
- Fixed sizes that fit inside the gateway's effective pixel budget
- Single-image requests

### Do not rely on this gateway for

- Exact output dimensions
- `n > 1`
- `POST /images/edits`
- `POST /images/generations` with `image=@reference`
- `tool_choice: "auto"` if the workflow strictly requires image editing
- Any image-to-image workflow built on the Image API compatibility layer alone

## Suggested Backend Strategy

If this gateway is used in production:

1. Standardize on `/responses` plus `image_generation` for both text-to-image and image-to-image.
2. Treat `/images/generations` as a secondary compatibility path, not as the main integration.
3. Do not use `/images/edits` as the primary image-to-image path on this gateway.
4. For image generation or editing through `/responses`, prefer `tool_choice: "required"` over `auto`.
5. For ratio-sensitive images, prefer `size: "auto"` plus a strong prompt instruction such as `strict 21:9 aspect ratio`.
6. For fixed-size images, compute requested dimensions using:
   desired ratio + gateway pixel budget + width/height aligned down to multiples of `16`
7. Always inspect the actual returned image dimensions instead of trusting the requested `size`.
8. For multi-image generation, issue multiple single-image requests in parallel instead of relying on `n`.
9. If you use the Image API compatibility layer for image-to-image anyway, add a server-side verification step:
   if output hash equals input hash, treat the request as failed or unsupported.

## Saved Files From Testing

- `output/imagegen/flux-kitten.png`
- `output/imagegen/flux-kitten-3840.png`
- `output/imagegen/flux-kitten-n2/kitten-1.png`
- `output/imagegen/flux-kitten-21x9-auto.png`
- `output/imagegen/flux-kitten-21x9-flat.png`
- `output/imagegen/flux-kitten-21x9-flat-v2.png`
- `output/imagegen/flux-kitten-ref-generate.png`
- `output/imagegen/test-img2img/output.png`
- `output/imagegen/imagegen-cli-img2img.png`
- `output/imagegen/flux-endpoint-edits-output.png`
- `output/imagegen/test-responses-img2img/output.png`
- `output/imagegen/test-responses-img2img-21x9-forced/output.png`
- `output/imagegen/flux-edits-prompt-check-output.png`
- `output/imagegen/test-responses-generate-fixed-size/output-from-body.png`
