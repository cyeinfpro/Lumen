# Mock Image Upstream

独立可运行的图片上游故障模拟器，用于 release 前 image stability check。
它不依赖 Lumen 业务代码，覆盖 OpenAI Images API、Responses
`image_generation`、以及 Lumen image-jobs submit/poll/result 的最小协议。

仅用于本地联调和 CI/smoke 场景；根目录 `.dockerignore` 已排除 `tools/`，
不会进入生产 Docker 镜像或部署包。

## Start

```bash
python3 tools/mock-image-upstream/server.py --port 8787
```

常用参数：

```bash
python3 tools/mock-image-upstream/server.py \
  --host 127.0.0.1 \
  --port 8787 \
  --scenario success_b64 \
  --slow-delay-ms 31000
```

环境变量同名可用：

- `MOCK_IMAGE_UPSTREAM_HOST`
- `MOCK_IMAGE_UPSTREAM_PORT`
- `MOCK_IMAGE_UPSTREAM_SCENARIO`
- `MOCK_IMAGE_UPSTREAM_DELAY_MS`
- `MOCK_IMAGE_UPSTREAM_SLOW_DELAY_MS`

## Scenarios

| Scenario | Purpose |
| --- | --- |
| `success_b64` | OpenAI Images API returns `data[0].b64_json`; Responses returns `output[0].result`. |
| `success_url` | Images API returns `data[0].url` pointing at `/assets/generated.png`. |
| `unauthorized_401` | HTTP 401 OpenAI-style error body. |
| `rate_limit_429` | HTTP 429 OpenAI-style error body plus `Retry-After`. |
| `server_error_500` | HTTP 500 OpenAI-style server error body. |
| `invalid_json` | HTTP 200 with broken JSON body. |
| `slow_response` | Delays response; defaults to 31s unless overridden. |
| `revised_prompt` | Successful image response with `revised_prompt`. |
| `url_404` | Successful API response points at an image URL that returns 404. |
| `url_expired` | Successful API response points at an image URL that returns 403 expired. |
| `url_cors_blocked` | Successful API response points at an image URL without CORS headers. |
| `async_success` | `POST /v1/image-jobs` then poll until a succeeded result URL. |
| `async_failed` | `POST /v1/image-jobs` then poll until a failed sidecar-style job. |
| `actual_size_missing` | Successful Images API response without `actual_size`. |

Aliases such as `401`, `429`, `500`, `slow`, `timeout`, `url`, and `async`
are accepted.

## Change Scenario

Set the process-wide default:

```bash
curl -s http://127.0.0.1:8787/scenario/rate_limit_429
```

Override per request with a query string or header:

```bash
curl -s http://127.0.0.1:8787/v1/images/generations?scenario=success_url \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-image-1","prompt":"smoke","size":"1024x1024"}'

curl -s http://127.0.0.1:8787/v1/responses \
  -H 'Content-Type: application/json' \
  -H 'X-Mock-Image-Scenario: revised_prompt' \
  -d '{"model":"gpt-5.1","stream":false,"input":[],"tools":[{"type":"image_generation"}]}'
```

Delay override for timeout tests:

```bash
curl -s 'http://127.0.0.1:8787/v1/images/generations?scenario=slow_response&delay_ms=35000' \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-image-1","prompt":"slow","size":"1024x1024"}'
```

## Endpoints

- `GET /health`
- `GET /scenarios`
- `GET|POST /scenario/{name}`
- `POST /v1/images/generations`
- `POST /v1/images/edits`
- `POST /v1/responses`
- `POST /v1/image-jobs`
- `GET /v1/image-jobs/{job_id}`
- `POST /v1/refs`
- `GET|HEAD /assets/{name}.png`

Responses supports both JSON fallback and SSE:

```bash
curl -N http://127.0.0.1:8787/v1/responses?scenario=revised_prompt \
  -H 'Accept: text/event-stream' \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-5.1","stream":true,"input":[],"tools":[{"type":"image_generation"}]}'
```

Image-jobs smoke:

```bash
JOB_ID="$(curl -s 'http://127.0.0.1:8787/v1/image-jobs?scenario=async_success&polls=1' \
  -H 'Content-Type: application/json' \
  -d '{"request_type":"generations","endpoint":"/v1/images/generations","body":{}}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["job_id"])')"

curl -s "http://127.0.0.1:8787/v1/image-jobs/${JOB_ID}"
curl -s "http://127.0.0.1:8787/v1/image-jobs/${JOB_ID}"
```

## Smoke Tests

```bash
uv run pytest tools/mock-image-upstream/tests
```

The tests start the server on a random local port and verify b64, URL assets,
401/429/500, invalid JSON, slow response override, revised prompt SSE, and
async submit/poll/result.
