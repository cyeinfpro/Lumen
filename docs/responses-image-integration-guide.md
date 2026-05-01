# Responses Image Integration Guide

> **2026-04-23 更新**：项目实际走的是 direct `POST /v1/images/generations` 与 `/v1/images/edits`，不是本指南主推的 `/v1/responses` 路径；并且 direct 路径已实测 `3840×2160` 4K 可用。因此文内“1.57M 像素预算”只反映某次历史测试的经验值，在当前项目里仅作为 `size_mode=auto` 的默认预算使用；显式 fixed_size 走独立校验（最长边 ≤ 3840 / 宽高 16 对齐 / 总像素 655,360–8,294,400 / 长宽比 ≤ 3:1），详见 `docs/4k-support-upgrade-plan.md` 与 `docs/DESIGN.md §7.2`。

This document is the practical integration guide derived from testing.

Primary recommendation:

- Use `POST /v1/responses`
- Use `tools: [{ "type": "image_generation", ... }]`
- Use the same path for both text-to-image and image-to-image
- Do not use `/v1/images/edits` as the main image-to-image path on this gateway

## Final Recommendation

Use these defaults unless testing proves a better alternative:

- Endpoint: `https://api.example.com/v1/responses`
- Main model: `gpt-5.4`
- Tool type: `image_generation`
- For text-to-image: `action: "generate"`
- For image-to-image: `action: "edit"`
- Use `tool_choice: "required"`
- Prefer `stream: false` for simpler website backends
- Use `size: "auto"` when aspect ratio matters more than exact pixel size
- Use a fixed `size` only when you know it fits the gateway pixel budget
- Treat the current effective gateway pixel budget as approximately `1.57M` pixels

## Why This Path

Tested behavior on this gateway:

- `/v1/images/generations` can generate images, but parameter behavior is inconsistent
- `/v1/images/edits` returns the original image unchanged in tested cases
- `/v1/images/generations` with `image=@reference` also returns the original image unchanged in tested cases
- `/v1/responses` with `image_generation` performs real image generation and real image editing

So the stable product direction is:

- Text-to-image -> `/responses`
- Image-to-image -> `/responses`

## Official Size Rules You Should Assume

For `gpt-image-2`, official constraints are:

- Width and height must be multiples of `16`
- Longest edge must be `<= 3840`
- Aspect ratio must be `<= 3:1`
- Total pixels must be between `655,360` and `8,294,400`

However, this gateway currently behaves as if it has a much smaller effective pixel budget than the official maximum.

Observed successful outputs cluster around about `1.57M` pixels.

Working assumption for implementation:

- Effective gateway pixel budget: about `1.57M` pixels
- In other words, start your size planning from roughly `1,572,864` pixels, then apply ratio and `16`-alignment

Examples:

- `1536 x 1024 = 1,572,864`
- `1915 x 821 ≈ 1,572,215`
- `1535 x 1024 = 1,571,840`
- `1122 x 1402 = 1,573,044`

Practical implication:

- Think in terms of:
  desired aspect ratio + effective gateway pixel budget + 16-aligned dimensions

## Text-to-Image Request

Recommended request body:

```json
{
  "model": "gpt-5.4",
  "input": [
    {
      "role": "user",
      "content": [
        {
          "type": "input_text",
          "text": "A cute small kitten sitting by a window, flat illustration style, soft warm colors, clean outlines."
        }
      ]
    }
  ],
  "tools": [
    {
      "type": "image_generation",
      "action": "generate",
      "output_format": "png",
      "size": "1536x1024"
    }
  ],
  "tool_choice": "required",
  "stream": false,
  "store": false
}
```

What was tested successfully:

- `generate`
- `size: "1536x1024"`
- exact output size was `1536 x 1024`

## Image-to-Image Request

Recommended request body:

```json
{
  "model": "gpt-5.4",
  "input": [
    {
      "role": "user",
      "content": [
        {
          "type": "input_text",
          "text": "Edit this image into a clean flat illustration. Preserve the scene and subject, keep a strict 21:9 ultra-wide cinematic composition, and return an edited image only."
        },
        {
          "type": "input_image",
          "image_url": "data:image/png;base64,..."
        }
      ]
    }
  ],
  "tools": [
    {
      "type": "image_generation",
      "action": "edit",
      "output_format": "png",
      "size": "auto"
    }
  ],
  "tool_choice": "required",
  "stream": false,
  "store": false
}
```

What was tested successfully:

- `edit`
- `size: "auto"`
- prompt-level `21:9` instruction
- actual output ratio came back very close to `21:9`

## When To Use `size: "auto"`

Use `size: "auto"` when:

- you care about aspect ratio more than exact resolution
- you are doing image-to-image edits
- you want the model to preserve a composition like `21:9`, `16:9`, or `9:16`

Prompt example:

```text
Edit this image into a clean flat illustration.
Preserve the subject and scene.
Keep a strict 21:9 ultra-wide cinematic composition.
Use simplified shapes, crisp outlines, limited colors, and minimal shading.
```

## When To Use Fixed Size

Use a fixed `size` when:

- you need a predictable output dimension for a layout slot
- you already know the requested size fits the gateway budget
- you are doing text-to-image or simple controlled edits

Tested working fixed size:

- `1536x1024`

Tested rejected fixed size:

- `2736x3840`

Error returned by the gateway:

```json
{
  "error": {
    "message": "Invalid size '2736x3840'. Requested resolution exceeds the current pixel budget.",
    "type": "image_generation_user_error",
    "param": "tools",
    "code": "invalid_value"
  }
}
```

## Backend Handling Strategy

For a website backend:

1. Accept prompt, mode, optional image upload, and optional requested ratio or size.
2. If ratio matters more than exact size:
   send `size: "auto"` and encode the ratio in the prompt.
3. If exact size matters:
   send a fixed size only if it fits your tested gateway budget.
4. Use `tool_choice: "required"` so the model does not silently return a text-only answer.
5. Decode the returned image and persist it to storage.
6. Return your own image URL and actual dimensions to the frontend.

## Output Handling

If you use `stream: false`:

- Read the final JSON response
- Extract image output from the response body your gateway returns

If you use `stream: true`:

- Parse SSE events
- Capture the last `response.image_generation_call.partial_image`
- Decode `partial_image_b64`

For most production websites, start with:

- `stream: false`

Reason:

- simpler backend logic
- easier retries
- easier observability

Use streaming only if:

- you want progressive image previews
- you need a more interactive UI

## Stability Notes

Observed on this gateway:

- `tool_choice: "auto"` is not stable enough for image workflows
- some requests can rate-limit with:
  `Concurrency limit exceeded for account, please retry later`
- some `auto` requests can degrade into a plain text response instead of invoking the image tool

So:

- prefer `tool_choice: "required"`
- implement retry with backoff for `rate_limit_error`

## Retry Policy

Recommended retry behavior:

- Retry only on retryable transport or rate-limit failures
- Wait `5s`, then `10s`, then `20s`
- Stop after a small number of attempts such as `3`

Do not blindly retry:

- invalid size errors
- invalid payload errors
- unsupported option errors

## What Not To Do

Do not build your website around:

- `/v1/images/edits`
- `/v1/images/generations` with `image=@reference`
- `tool_choice: "auto"` for required image workflows

Do not assume:

- the gateway will honor very large official-valid sizes
- the gateway will always return exact requested dimensions
- `n > 1` will behave reliably

## Minimal Website Product Decision

If you need one clear integration policy:

- Text-to-image:
  `/v1/responses` + `image_generation(generate)` + `tool_choice: "required"`
- Image-to-image:
  `/v1/responses` + `image_generation(edit)` + `tool_choice: "required"`
- Ratio-sensitive output:
  `size: "auto"` + explicit ratio in prompt
- Fixed-size output:
  use only tested-safe sizes, starting with `1536x1024`
