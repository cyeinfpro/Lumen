# Seedance 2.5 official contract

Verified on 2026-08-17 against Volcengine ModelArk documentation updated on
2026-08-17.

## Sources

- Tutorial and capability matrix:
  `https://docs.volcengine.com/docs/82379/2607688`
- Create video generation task API:
  `https://docs.volcengine.com/docs/82379/1520757`
- Model pricing:
  `https://docs.volcengine.com/docs/82379/1544106`

## Model identity

- Lumen model key: `seedance-2.5`
- Volcengine model ID: `doubao-seedance-2-5-260628`

## Output capabilities

- Resolutions: `480p`, `720p`, `1080p`
- Ratios: `adaptive`, `21:9`, `16:9`, `4:3`, `1:1`, `3:4`, `9:16`
- Duration: integer seconds from `4` through `30`, or `-1` for model-selected
  duration
- Audio generation: supported
- Output formats: `mp4`, `mov`; Lumen currently requests the default `mp4`

Official output pixel dimensions:

| Resolution | Ratio | Pixels |
| --- | --- | --- |
| 480p | 16:9 | 854 x 480 |
| 480p | 4:3 | 752 x 560 |
| 480p | 1:1 | 640 x 640 |
| 480p | 3:4 | 560 x 752 |
| 480p | 9:16 | 480 x 854 |
| 480p | 21:9 | 992 x 432 |
| 720p | 16:9 | 1280 x 720 |
| 720p | 4:3 | 1112 x 834 |
| 720p | 1:1 | 960 x 960 |
| 720p | 3:4 | 834 x 1112 |
| 720p | 9:16 | 720 x 1280 |
| 720p | 21:9 | 1470 x 630 |
| 1080p | 16:9 | 1920 x 1080 |
| 1080p | 4:3 | 1664 x 1248 |
| 1080p | 1:1 | 1440 x 1440 |
| 1080p | 3:4 | 1248 x 1664 |
| 1080p | 9:16 | 1080 x 1920 |
| 1080p | 21:9 | 2206 x 946 |

Seedance 2.5 1080p output uses 10-bit color and H.265/HEVC encoding. Clients
must not assume every browser or operating system can decode it natively.

## Reference media

Content roles:

- First frame: `first_frame`
- Last frame: `last_frame`
- Reference image: `reference_image`
- Reference video: `reference_video`
- Reference audio: `reference_audio`

Per-request limits:

- Up to 30 reference images
- Up to 10 reference videos
- Up to 10 reference audio clips
- Audio-only reference generation is supported

Image requirements:

- Formats: JPEG, PNG, WebP, BMP, TIFF, GIF, HEIC, HEIF
- Width/height ratio: `0.4` through `2.5`
- Each side: 300 through 6000 pixels
- Each image: less than 30 MB
- JSON request body: no more than 64 MB when Base64 content is used

Video requirements:

- Containers: MP4 or MOV
- Each video: 2 through 30 seconds
- All reference videos combined: no more than 30 seconds
- Each video: no more than 200 MB
- Frame rate: 24 through 60 FPS

Audio requirements:

- Formats: WAV or MP3
- Each audio clip: 2 through 30 seconds
- All reference audio combined: no more than 30 seconds
- Each audio clip: no more than 15 MB

Lumen uses authenticated public reference URLs instead of embedding image
Base64 data in the upstream request.

## Pricing

Online inference list rates:

- 480p/720p with no input video: 70 RMB per million completion tokens
- 480p/720p with input video: 42 RMB per million completion tokens
- 1080p with no input video: 77 RMB per million completion tokens
- 1080p with input video: 46 RMB per million completion tokens

Volcengine documents a temporary 72% price multiplier for Seedance 2.5 1080p
from 2026-08-14 14:00 through 2026-09-17 14:00 (UTC+8). Lumen keeps durable
default pricing at the list rate; operators can apply a temporary override
without creating an undercharge after the promotion ends.

Official token estimate:

`(input video seconds + output video seconds) * output width * output height * 24 / 1024`

The API's `usage.completion_tokens` remains authoritative for settlement.
Lumen reserves a conservative hold before submission and settles against the
returned completion-token count after the task finishes.

Official 5-second, 16:9 examples:

| Input | Resolution | Price |
| --- | --- | --- |
| No video | 480p | 3.36 RMB |
| No video | 720p | 7.56 RMB |
| No video | 1080p | 18.71 RMB |
| 2-30s video | 480p | 3.63-14.12 RMB |
| 2-30s video | 720p | 8.16-31.75 RMB |
| 2-30s video | 1080p | 20.12-78.25 RMB |
