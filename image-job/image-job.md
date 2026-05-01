# image-job 部署与接入说明

更新时间：2026-04-30

`image-job` 是一个异步生图 sidecar。调用方把图片请求提交给它，它把请求转发给本机上游图片 API，等待上游同步返回后把图片保存到临时目录，并返回可公网访问的图片 URL。

典型链路：

```text
<CALLER_APP>
  -> POST /v1/image-jobs
  -> image-job 队列
  -> IMAGE_JOB_UPSTREAM_BASE_URL + endpoint
  -> 保存图片到 IMAGE_JOB_DATA_DIR/images/temp
  -> GET /v1/image-jobs/{job_id}
  -> <CALLER_APP> 下载/复制图片到自己的正式存储
```

本文档已脱敏。部署时把 `<SERVER_HOST>`、`<SSH_USER>`、`https://example.com`、`<CALLER_APP>`、`<DB_PASSWORD>` 等占位符替换成你自己的值。

## 一、部署目标

部署后需要提供三个能力：

```text
POST https://example.com/v1/image-jobs
GET  https://example.com/v1/image-jobs/{job_id}
GET  https://example.com/images/temp/...
```

推荐路径和端口：

```text
应用目录: /opt/image-job
数据目录: /opt/image-job/data
状态目录: /var/lib/image-job/state
SQLite:   /var/lib/image-job/state/image_jobs.sqlite3
监听:     127.0.0.1:8091
上游:     http://127.0.0.1:8081
公网:     https://example.com
```

## 二、部署前提

服务器需要具备：

```text
Python 3.11+
可运行 uvicorn / fastapi / httpx / pillow
本机上游图片 API 已启动，例如 http://127.0.0.1:8081
nginx 可代理 /v1/image-jobs 并静态暴露 /images/temp/
systemd 可管理 image-job 服务
```

上游图片 API 必须能接收调用方传来的 `Authorization: Bearer ...`。`image-job` 不生成上游 key，只转发提交任务时收到的 Authorization header。

## 三、文件放置

把项目文件放到服务器：

```bash
ssh <SSH_USER>@<SERVER_HOST> "sudo mkdir -p /opt/image-job /opt/image-job/data /var/lib/image-job/state"
rsync -av --delete ./image-job/ <SSH_USER>@<SERVER_HOST>:/opt/image-job/
```

确认服务器上至少有：

```text
/opt/image-job/app.py
/opt/image-job/README.md
/opt/image-job/image-job.md
```

数据目录需要服务进程可写：

```bash
ssh <SSH_USER>@<SERVER_HOST> "sudo chown -R <SERVICE_USER>:<SERVICE_GROUP> /opt/image-job /var/lib/image-job"
```

如果你直接用 root 跑 systemd，可以不改 owner，但生产环境建议使用独立服务用户。

## 四、Python 环境

创建虚拟环境并安装依赖。依赖文件如果由上层项目统一管理，可按你的项目方式安装；最小依赖如下：

```bash
ssh <SSH_USER>@<SERVER_HOST> "python3 -m venv /opt/image-job/.venv"
ssh <SSH_USER>@<SERVER_HOST> "/opt/image-job/.venv/bin/pip install -r /opt/image-job/requirements.txt"
```

启动命令中的 Python 路径要和 systemd 的 `ExecStart` 保持一致。

## 五、环境变量

可直接参考 `.env.example` 或 systemd 文件。核心配置：

```text
IMAGE_JOB_UPSTREAM_BASE_URL=http://127.0.0.1:8081
IMAGE_JOB_PUBLIC_BASE_URL=https://example.com
IMAGE_JOB_ROOT_DIR=/opt/image-job
IMAGE_JOB_DATA_DIR=/opt/image-job/data
IMAGE_JOB_STATE_DIR=/var/lib/image-job/state
IMAGE_JOB_DB_PATH=/var/lib/image-job/state/image_jobs.sqlite3
IMAGE_JOB_CONCURRENCY=2
IMAGE_JOB_UPSTREAM_TIMEOUT_S=1800
IMAGE_JOB_RETENTION_DAYS=1
IMAGE_JOB_MAX_RETENTION_DAYS=1
IMAGE_JOB_JOB_TTL_DAYS=1
```

关键含义：

```text
IMAGE_JOB_UPSTREAM_BASE_URL  上游同步图片 API 地址，不带末尾 /
IMAGE_JOB_PUBLIC_BASE_URL    返回给调用方的公网域名，不带末尾 /
IMAGE_JOB_DATA_DIR           临时图片落盘目录的根目录
IMAGE_JOB_DB_PATH            任务状态 SQLite 文件，必须放本机磁盘
IMAGE_JOB_CONCURRENCY        同时处理的图片任务数
IMAGE_JOB_UPSTREAM_TIMEOUT_S 单个上游请求最长等待时间
```

不要把真实 API Key、Bearer token、数据库密码写进环境变量示例、文档或日志。

## 六、systemd 服务

写入 `/etc/systemd/system/image-job.service`：

```ini
[Unit]
Description=sub2api image async job sidecar
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/image-job
Environment=IMAGE_JOB_UPSTREAM_BASE_URL=http://127.0.0.1:8081
Environment=IMAGE_JOB_PUBLIC_BASE_URL=https://example.com
Environment=IMAGE_JOB_ROOT_DIR=/opt/image-job
Environment=IMAGE_JOB_DATA_DIR=/opt/image-job/data
Environment=IMAGE_JOB_STATE_DIR=/var/lib/image-job/state
Environment=IMAGE_JOB_DB_PATH=/var/lib/image-job/state/image_jobs.sqlite3
Environment=IMAGE_JOB_CONCURRENCY=2
Environment=IMAGE_JOB_UPSTREAM_TIMEOUT_S=1800
ExecStart=/opt/image-job/.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8091
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

启用并启动：

```bash
ssh <SSH_USER>@<SERVER_HOST> "sudo systemctl daemon-reload"
ssh <SSH_USER>@<SERVER_HOST> "sudo systemctl enable --now image-job"
ssh <SSH_USER>@<SERVER_HOST> "systemctl status image-job --no-pager"
```

看日志：

```bash
ssh <SSH_USER>@<SERVER_HOST> "journalctl -u image-job -n 160 --no-pager"
```

## 七、nginx 配置

需要两段路由：

```nginx
location ^~ /v1/image-jobs {
  client_max_body_size 100M;
  proxy_pass http://127.0.0.1:8091;
  proxy_http_version 1.1;
  proxy_set_header Host $host;
  proxy_set_header X-Real-IP $remote_addr;
  proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
  proxy_set_header X-Forwarded-Proto $scheme;
  proxy_connect_timeout 30s;
  proxy_send_timeout 60s;
  proxy_read_timeout 60s;
}

  location ^~ /images/temp/ {
    alias /opt/image-job/data/images/temp/;
    try_files $uri =404;
    expires 1d;
    add_header Cache-Control "public, max-age=86400" always;
  }
```

如果这个域名还要继续代理上游 API，可以保留默认路由：

```nginx
location / {
  proxy_pass http://127.0.0.1:8081;
  proxy_set_header Host $host;
  proxy_set_header X-Real-IP $remote_addr;
  proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
  proxy_set_header X-Forwarded-Proto $scheme;
}
```

检查并 reload：

```bash
ssh <SSH_USER>@<SERVER_HOST> "sudo nginx -t"
ssh <SSH_USER>@<SERVER_HOST> "sudo systemctl reload nginx"
```

## 八、部署验收

先查本机健康状态：

```bash
ssh <SSH_USER>@<SERVER_HOST> "curl -sS http://127.0.0.1:8091/health"
```

期望返回类似：

```json
{
  "status": "ok",
  "queue_size": 0,
  "queued_known": 0,
  "queue_max": 1000,
  "inflight": 0,
  "concurrency": 2,
  "upstream_base_url": "http://127.0.0.1:8081",
  "data_dir": "/opt/image-job/data",
  "db_path": "/var/lib/image-job/state/image_jobs.sqlite3"
}
```

再查公网路由是否能到 sidecar。这个请求不带 Authorization，期望返回 `401`，说明 nginx 已经把 `/v1/image-jobs` 代理到了 sidecar：

```bash
curl -i https://example.com/v1/image-jobs/img_probe
```

如果返回 `404` 或命中上游默认服务，优先检查 nginx 的 `/v1/image-jobs` location 是否生效。

## 九、调用方怎么接

调用方需要做四件事：

```text
1. 把同步图片请求包装成 image-job 任务。
2. POST /v1/image-jobs，并带上原本给上游使用的 Authorization。
3. 保存返回的 job_id，后台轮询 GET /v1/image-jobs/{job_id}。
4. 成功后下载 images[].url，复制到调用方自己的正式存储。
```

调用方配置建议：

```text
image.primary_route = image_jobs
image.job_base_url = https://example.com
image_jobs_enabled = true
```

`image.job_base_url` 指向 sidecar 公网域名，不要写上游本机地址。`127.0.0.1:8081/v1/image-jobs` 不是这个 sidecar 的入口。

调用方伪代码：

```python
import time
import requests

JOB_BASE_URL = "https://example.com"
AUTH_HEADER = {"Authorization": "Bearer <UPSTREAM_API_KEY>"}


def submit_image_job(endpoint: str, body: dict, request_type: str | None = None) -> str:
    payload = {
        "endpoint": endpoint,
        "body": body,
        "retention_days": 1,
    }
    if request_type:
        payload["request_type"] = request_type

    resp = requests.post(
        f"{JOB_BASE_URL}/v1/image-jobs",
        headers={**AUTH_HEADER, "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["job_id"]


def wait_image_job(job_id: str, timeout_s: int = 1900) -> list[dict]:
    deadline = time.time() + timeout_s
    interval_s = 1

    while time.time() < deadline:
        resp = requests.get(
            f"{JOB_BASE_URL}/v1/image-jobs/{job_id}",
            headers=AUTH_HEADER,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        if data["status"] == "succeeded":
            return data["images"]
        if data["status"] == "failed":
            raise RuntimeError(f"{data.get('error_class')}: {data.get('error')}")

        time.sleep(interval_s)
        interval_s = 5 if interval_s >= 10 else min(interval_s + 1, 10)

    raise TimeoutError(f"image job timed out: {job_id}")
```

## 十、鉴权规则

提交和查询都必须带同一个 Authorization：

```http
Authorization: Bearer <UPSTREAM_API_KEY>
```

规则：

```text
POST 时 image-job 会保存 Authorization 的哈希，并临时保存原始 Authorization 用于转发上游。
任务成功或失败后，原始 Authorization 会从 SQLite 中清掉。
GET 时必须带同一个 Authorization，否则返回 403。
```

这保证一个 key 创建的任务只能被同一个 key 查询。调用方不要把真实 Authorization 写进日志。

## 十一、提交任务

文生图示例：

```bash
curl -sS https://example.com/v1/image-jobs \
  -H "Authorization: Bearer <UPSTREAM_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
    "request_type": "generations",
    "endpoint": "/v1/images/generations",
    "body": {
      "model": "gpt-image-2",
      "prompt": "一张产品图",
      "size": "1024x1024",
      "quality": "medium",
      "output_format": "jpeg",
      "n": 1
    },
    "retention_days": 1
  }'
```

成功返回：

```json
{
  "job_id": "img_20260430_xxxxx",
  "status": "queued",
  "request_type": "generations",
  "endpoint": "/v1/images/generations",
  "relay_url": "http://127.0.0.1:8081",
  "retention_days": 1
}
```

字段说明：

```text
endpoint        必填，上游 API path，只能是允许的图片 endpoint。
body            必填，原本要发给上游的 JSON body。
request_type    可选，不传时按 endpoint 推断。
retention_days  可选，线上建议固定为 1；默认 1。
```

允许的 endpoint：

```text
/v1/images/generations
/v1/images/edits
/v1/responses
/v1beta/models/*
```

## 十二、图生图怎么传

如果调用方已经有公网图片 URL，可以直接放在上游请求体里：

```json
{
  "request_type": "edits",
  "endpoint": "/v1/images/edits",
  "body": {
    "model": "gpt-image-2",
    "prompt": "换成白底产品图",
    "images": [
      {
        "image_url": "https://example.com/path/to/input.png"
      }
    ],
    "size": "1024x1024"
  },
  "retention_days": 1
}
```

如果 `images[].image_url` 或 `mask.image_url` 是 `data:image/...;base64,...`，sidecar 会先把输入图保存到：

```text
/opt/image-job/data/images/temp/...
```

然后把请求体改写成公网 URL 再转发给上游。调用方不需要自己处理这一步。

## 十三、轮询任务

调用方拿到 `job_id` 后轮询：

```bash
curl -sS https://example.com/v1/image-jobs/img_20260430_xxxxx \
  -H "Authorization: Bearer <UPSTREAM_API_KEY>"
```

建议轮询策略：

```text
前 10 秒：每 1 秒一次
之后：每 3 到 5 秒一次
总超时：略大于 IMAGE_JOB_UPSTREAM_TIMEOUT_S
```

`queued` / `running` 返回：

```json
{
  "job_id": "img_20260430_xxxxx",
  "status": "running",
  "request_type": "generations",
  "endpoint": "/v1/images/generations",
  "relay_url": "http://127.0.0.1:8081",
  "retention_days": 1,
  "endpoint_used": "/v1/images/generations"
}
```

成功返回：

```json
{
  "job_id": "img_20260430_xxxxx",
  "status": "succeeded",
  "request_type": "generations",
  "endpoint": "/v1/images/generations",
  "relay_url": "http://127.0.0.1:8081",
  "retention_days": 1,
  "upstream_status": 200,
  "elapsed_ms": 80963,
  "image_count": 1,
  "images": [
    {
      "url": "https://example.com/images/temp/2026/04/30/img_20260430_xxxxx/image-1.png",
      "width": 2560,
      "height": 1440,
      "bytes": 5407648,
      "format": "png",
      "expires_at": "2026-05-01T00:00:00+00:00"
    }
  ]
}
```

调用方成功后应尽快下载 `images[].url` 并写入自己的正式存储。`/images/temp/` 是临时文件，过期会被清理。

失败返回：

```json
{
  "job_id": "img_20260430_xxxxx",
  "status": "failed",
  "request_type": "generations",
  "endpoint": "/v1/images/generations",
  "relay_url": "http://127.0.0.1:8081",
  "retention_days": 1,
  "upstream_status": 400,
  "elapsed_ms": 1200,
  "error": "上游返回 HTTP 400",
  "error_class": "upstream_4xx",
  "upstream_body": {
    "error": "..."
  }
}
```

## 十四、错误分类与调用方策略

调用方不要只看 HTTP 状态。任务查询接口自身可能是 200，但任务状态是 `failed`。失败时按 `error_class` 决策：

```text
network       连接、读取、超时问题。建议切换 provider 或稍后重试。
upstream_4xx  上游认为请求格式不对。建议切换 endpoint 或调整请求体。
upstream_5xx  上游服务错误。建议切换 provider 或稍后重试。
no_image      上游 200 但没有可提取图片。建议切换 endpoint。
image_save    图片下载、解码或落盘失败。建议切换 provider 或重试。
internal      sidecar 内部错误。建议切换 provider，并检查 sidecar 日志。
validation    调用方提交参数错误。不要重试，修正请求。
```

推荐状态机：

```text
queued/running -> 继续轮询
succeeded      -> 下载 images[].url，入正式存储，业务任务完成
failed         -> 根据 error_class 决定切 endpoint、切 provider、重试或终止
404            -> job_id 错误或任务已清理
403            -> 查询时 Authorization 和提交时不一致
```

## 十五、sidecar 会做的默认化

对 `/v1/images/generations` 和 `/v1/images/edits`，如果调用方没有显式指定，sidecar 会补默认图片输出选项：

```text
output_format 默认 jpeg
output_compression 默认 0
background 默认 auto
moderation 默认 low
```

如果 `background` 是 `transparent`，会强制使用 `png` 并移除 `output_compression`。

对 `/v1/responses`，sidecar 会扫描 `tools[]` 中 `type=image_generation` 的工具并应用同样规则。

上游最终可能不按请求格式返回图片。例如请求 `webp`，实际可能返回 `png`。调用方应以返回图片的真实内容或 `format` 字段为准。

## 十六、排查命令

查服务：

```bash
ssh <SSH_USER>@<SERVER_HOST> "systemctl status image-job --no-pager"
ssh <SSH_USER>@<SERVER_HOST> "journalctl -u image-job -n 160 --no-pager"
```

查健康状态：

```bash
ssh <SSH_USER>@<SERVER_HOST> "curl -sS http://127.0.0.1:8091/health"
```

查任务库：

```bash
ssh <SSH_USER>@<SERVER_HOST> "<PYTHON_ENV>/bin/python - <<'PY'
import json
import sqlite3

conn = sqlite3.connect('/var/lib/image-job/state/image_jobs.sqlite3')
conn.row_factory = sqlite3.Row
for row in conn.execute(
    'select job_id,status,endpoint,created_at,started_at,finished_at,'
    'elapsed_ms,upstream_status,error_class,error,image_count '
    'from jobs order by created_at desc limit 10'
):
    print(json.dumps(dict(row), ensure_ascii=False))
PY"
```

查临时图片是否存在：

```bash
ssh <SSH_USER>@<SERVER_HOST> "find /opt/image-job/data/images/temp -type f | tail"
```

查公网图片是否可访问：

```bash
curl -I -L https://example.com/images/temp/2026/04/30/img_20260430_xxxxx/image-1.png
```

查调用方业务记录时使用占位符，避免泄露密码：

```bash
ssh <SSH_USER>@<SERVER_HOST> "docker exec -e PGPASSWORD=<DB_PASSWORD> <POSTGRES_CONTAINER> psql -U <DB_USER> -d <DB_NAME> -x -c \"select id,status,error_message from <CALLER_TABLE> order by created_at desc limit 5;\""
```

## 十七、常见问题

### 任务一直 running

通常是 sidecar 正在等待上游同步图片接口返回。看两个日志：

```bash
ssh <SSH_USER>@<SERVER_HOST> "journalctl -u image-job -n 160 --no-pager"
ssh <SSH_USER>@<SERVER_HOST> "docker logs --tail 200 <UPSTREAM_CONTAINER>"
```

### POST 返回 401

调用方没有传 `Authorization: Bearer ...`。sidecar 不接受无鉴权任务。

### GET 返回 403

查询任务时使用的 Authorization 和提交任务时不是同一个。调用方需要用同一个 provider key 查询同一个 job。

### 图片 URL 404

先确认 nginx alias：

```text
/images/temp/ -> /opt/image-job/data/images/temp/
```

再确认文件是否存在：

```bash
ssh <SSH_USER>@<SERVER_HOST> "find /opt/image-job/data/images/temp -type f | tail"
```

### 调用方没有走 image-job

检查调用方配置：

```text
image.primary_route = image_jobs
image.job_base_url = https://example.com
至少一个 provider 的 image_jobs_enabled = true
```

### 队列满

`POST /v1/image-jobs` 可能返回：

```json
{
  "detail": "image job queue full"
}
```

处理方式：

```text
降低调用方并发
调大 IMAGE_JOB_QUEUE_MAX
确认上游没有长时间卡住
必要时调大 IMAGE_JOB_CONCURRENCY
```

## 十八、安全边界

可以记录：

```text
服务路径
接口路径
job_id
非敏感配置项
脱敏后的错误类别
```

不要记录：

```text
Authorization header
Provider key 明文
上游 API key 明文
SSH 密码
数据库密码
私有域名或真实服务器地址
```
