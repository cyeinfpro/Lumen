# Lumen 部署

Lumen 默认部署方式 = Docker Compose 全栈。本目录的模板按以下角色分工：

| 子目录 / 文件 | 角色 | 默认是否启用 |
| --- | --- | --- |
| `nginx.conf.example` | nginx 反代主模板（Web + /api + /events） | 必须 |
| `image-job/` | 可选异步图片 sidecar | 可选 |
| `systemd/lumen-update-runner.service` | 后台一键更新触发器 | 启用 |
| `systemd/lumen-backup.{service,timer}` | 定时备份 | 启用 |
| `systemd/lumen-{api,worker,web,tgbot}.service` | systemd 兜底 unit，仅 §18.2 回滚使用 | 默认 disable |
| `systemd/lumen-health-watchdog.{service,timer}` | 健康看门狗（容器重启已由 Docker 接管，本 timer 保留为外部可观测用） | 可选 |

完整切换 SOP、镜像标签、Compose 设计、回滚方案见 [`../docs/docker-full-stack-cutover-plan.md`](../docs/docker-full-stack-cutover-plan.md)，本文档只给部署工程师视角的最小指引。

## 全栈 Docker 切换 SOP

新机器一键安装：

```bash
bash scripts/lumenctl.sh install-lumen
```

已有部署切换到 Docker 全栈，按 `docs/docker-full-stack-cutover-plan.md` §17 执行：

1. §17.0 `named volume → /opt/lumendata` 数据迁移（**两件改动同时切，旧 volume 必须先 cp -a 到 bind mount**）
2. §17.1 切换前检查（`docker info`、`docker compose version` >= v2.17，`/opt` 余量）
3. §17.2 备份 `bash scripts/lumenctl.sh backup`
4. §17.3 `docker compose config && docker compose build api worker web`
5. §17.4 停旧 systemd（仅 stop，不 disable）
6. §17.5 `COMPOSE_PROJECT_NAME=lumen docker compose up -d --wait postgres redis`
7. §17.6 `COMPOSE_PROJECT_NAME=lumen docker compose --profile migrate run --rm migrate`（fail-fast，迁移失败立即停止整个切换）
8. §17.7 `COMPOSE_PROJECT_NAME=lumen docker compose up -d --wait api worker web`，按需 `--profile tgbot up -d tgbot`
9. §17.8 验证：`docker compose ps`、`curl http://127.0.0.1:8000/healthz`、`curl http://127.0.0.1:3000/`
10. §17.9 Docker 栈稳定后 `sudo systemctl disable --now lumen-api lumen-worker lumen-web lumen-tgbot`

## 数据目录与权限

默认 `/opt/lumendata` 是所有持久化数据根目录（`LUMEN_DATA_ROOT` 可改）。如果它是 CIFS/NAS，使用 `LUMEN_DB_ROOT` 把 PostgreSQL / Redis 单独放到本机 Linux 文件系统，storage/backup 仍留在 CIFS。**按服务分别 chown，禁止整体 `chown -R 10001:10001`**——否则 PostgreSQL（uid 70）和 Redis（uid 999）启动会失败（参考 `docs/docker-full-stack-cutover-plan.md` §15.2）：

```bash
sudo mkdir -p \
  /var/lib/lumen-data/postgres \
  /var/lib/lumen-data/redis \
  /opt/lumendata/storage \
  /opt/lumendata/backup

sudo chown -R 70:70   /var/lib/lumen-data/postgres
sudo chown -R 999:999 /var/lib/lumen-data/redis
sudo chown -R 10001:10001 /opt/lumendata/storage
sudo chown -R 10001:10001 /opt/lumendata/backup

sudo chmod 700 /var/lib/lumen-data/postgres /var/lib/lumen-data/redis
sudo chmod 750 /opt/lumendata/storage /opt/lumendata/backup

# /opt/lumendata 顶层归 root，不递归
sudo chown root:root /opt/lumendata
sudo chmod 755 /opt/lumendata
```

如果运维人工要往 `/opt/lumendata/backup` 写文件，加 ACL：

```bash
sudo setfacl -R -m u:root:rwx /opt/lumendata/backup
sudo setfacl -R -d -m u:10001:rwx /opt/lumendata/backup
```

对应 `.env`：

```dotenv
LUMEN_DATA_ROOT=/opt/lumendata
LUMEN_DB_ROOT=/var/lib/lumen-data
STORAGE_ROOT=/opt/lumendata/storage
BACKUP_ROOT=/opt/lumendata/backup
```

## 备份与恢复

`scripts/backup.sh` 仍依赖容器名 `lumen-pg` / `lumen-redis`（兼容；`docker-compose.yml` 已固定 `container_name`）。统一入口：

```bash
bash scripts/lumenctl.sh backup           # 等价 scripts/backup.sh
bash scripts/lumenctl.sh restore <ts>     # 等价 scripts/restore.sh <timestamp>
```

输出落盘：

```text
/opt/lumendata/backup/pg/<timestamp>.pg.dump.gz
/opt/lumendata/backup/redis/<timestamp>.redis.tgz
```

`lumen-backup.timer` 默认每 4 小时跑一次，保留最近 `MAX_KEEP=40` 份。

## 后台一键更新（update-runner）

`systemd/lumen-update-runner.service` 是后台 "一键更新" 按钮的执行端，行为：

- 触发链：管理后台写入 `${LUMEN_DEPLOY_ROOT}/.update-trigger` -> `lumen-update-trigger.path` 监听变化 -> 启动 `lumen-update-runner.service`
- runner 默认 `LUMEN_UPDATE_BUILD=0` —— **优先 `docker compose pull` GHCR 预构建镜像**，仅当外部 `EnvironmentFile` 显式置 1 时才本地构建
- runner 用宿主机用户身份调用 `scripts/update.sh`，按阶段输出 `phase=check / backup_preflight / fetch_release / set_image_tag / pull_images / start_infra / migrate_db / switch / restart_services / health_check / cleanup`
- 后台 API 解析这些阶段并实时推送到前端

如要禁用后台一键更新：

```bash
sudo systemctl disable --now lumen-update-trigger.path lumen-update-runner.service
```

## systemd 兜底（仅 §18.2 回滚）

`systemd/lumen-{api,worker,web,tgbot}.service` 默认 disable；只有 Docker 栈完全不可用时才用：

```bash
# Docker 全栈停掉
COMPOSE_PROJECT_NAME=lumen docker compose stop api worker web tgbot

# systemd 兜底拉起
sudo systemctl start lumen-api lumen-worker lumen-web

# 如果之前 disable 了，需要 enable
sudo systemctl enable --now lumen-api lumen-worker lumen-web
```

兜底前提（详见 `docs/docker-full-stack-cutover-plan.md` §18.2）：

- 宿主机 `.venv`、`.next`、`node_modules` 仍可用
- 旧代码能兼容当前 DB schema

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
| `nginx.conf.example` | **主反代**（`lumen.example.com`） | 反代 Next.js + API，含 SSE / body size / TLS / 限流。整段拷到 `sites-available/lumen.conf`。proxy_pass 仍指 `127.0.0.1:3000`，与 Docker Web 容器映射端口一致（详见 `docs/docker-full-stack-cutover-plan.md` §14）。 |
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

仅在 §18.2 回滚到 systemd 模式时需要在宿主机准备 Python 编译依赖（Debian/Ubuntu）：

```bash
sudo apt-get update
sudo apt-get install -y build-essential libpq-dev
```

Docker 全栈模式下，宿主机不需要 `uv / node / npm / build-essential / libpq-dev`。

## 发布流程（Docker 全栈）

1. rsync 代码到目标机（**必须排除 `apps/worker/var/` 整目录**，否则会覆盖用户数据）
2. **`sudo deploy/scripts/sync_env_version.sh`**（刷新 `LUMEN_VERSION` 为当次 commit hash）
3. `bash scripts/lumenctl.sh update-lumen`（默认 pull GHCR；按阶段执行 set_image_tag -> pull_images -> migrate_db -> switch -> restart_services）
4. 如果改了 `image-job/app.py`：`cp image-job/app.py /opt/image-job/ && systemctl restart image-job`
5. 验证：`grep '^LUMEN_VERSION=' /opt/lumen/shared/.env` 应为当次 commit 短 hash；`bash scripts/lumenctl.sh status` 全部 healthy

无 GHCR 访问或本地有改动时改用本地构建：

```bash
LUMEN_UPDATE_BUILD=1 bash scripts/lumenctl.sh update-lumen
```

## 环境变量约定

- 所有变量集中在 `${LUMEN_DEPLOY_ROOT}/shared/.env`（典型路径 `/opt/lumen/shared/.env`），release/.env 是 -> shared/.env 的 symlink，被 docker compose 自动读取
- 新机器从 `.env.example` 拷出来填，强随机密钥由 install.sh 自动生成
- 不要在 systemd unit 内 hardcode `Environment=KEY=value`；后台一键更新 runner 通过 `EnvironmentFile=-` 注入
- `image-job.service` 是个例外（它在 sidecar 自己的 unit 里写 `Environment=`），因为它不读 lumen .env

## 发布前测试

宿主机：

```bash
bash scripts/test.sh   # 按 worker / api / core 三个子进程分别跑（同进程合跑会有全局状态污染）
```

CI 拆 job 时按相同 3 段切分。镜像构建走 `.github/workflows/docker-release.yml`（push main / tag v* 触发，buildx + GHCR + matrix 4 镜像）。
