# Lumen 部署

## systemd 服务
- `systemd/lumen-{api,worker,web,tgbot}.service` 四个核心服务
- `systemd/lumen-backup.{service,timer}` 每日数据备份
- `systemd/lumen-health-watchdog.{service,timer}` 每分钟探测本机 API/Web；进程 active 但无响应时自动重启 API/Web，API 重启前会请求 Python 栈 dump 进 journald

## image-job sidecar

`image-job` 是异步图像任务的 sidecar 进程，独立部署，监听 `127.0.0.1:8091`。
worker 通过 sidecar 把参考图转成短 URL，避免 base64 内联到上游请求。
它必须绑定一个已运行的 sub2api/OpenAI 兼容上游；`scripts/lumenctl.sh install-image-job`
会让你填写实际上游 base URL，例如本机常见默认值 `http://127.0.0.1:8081`，也可以是其他端口、内网地址或公网反代地址；脚本会探测你填写的地址，不可达时会中止安装。

**源码唯一真相**：仓库根 `image-job/app.py`。`deploy/image-job/` 只放部署模板。

发布步骤：

```bash
bash scripts/lumenctl.sh install-image-job
```

> `image-job.service` 里的 `IMAGE_JOB_PUBLIC_BASE_URL=https://example.com` 必须改成 sidecar 对外可达的真实 URL（caller 拿到的 ref URL 就是基于这个 base 拼的）。

## nginx 反代

仓库提供三份 nginx 配置示例，按角色分工：

| 文件 | 角色 | 用法 |
|------|------|------|
| `nginx.conf.example` | **主反代**（`lumen.example.com`） | 反代 Next.js + API，含 SSE / body size / TLS / 限流。整段拷到 `sites-available/lumen.conf`。 |
| `image-job/image-job.example.com.conf` | **image-job 独立子域** | sidecar 用独立域名（如 `img.example.com`）暴露时用。完整 server block，含 ACME + TLS。 |
| `image-job/nginx-image-job.locations.conf` | **image-job location 片段** | 想把 sidecar 挂在主站 `/v1/image-jobs` 路径下时用。`include` 进主反代的 `server {}` 即可。 |

**两种 image-job 部署模式二选一**：

```nginx
# 模式 A: 独立子域 (推荐生产用)
# sites-available/img.conf —— 直接拷 image-job.example.com.conf

# 模式 B: 共用主站
# sites-available/lumen.conf 的 server {} 末尾：
include /etc/nginx/snippets/nginx-image-job.locations.conf;
```

也可以使用统一 nginx 反代向导：

```bash
bash scripts/lumenctl.sh nginx-optimize
```

向导支持四类配置：

- Lumen 反代：Next.js、`/api/`、`/events` SSE。
- sub2api 单机公网反代：公网域名直接代理到本机 sub2api。
- sub2api 内层/外层两段反代：sub2api 所在机器先由本机 nginx 代理，公网域名所在机器再代理到这个内层地址。
- image-job 路由注入：给已有站点自动备份并注入 `/v1/image-jobs`、`/v1/refs`、`/images/temp/`、`/refs/`。

**关键不变量（修改前必读 `nginx.conf.example` 顶部注释）**：
- `proxy_buffering off` —— SSE 必需
- `proxy_request_buffering off` —— 大图上传不要先缓存到磁盘
- `client_max_body_size 60m` —— 与前端上传上限对齐
- `proxy_read_timeout 600s` —— 4K 图像 timeout 分层的反代层
- `gzip off` —— SSE 帧不能压缩

> `sites-enabled/` 是 `include sites-available/*` 而非 `*.conf`，备份文件别留在 `sites-enabled/` 下，否则会被加载导致 nginx -t 失败。

## 系统依赖

Debian/Ubuntu 目标机安装 Python 依赖前先准备编译与 libpq 头文件：

```bash
sudo apt-get update
sudo apt-get install -y build-essential libpq-dev
```

## 发布流程

1. rsync 代码到目标机（**必须排除 `apps/worker/var/` 整目录**，否则会覆盖用户数据）
2. **`sudo deploy/scripts/sync_env_version.sh`**（刷新 LUMEN_VERSION 为当次 commit hash）
3. `bash scripts/update.sh`（同步依赖、迁移、可选 build、重启服务并执行 API/Web/Worker 健康检查）
4. 如果改了 `image-job/app.py`：`cp image-job/app.py /opt/image-job/ && systemctl restart image-job`
5. 首次安装或 watchdog 变更后：`systemctl enable --now lumen-health-watchdog.timer`
6. 验证：`grep '^LUMEN_VERSION=' /opt/lumen/.env` 应为当次 commit 短 hash

## 环境变量约定

- 所有变量集中在 `/opt/lumen/.env`，通过 `EnvironmentFile=-` 注入到 service
- 不要在 systemd unit 内 hardcode `Environment=KEY=value`，统一走 .env 便于轮换
- `image-job.service` 是个例外（它在 sidecar 自己的 unit 里写 `Environment=`），因为它不读 lumen .env

## 发布前测试

- 仓库根 `bash scripts/test.sh` 按 worker / api / core 三个子进程分别跑（同进程合跑会有全局状态污染）。
- CI 拆 job 时按相同 3 段切分。
