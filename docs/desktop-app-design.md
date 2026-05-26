# Lumen 桌面端（Windows / macOS）高可靠设计与实施文档

> Status: Draft v0.2 · Owner: TBD · Target: Lumen v1.2.x
> 关联文档：[`DESIGN.md`](./DESIGN.md)、[`docker-full-stack-cutover-plan.md`](./docker-full-stack-cutover-plan.md)、[`one-click-update-refactor-plan.md`](./one-click-update-refactor-plan.md)

## 1. 背景与目标

### 1.1 现状
Lumen 目前的发布形态是 Docker 全栈（见 `docker-compose.yml`）：

```
postgres (pgvector/pg16) + redis (7.4-alpine)
  + api      (FastAPI / uvicorn)
  + worker   (arq, 长任务 1800s)
  + web      (Next.js 16 / React 19)
  + tgbot    (可选 profile)
```

部署门槛对自托管用户偏高：要装 Docker、要会 compose、要懂反代、要管 systemd path-unit 触发更新。

### 1.2 目标用户
**完全离线 / 隐私敏感的单机用户**——所有业务数据（账户、对话、生成历史、记忆向量）落在本机磁盘，不出本地；远程 LLM / 图模型 API 调用由用户自己的 key 直接发起（BYOK），桌面端不做中转。

### 1.3 目标
1. 一键安装：`.dmg` (macOS arm64) / NSIS `.exe` (Windows x64)，无需 Docker、无需手工配置。
2. 本机桌面应用体验：保留现有 Web UI 的交互质量，但所有内部服务只绑定 `127.0.0.1`，由 Tauri 统一监管。
3. 数据 100% 在本地：`~/Library/Application Support/Lumen`（mac）/ `%LOCALAPPDATA%\Lumen`（win）。
4. 与 Docker 版本共享同一份业务代码（`apps/api`、`apps/worker`、`apps/web`、`packages/core`）。
5. 自动更新：基于 GitHub Releases，与 Docker 渠道（`v*` tag）共生。
6. 高鲁棒性优先：宁愿增加本地运行时 sidecar 和上百 MB 包体积，也不为“少进程/小体积”重写成熟路径或牺牲长任务可靠性。

### 1.4 非目标
- 多用户 / SaaS 多租户：桌面版退化为单用户单机。
- 计费、订阅、redemption：桌面版完全关闭（运行时关闭 + 构建/打包裁剪）。
- Telegram Bot：不打包进桌面端。
- Linux 桌面包：本期不做（自托管用户继续用 Docker）。
- 移动端：不在范围内。
- **Intel Mac（x86_64）**：v1.2 不出。Apple Silicon 自 2020 年起已 5 年，活跃自托管用户基本完成换机；Intel 用户继续用 Docker 版。
- **Windows ARM64**：Python C 扩展（sqlite-vec、pillow、argon2-cffi）轮子覆盖不全，v1.2 不出。
- **Windows MSI 安装包 / portable 单文件 exe**：只出 NSIS 安装器 `.exe`，一种格式。
- **登录 / 注册 / 用户管理**：桌面版定位是单人本机使用，桌面运行时不展示登录页、注册页、密码修改和 admin 用户管理入口。注意：这些模块不能从共享代码中物理删除，必须通过 `LUMEN_RUNTIME=desktop` 条件注册 / 条件构建 / 打包白名单裁剪，保证 Docker 版零回归。
- **BYOK 用户绑定层 / DB 加密 Key 仓库**：桌面版不使用原 `user_api_credential_id` 多用户分流、`BYOK_API_KEY_MASTER_SECRET`、`byok.mode_enabled`、邀请链接 / 邮箱白名单等 SaaS 多租户机制；但 Docker 版代码路径继续保留。
- **计费 / 钱包 / 兑换码 / 订阅**：桌面版不存在「为别人花的钱付款」语义，运行时关闭并隐藏；Docker 版保留。

> ⚠️ **必须保留**的核心能力：`packages/core/lumen_core/providers.py` + `apps/worker/app/provider_pool.py` 这套**供应商池**（多个 OpenAI 兼容上游端点：`base_url + api_key + priority + weight + purposes + image_jobs_endpoint + proxy + concurrency`，含轮询 / 容量门控 / 健康降级 / 能力路由）整体保留。被砍的只是"管理员配置 vs 用户绑定"这条多租户分流，不是供应商池本身。详见 §6.6。

---

## 2. 总体架构

```
┌──────────────────────────────────────────────────────────────┐
│  Lumen.app / Lumen.exe   (Tauri v2 / Rust supervisor)        │
│                                                              │
│   ┌──────────────────┐    IPC      ┌──────────────────────┐ │
│   │ WebView          │ ◄────────► │ Tauri Rust Core      │ │
│   │  (system browser)│             │  · 生命周期管理      │ │
│   │  http://127.0.0.1│             │  · sidecar 子进程    │ │
│   │  :<web_port>     │             │  · 自动更新          │ │
│   └────────┬─────────┘             │  · 文件系统授权      │ │
│            │ same-origin /api      │  · 托盘 / 菜单       │ │
│            ▼ via local Next        └─────────┬────────────┘ │
│   ┌──────────────────────────────────────────┴───────────┐  │
│   │  Local sidecars (all loopback-only)                  │  │
│   │  ├── lumen-web     (Next.js standalone / Node)       │  │
│   │  ├── lumen-api     (FastAPI / uvicorn)               │  │
│   │  ├── lumen-worker  (Python / arq)                    │  │
│   │  └── lumen-redis   (Garnet, Redis-compatible)        │  │
│   └──────────────────────┬───────────────────────────────┘  │
│                          ▼                                  │
│   ┌─────────────────────────────────────────────────────┐   │
│   │ 本地数据（用户目录）                                │   │
│   │  · lumen.sqlite (SQLite + sqlite-vec 扩展)          │   │
│   │  · storage/   生成图、上传素材                      │   │
│   │  · redis/     arq 队列、SSE stream、缓存、运行态     │   │
│   │  · logs/      supervisor/web/api/worker/redis logs   │   │
│   │  · OS keychain  ← 第三方 API key                    │   │
│   └─────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────┘
         │ HTTPS (用户自己的 API key)
         ▼
   OpenAI 兼容上游（官方 OpenAI / 中转站 / new-api / 自部署 sidecar / LM Studio / Ollama 等）
```

进程数：5（Tauri 主壳 + web + api + worker + Redis-compatible runtime）。这是高可靠方案的刻意选择：复用当前 Docker 版最成熟的 Next.js / arq / Redis 路径，减少重写面和跨平台未知数。所有内部通信都绑定 `127.0.0.1`，不监听任何外网接口。

---

## 3. 技术选型决策

| 维度 | 候选 | 选定 | 理由 |
|------|------|------|------|
| 桌面壳 | Electron / Tauri v2 / Wails | **Tauri v2 + Rust supervisor** | 体积小；原生 sidecar 管理、托盘、签名、更新、keychain、文件授权都适合放在 Rust 壳层 |
| 前端构建 | Next static export / Next standalone / 重写 Vite | **Next.js standalone sidecar** | 当前前端依赖 rewrites、headers、cookies、动态分享页和服务端 layout 逻辑；standalone 复用度最高，稳定性优先于省掉 Node |
| 后端打包 | PyInstaller / Nuitka / py2app+py2exe | **PyInstaller `--onedir`** | 生态成熟、跨平台、与 uv/hatchling 兼容；onedir 比 onefile 启动快 5×（避免 tmp 解压） |
| 数据库 | 嵌入式 Postgres / SQLite+vec | **SQLite + sqlite-vec（桌面最小 schema）** | 单机数据放 SQLite 最合理；但不做“所有历史 Alembic 原样双库化”，先落桌面功能全集所需表 |
| 队列 / 缓存 / SSE runtime | 自研 SQLite queue / SAQ / Redis / Garnet | **Garnet sidecar + 现有 arq/Redis 代码路径** | 当前系统大量依赖 Redis Stream、Pub/Sub、Lua、ZSET、缓存和 arq；保留 Redis-compatible 运行时比重写队列和事件总线更稳 |
| 自动更新 | Sparkle+Squirrel / Tauri updater | **Tauri updater** | 同一份配置覆盖 mac+win；签名校验内建；与 GitHub Releases 直连 |
| 签名 | mac: Developer ID / win: EV cert | **同左** | macOS 必须公证才能开盖；Windows EV 可去 SmartScreen 警告 |
| 配置存储 | dotfile / OS keyring | **OS keychain (keyring)** | API key 等敏感字段不落明文磁盘 |

> **语言选择结论**：macOS 和 Windows 版不需要重写成双端原生。最佳工程组合是 **Rust/Tauri 负责系统边界与监管、TypeScript/Next.js 负责 UI、Python 负责现有业务 sidecar、Garnet 提供 Redis-compatible 本地运行时、SQLite 负责单机持久业务数据**。性能瓶颈主要在上游 API、图片 I/O、队列调度和数据库并发，不在桌面壳语言。

> **SAQ 结论**：不要把 SAQ 当作 SQLite-backed 队列使用。当前高可靠方案优先保留 arq + Redis-compatible runtime；只有在 Garnet 兼容性验证失败时，才进入 P0-B 备选：自研 SQLite job table + runtime bus 抽象。

---

## 4. 仓库结构改造

### 4.1 唯一新增的顶层目录：`apps/desktop/`

所有桌面专属的代码 / 配置 / 脚本都收在这一个目录下，**不**再开 `apps/packaging/` 或 `packages/desktop-bridge/` 等并列目录：

```
apps/desktop/                     ← 唯一新增目录
  Cargo.toml                      Tauri Rust 项目根（不再嵌一层 src-tauri/）
  tauri.conf.json
  build.rs
  src/                            Rust 源码
    main.rs                       入口（含启动流程，§7.2）
    sidecar.rs                    spawn / 监管 web/api/worker/redis
    ipc.rs                        前端 invoke 命令
    secrets.rs                    keyring 读写
    single_instance.rs
    updater.rs
    tray.rs
    diagnostics.rs                诊断包、日志定位、健康检查聚合
  icons/                          mac/win 各规格图标
  packaging/                      构建与打包（取代旧设计的 apps/packaging/）
    pyinstaller/
      lumen-api.spec
      lumen-worker.spec
      hooks/                      处理 alembic / sqlite-vec / 隐式依赖
    runtime/
      garnet/                     Garnet 可执行文件与许可说明
      node/                       Node runtime（仅给 Next standalone）
    scripts/
      build-mac.sh
      build-win.ps1
      sign-mac.sh
      notarize-mac.sh
      sign-win.ps1
    migrate/
      pg-to-sqlite.py             docker → desktop 数据迁移
```

### 4.2 既有目录里的就地修改（不可避免，但不能破坏 Docker）

下列改动**必须就地放在原本归属的目录**，因为它们要么被 docker 版共用，要么受框架（Next.js 路由 / Alembic 路径 / SQLAlchemy engine / GitHub Actions）约束位置。原则是：**Docker 路径默认行为不变；desktop 只通过 runtime 分支、router 条件注册、打包白名单、前端导航条件渲染来裁剪。不得为了桌面版物理删除 Docker 版正在使用的模块。**

| 文件 / 目录 | 改动 | 不能集中的原因 |
|------------|------|--------------|
| `apps/api/app/main.py` | 新增 `include_desktop_routers(app)` / `include_docker_routers(app)` 分支；desktop 不注册 auth/signup/admin/billing/projects/tgbot/update 等入口，Docker 注册保持原样 | FastAPI router 必须挂在 api 进程下 |
| `apps/api/app/routes/` | 新增桌面设置 / bootstrap / provider key 测试路由；现有 SaaS 路由保留但 desktop 不注册 | 避免破坏 Docker 版 |
| `apps/api/app/db.py` | 改 runtime 分支（postgres asyncpg / sqlite aiosqlite） | docker 版也要用 |
| `apps/api/app/db_desktop_ext.py` | **新增**：sqlite-vec extension 加载钩子 | engine 钩子位置 |
| `apps/api/app/services/provider_probe.py` | **新增**：从 `auth.byok_verify` 抽出的探活逻辑 | docker 版可能也复用 |
| `apps/api/alembic/desktop/` | **新增**桌面最小 schema 迁移链；不要把所有历史 Postgres migration 原样双库化 | 桌面 schema 与 Docker schema 生命周期不同 |
| `apps/worker/app/` | 保留任务模块；desktop WorkerSettings 只注册 chat / image / memory / outbox 需要的任务，项目/计费任务不进入打包白名单 | worker 进程下 |
| `apps/web/next.config.desktop.ts` | **新增**：桌面版 standalone 配置，保留 rewrites/headers/cookies 能力 | Next CLI 要求 `next.config.*` 同级 |
| `apps/web/src/app/{login,signup,reset-password,invite,projects,library,poster-styles,me/wallet,admin}/` | 不物理删除；desktop 导航隐藏，直接访问返回桌面版 not-found/unsupported；打包时可用 tree-shaking 和路由条件减少暴露 | Next.js App Router 约束 |
| `apps/web/src/app/assets/` | **新增**：资产图库页面（§6.5） | Next.js 路由按文件系统识别 |
| `apps/web/src/app/onboarding/` | **新增**：首启动向导（§6.7） | 同上 |
| `apps/web/src/lib/desktop/` | **新增**：替代旧设计的 `packages/desktop-bridge`，封装 `window.__LUMEN__`、本地 token、Tauri invoke 桥 | 体量小（< 200 行），独立 workspace 包不划算，直接挂 web 内更利于打包 |
| `apps/tgbot/` | **不动也不打包**（桌面打包时不进 PyInstaller spec、不进 Tauri resources） | 桌面版不需要 |
| `packages/core/lumen_core/desktop_runtime.py` | **新增**：桌面运行时标识、local user 常量、provider key 引用规则、SQLite JSON 类型 helper | API/worker 共享 |
| `.github/workflows/desktop-release.yml` | **新增** CI | GitHub Actions 强制目录 |
| `.gitignore` | 加 `apps/desktop/target/`、`apps/desktop/dist/`、`build/`、`*.dmg`、`*.exe.unsigned` | git 排除产物 |
| `docs/desktop-app-design.md` | 本文档 | — |

### 4.3 一图概览

```
Image/  (现有 monorepo)
├── apps/
│   ├── api/         共享主体 + runtime 条件注册（见 §4.2）
│   ├── worker/      共享主体 + desktop WorkerSettings / 打包白名单
│   ├── web/         共享主体 + assets/onboarding/lib/desktop/ + next.config.desktop.ts
│   ├── tgbot/       桌面打包时整体排除
│   └── desktop/     【唯一新增】Tauri 壳 + packaging 子目录（含 PyInstaller + 签名 + 迁移）
├── packages/
│   └── core/        共享主体 + 新增 desktop runtime helpers
├── deploy/          仅 docker 版用，不动
├── docker-compose.yml
├── Dockerfile.python
└── .github/workflows/
    ├── docker-release.yml   现有
    └── desktop-release.yml  【新增】
```

核心原则：**共享代码默认服务 Docker，desktop 是一个受控 runtime profile**。桌面专属代码全部收在 `apps/desktop/`；共享目录里的改动必须满足两条线：

1. `LUMEN_RUNTIME=docker` 或未设置时，现有 Docker 版路由、构建、测试、发布行为不变。
2. `LUMEN_RUNTIME=desktop` 时，API / worker / web 通过条件注册和打包白名单形成桌面功能全集；不得依赖“删除文件”制造差异。

---

## 5. 数据层与运行时差异

### 5.1 数据库：Postgres → SQLite

#### 5.1.1 方言切换
- SQLAlchemy 已是 async，但不能只切 driver。桌面版必须建立**桌面最小 schema**，只覆盖 GA 功能全集：users(local-user)、conversations、messages、generations、completions、images、image_variants、shares（如保留分享）、outbox、account_memory、providers、settings、diagnostics。
- `apps/api/app/db.py`（创建 engine 的位置）按 `LUMEN_RUNTIME` 环境变量分支：
  - `docker` → asyncpg
  - `desktop` → aiosqlite + `PRAGMA journal_mode=WAL; foreign_keys=ON; busy_timeout=5000;`
- `psycopg2` 同步驱动只在 Alembic 用——desktop 版改用 `sqlite` 同步 dialect。
- 所有 SQLite 连接必须统一设置：`WAL`、`synchronous=NORMAL`、`foreign_keys=ON`、`busy_timeout=5000`、`temp_store=MEMORY`。启动时执行 `PRAGMA quick_check`；异常时进入恢复窗口，不直接继续写库。

#### 5.1.2 Desktop migration 策略
不要把 `apps/api/alembic/versions/*.py` 的全部 Postgres 历史迁移原样改成双方言。这会把大量项目、计费、BYOK、admin、Postgres-only 索引复杂度带进桌面版，风险高且收益低。

采用双迁移链：

- Docker：继续使用现有 `apps/api/alembic/versions/`，默认行为不变。
- Desktop：新增 `apps/api/alembic/desktop/versions/`，从一个清晰的 v1 desktop baseline 开始。
- 共享 ORM：把 Postgres-only 类型封装成 helper，例如 `JsonType()`、`StringListType()`、`VectorLiteralType()`；Docker 解析为 `JSONB` / `ARRAY` / pgvector 兼容文本，Desktop 解析为 `JSON` / JSON array / sqlite-vec 表达。
- 查询层：所有 `->>` / `@>` / `ARRAY` 查询要通过 helper 或按 dialect 分支，不能散落在 route 中。
- 数据迁移：Docker 导入桌面时走专用转换器，不走 Alembic 历史重放。

> **预审任务（P0-A）**：跑 `scripts/audit-desktop-portability.py`，扫描 ORM、route、worker 中的 `JSONB`、`ARRAY`、`postgresql.*`、`->>`、`@>`、`pg_advisory_xact_lock`、Redis 强依赖，输出“保留 / 替换 / desktop 不注册 / 待重构”矩阵。

#### 5.1.3 向量检索（pgvector → sqlite-vec）
`account_memory` 表（迁移 0018）现在用 pgvector。SQLite 端用 [sqlite-vec](https://github.com/asg017/sqlite-vec)：

```python
# apps/api/app/db_desktop_ext.py
from sqlalchemy import event
import sqlite_vec

@event.listens_for(engine.sync_engine, "connect")
def _load_vec(dbapi_conn, _):
    dbapi_conn.enable_load_extension(True)
    sqlite_vec.load(dbapi_conn)
    dbapi_conn.enable_load_extension(False)
```

API 表达层做一个 `vector_distance(col, query, k)` 函数，docker 走 `<->` 操作符，desktop 走 sqlite-vec 的 distance 函数。为了避免把整列 3072 维 JSON 扫描拖慢，desktop 端 account memory 必须有单独 vec 虚表和重建索引任务。

### 5.2 本地运行时：保留 arq + Redis-compatible sidecar

当前代码不只把 Redis 当队列，还用于：

- arq job queue、job timeout、retry、cron。
- SSE stream 回放、Pub/Sub、断线恢复。
- cancel 标记、手动 compact 状态、circuit breaker。
- provider stats、image concurrency / quota、reference cache。
- billing cache 在 desktop 关闭后可以不启用，但代码路径仍需可运行。

因此 desktop v1.2 高可靠方案不替换 arq，不引入 SAQ。Rust supervisor 启动一个 `lumen-redis` sidecar，默认使用 Garnet 的 Redis-compatible server，绑定 `127.0.0.1:<redis_port>`，持久化到 `data/redis/`，然后把 `REDIS_URL` 注入 API 和 worker。这样现有 arq WorkerSettings、Redis Stream、Pub/Sub、Lua 脚本、ZSET 缓存大部分保持原样。

#### 5.2.1 Garnet 兼容性 Gate
P0 必须验证以下命令族；任一不兼容都不能进入桌面主线：

- arq 基础：`SET`/`GET`/`DEL`/`EXPIRE`/`TTL`/`INCR`/`DECRBY`、sorted set、hash、list。
- Redis Stream：`XADD`、`XRANGE`、`XREAD`、`XTRIM`。
- Pub/Sub：`PUBLISH`、`SUBSCRIBE`。
- Lua / eval：现有 SSE idempotent XADD、billing cache、system lock 用到 `EVAL`。
- 持久化：mac/win 进程崩溃后队列和 stream 不丢；重启恢复时间可接受。

#### 5.2.2 备选方案
若 Garnet 无法满足兼容性，才启动 P0-B：

- 自研 `lumen_core.runtime_bus` + `lumen_core.jobq`。
- SQLite job table 支持 `lease_until`、`heartbeat_at`、`attempt`、`max_attempts`、`defer_until`、`cancel_requested`、`idempotency_key`、stuck job sweeper。
- SQLite event table 替代 Redis Stream，SSE 通过 DB cursor 回放。
- 本地内存 Pub/Sub 只作为在线加速，DB outbox 是事实来源。

这个备选会显著增加工期，不作为最优主线。

### 5.3 文件存储
- `LUMEN_DATA_ROOT` 桌面版固定为 OS 标准路径：
  - macOS: `~/Library/Application Support/Lumen/`
  - Windows: `%LOCALAPPDATA%\Lumen\`
- 目录布局：
  ```
  data/
    db/lumen.sqlite          主库
    db/lumen.sqlite-wal      WAL
    redis/                   Garnet 持久化目录（arq / SSE / 缓存 / runtime state）
    storage/                 生成图、上传素材（原 docker 版 storage 卷）
    logs/api.log
    logs/worker.log
    cache/                   下载缓存、缩略图
  ```
- 由 Tauri 在首次启动时创建，并把绝对路径以环境变量 `LUMEN_DATA_ROOT` 注入 sidecar。

### 5.4 密钥与配置（极简化）
桌面版定位单人单机，原 SaaS 模型里的多层密钥体系全部退化：

| 现有 SaaS 配置 | Docker 版用途 | 桌面版处理 |
|----------------|--------------|------------|
| `SESSION_SECRET` | 签发用户登录 cookie | desktop API 仍可生成进程内随机 session secret，兼容现有中间件；不持久化、不暴露给用户 |
| `BYOK_API_KEY_MASTER_SECRET` | 对 DB 中用户存储的供应商 key 二次加密 | desktop 不使用用户级 BYOK 表；供应商 key 写 OS keychain，由 OS 保护 |
| 数据库密码 / Redis 密码 | 服务间互认 | SQLite 无密码；本地 Redis-compatible sidecar 使用随机端口 + 随机 requirepass，仅注入 API/worker env |
| 用户密码（argon2 哈希） | 多用户登录 | **不再需要**——`users` 表退化为单行「local-user」记录，无密码字段 |

桌面端唯一持久化的密钥就是用户输入的第三方 API key（OpenAI、Anthropic、各图模型等）：

```rust
// apps/desktop/src/secrets.rs
use keyring::Entry;
fn set_provider_key(provider: &str, value: &str) -> Result<()> {
    Entry::new("com.lumen.desktop", &format!("provider:{provider}"))?
        .set_password(value)
}
```

非敏感配置（语言、主题、最近使用模型、UI 偏好、最后选用的供应商）：`data/settings.json`，纯文本。

#### 5.4.1 `users` 表的处理
不删表，否则要改一堆外键。改为：首启动 Alembic upgrade 跑完后，由 `app.scripts.bootstrap_desktop` 插入唯一一行 `id="local-user"`、`role="owner"`，所有业务表的 `user_id` 外键都指向它。这样：
- 业务代码不需要改外键关系。
- 后续如果需要可选「云同步」，仍能升级到多用户模型。
- API 中间件统一注入 `current_user = local-user`，绕过认证。

---

## 6. 前端改造（apps/web）

### 6.1 Next standalone 桌面构建
新增 `apps/web/next.config.desktop.ts`：

```ts
import base from "./next.config";
export default {
  ...base,
  output: "standalone",
  images: { unoptimized: true },
  // desktop web 由本地 Next sidecar 提供；rewrites / headers / cookies 保留。
};
```

构建脚本：
```bash
cd apps/web
NEXT_PUBLIC_LUMEN_RUNTIME=desktop \
  LUMEN_BACKEND_URL=http://127.0.0.1:${LUMEN_API_PORT} \
  next build --config next.config.desktop.ts
# 产物 .next/standalone + .next/static + public → 复制到 apps/desktop/dist/web/
```

Tauri 启动时先分配 `web_port` / `api_port`，把 `PORT` 和 `LUMEN_BACKEND_URL` 注入 `lumen-web` sidecar，再让 WebView 加载 `http://127.0.0.1:<web_port>`。

> 说明：static export 仍保留为 P0 spike 项，用于评估未来 v1.3 减包体积。但 v1.2 高可靠主线不用它，因为当前代码依赖 `next.config.ts` 的 rewrites/headers、`next/headers` cookies、动态 share 页面和服务端 layout 行为。

### 6.2 客户端 API base 注入
现状：前端通过相对路径 `/api/...` 走 Next.js rewrites 到后端。桌面高可靠主线继续保持这个模型：浏览器视角只有 `http://127.0.0.1:<web_port>` 一个 origin，Next sidecar 内部转发 `/api/*` 和 `/events` 到 API sidecar。

桌面版改造：
- `apps/web/src/lib/api/http.ts` 默认仍返回 `/api`，避免跨域、CSP 和 cookie 复杂度。
- `apps/web/src/lib/desktop/` 只负责 Tauri invoke、保存文件、打开数据目录、读取桌面运行时状态。
- 直接 `fetch()` 的散点调用必须收敛到 `apiFetch` / `eventUrl` / `imageBinaryUrl` 等 helper，便于统一附加 local token、错误分类和重试策略。
- SSE 仍走 `/events` 相对路径，由 Next rewrites 转发；避免 WebView 直接连 API 端口造成 CSP 双 origin。

### 6.3 Konva / canvas 兼容性
现有依赖 `konva`、`react-konva`（10.3 / 19.2）。Tauri v2 在 macOS 用 WKWebView、Windows 用 WebView2（Chromium 内核 Edge 同源）。两侧 Canvas / WebGL 都 OK，无需改动。

### 6.4 文件拖拽与本机能力
桌面化增强（放到 P5 增量做）：
- 拖文件入 web → 走 Tauri `onFileDrop` 拿到绝对路径，避免 multipart upload。
- 生成结果一键保存到任意目录 → Tauri `dialog::save`。
- 系统通知（生成完成）→ Tauri `notification`。

通过 `apps/web/src/lib/desktop/` 封装，前端用 `if (isDesktop)` 判断。

### 6.5 桌面版裁剪的 UI 面板
设置 `NEXT_PUBLIC_LUMEN_RUNTIME=desktop` 时条件隐藏 / 条件拦截。再次强调：这些页面和模块不能从共享代码物理删除，否则 Docker 版会回归。

**整个分组移除**：
- 「访问与用户」分组（含「白名单」「用户」「邀请链接」三项）
- 「账号、邀请、费用与自带 Key」分组（含「API 站接入」「计费」两项）

**对应底层路由 / 模块的桌面处理清单**：

| UI 入口 | 后端路由 / 模块 | 桌面版处理 |
|---------|----------------|----------|
| `/login` `/signup` `/reset-password` `/invite/[token]` | 前端：对应 `apps/web/src/app/{login,signup,reset-password,invite}/` 目录；后端 `apps/api/app/routes/auth.py` 中 `signup` / `login` / `reset` / `byok-signup` / `invite-redeem` 等端点 | desktop 下导航不出现；直接访问跳 `/` 或桌面 unsupported；API 不注册公开注册 / invite endpoint |
| admin → 访问与用户 → 白名单 | `apps/api/app/routes/admin_allowlist.py` | desktop 不注册 / 不展示 |
| admin → 访问与用户 → 用户 | `apps/api/app/routes/admin_users.py` | desktop 不注册 / 不展示 |
| admin → 访问与用户 → 邀请链接 | `apps/api/app/routes/admin_invites.py` | desktop 不注册 / 不展示 |
| admin → 账号 → API 站接入 | `apps/api/app/routes/admin_suppliers.py`（BYOK 模板 CRUD） + `auth/api-suppliers` + `/auth/api-key/verify` + `/auth/signup/byok` | desktop 不注册 BYOK 注册侧；供应商池走 §6.6 的本机设置 |
| admin → 计费（含计费规则、钱包、兑换码） | `apps/api/app/routes/admin_billing*.py`、`me/wallet`、`apps/worker/app/billing*.py` | desktop 不注册 / 不展示；worker 打包排除计费定时任务 |
| 用户头像下拉的「登出」「修改密码」「我的订阅」「钱包」「我的 API Key」 | — | 全部移除 |
| Telegram bot 设置 | `apps/api/app/routes/admin_tgbot.py` | 隐藏（tgbot 本身桌面版不打包） |
| 「Docker 一键更新」按钮 | `apps/api/app/routes/admin_update.py` | 改成「检查更新」走 Tauri updater；原 update 路由保留但前端入口换 |
| **「项目」一级导航**（服饰模特展示 / 海报制作 / 分镜制作）+ 模特库 + 样式库 | 前端：`apps/web/src/app/projects/*`、`apps/web/src/app/library/*`、`components/ui/projects/*`、`components/ui/library/*`；后端：`apps/api/app/routes/workflows.py`、`poster_styles.py`、`_apparel_library.py`、`_poster_library.py`、`apps/api/app/scripts/sync_apparel_library.py` 等；worker：项目任务；assets：`assets/apparel-model-presets/`、`assets/poster-styles/`；Alembic 表：`workflows`、`workflow_stages`、`apparel_library_*`、`poster_styles_*` | desktop v1.2 不注册 / 不展示 / 不打包预设 assets；Docker 版完整保留 |

桌面版剩下的核心业务面：**对话（chat）+ 单次生图 + 资产图库（我的图）+ 账户记忆（account memory，对话上下文向量记忆，与素材库无关）**，加上设置面板。

> 关键澄清：**「模特库 / 样式库」** ≠ **「资产图库」**。前者是给「项目」工作流（apparel showcase / poster workflow）用的**模板预设库**（admin-curated 模特照、风格参考），随项目功能一起删；后者是用户自己**生成或上传的图的浏览界面**，对应 `/images/*` 与 `/generations/feed`，是核心保留功能（详见 §6.5 保留入口列表）。

**保留并升级**的入口（这是桌面版功能全集）：
- **对话（chat）** — 主界面默认就是它
- **单次生图** — 走对话里调用，或独立入口
- **资产图库 `/assets`**（用户自己生成 / 上传的图）：
  - 后端复用 `apps/api/app/routes/images.py`（`/images/upload`、`/images/{id}/binary`、`/images/{id}/variants/{kind}`、`DELETE /images/{id}`）+ `apps/api/app/routes/generations.py::list_generation_feed`（feed 列表 + 分页 + 按对话筛选）
  - 前端：原 `/library` 路由仍属于 Docker 版模特库；桌面版新增 **`/assets`** 作为顶级导航；瀑布流 / 网格视图、按时间倒序、按来源会话筛选、按是否上传 / 生成筛选；点击进大图、保存到磁盘（Tauri `dialog::save`）、删除、复制 prompt 反查回原对话
  - 与 storage 目录的关系：图二进制走 `data/storage/`，缩略图走 `data/cache/`
- **账户记忆**（`account_memory` 表 + sqlite-vec 向量记忆，与素材库无关）
- **设置 → 供应商池**（关键，详见 §6.6）：原 `/admin/providers` 整套留下，只是从 admin 移到「设置 → 供应商池」，去掉权限校验
- **设置 → 健康面板**（sidecar 状态、磁盘占用、最近错误、provider 探活结果）
- **设置 → 存储管理**（数据目录定位、备份、清理缓存）
- **设置 → 检查更新**
- **设置 → 关于**（版本号、许可证、诊断包导出）

> 关键区分：**「供应商池」（保留）** vs **「API 站接入」（删除）** 是现有代码里两条独立的链路：前者是 worker 实际路由用的上游端点池（`ProviderDefinition` + `provider_pool.py`），后者是给 BYOK 用户用自带 key 注册时的供应商模板目录。桌面版只有一个本机用户、不需要注册流，所以"模板目录"整套没意义；而"端点池"是核心路由逻辑，必须留。

### 6.6 供应商池在桌面版的形态

桌面版**完整继承** `packages/core/lumen_core/providers.py` 里的 `ProviderDefinition` 数据结构与 `apps/worker/app/provider_pool.py` 的所有路由逻辑：优先级、权重、purposes (`chat` / `image` / `embedding`)、能力门控、image_jobs endpoint 锁定、proxy、并发上限、健康学习。这些是 Lumen 区别于"裸调 OpenAI"的核心价值。允许对 provider config 加一层 desktop source adapter，但不重写选择算法。

#### 6.6.1 改的是「在哪存」和「谁来配」

| 维度 | Docker / SaaS 版 | 桌面版 |
|------|------------------|--------|
| 谁能配置供应商 | admin 在 `/admin/providers` 配置全局池；BYOK 用户在 `/me/api-credentials` 绑定自己的 key | **本机用户**直接在「设置 → 供应商池」配置所有条目，无角色分裂 |
| Provider 元数据存哪 | `system_settings.providers` JSON | SQLite 表 `providers`（一行一条，字段与 `ProviderDefinition` 对齐）|
| `api_key` 存哪 | `system_settings.providers` JSON 明文 / `user_api_credentials` 用 master secret 加密 | **OS keychain**，service=`com.lumen.desktop`、account=`provider:<provider_name>`；DB 里只存 provider 名 / base_url / 路由参数，不存 key |
| 缺 key 时的回退 | 用户 key 失效→可选回退到 admin pool | **不回退**，直接抛错让用户去补 key |
| 探活 / 验证模型 | BYOK 注册时强制做一次 | 用户在配置页点「测试」时做一次，不强制；默认验证模型必须来自当前 runtime setting 或保守默认值，不能在文档里硬编码不存在的模型名 |

#### 6.6.2 启动时如何把 keychain 注入 provider_pool

```
Tauri supervisor 启动
 ├── 从 SQLite providers 表读 N 条不含 key 的 provider metadata
 ├── 从 OS keychain 读 provider:<name>，缺 key 则标记 enabled=false
 ├── 生成本次会话临时 providers JSON，写到 0600 临时文件或 env
 ├── 注入 API / worker：LUMEN_DESKTOP_PROVIDER_FILE=/path/providers.runtime.json
 └── worker provider_pool 的 desktop source adapter 优先读该文件；Docker 继续读 system_settings.providers / env
```

这样 `provider_pool.py` 的选择算法不用改，但配置来源需要新增 desktop adapter。不要让 Python 直接依赖 Rust 内存态，也不要把 key 写回 SQLite。

#### 6.6.3 首启动的默认条目

数据库 onboarding 时插入一条占位 provider：
```
name: "OpenAI 官方"
base_url: "https://api.openai.com/v1"
purposes: ("chat", "image")
priority: 100
enabled: False   ← 直到用户填了 key 才置 True
```

用户也可以一行不留，自己从零添加（典型场景：用自己买的 OpenAI 中转站 / new-api / 自部署 sidecar）。

#### 6.6.4 与既有代码的兼容
- `apps/api/app/routes/providers.py` 保持 Docker `/admin/providers` 形状；desktop 新增 `/settings/providers` facade，复用序列化、校验、探活 helper。
- `apps/api/app/routes/me_api_credentials.py` Docker 保留；desktop 不注册。
- worker 端新增 provider config source adapter，但 `ProviderPool.select()`、健康学习、路由算法不改。

### 6.7 首启动 Onboarding 向导

桌面版第一次打开（检测：`data/.bootstrap-done` 标记文件不存在）必须强制走完一个 4 步引导，否则不让进入主界面。向导是 React 内嵌全屏 modal，由前端检测 `GET /settings/bootstrap-status` 触发。

#### 6.7.1 步骤设计

**Step 1 · 欢迎与数据目录**
- 显示当前默认数据目录（`~/Library/Application Support/Lumen` / `%LOCALAPPDATA%\Lumen`），剩余磁盘空间。
- 允许「更改…」（Tauri `dialog::open`，选目录），写到 `data/.lumen-data-root` 指针文件。建议留默认，提示「移动到外接盘 = 离线时 app 无法启动」。
- 不勾选 / 默认下一步。

**Step 2 · 起步方式**
- ① **全新使用**（默认）
- ② **从 Docker 备份导入**（让用户选 `lumen-export.dump` + `lumen-storage.tar.gz`，触发 §10.2 流程；导入完跳到 Step 4）
- ③ **从其它桌面端备份恢复**（选 `.lumen-backup.zip`）

**Step 3 · 配置第一个供应商**（最关键的一步，决定 app 能不能用）
- 表单字段：
  - `名称`（默认填「OpenAI 官方」，可改）
  - `Base URL`（默认 `https://api.openai.com/v1`，可改成中转站 / 自建 sidecar）
  - `API Key`（密码型输入框，提交后写 OS keychain，**永远不回显**）
  - `用途`：勾选 chat / image / embedding（默认 chat + image）
  - `代理（可选）`：折叠面板，需要时填 `http://127.0.0.1:7890` 之类
  - `验证模型`（默认读取 `BYOK_VALIDATION_MODEL` / provider probe setting；没有配置时使用当前仓库支持的保守默认）
- 「测试连接」按钮：调 `POST /settings/providers/test`，后端临时建一个 `ProviderDefinition` 跑一次随机算术验证。
  - 先把现有 BYOK challenge 逻辑（生成随机算式、调上游 `/v1/responses`、解析数字答案、超时控制）抽到 `apps/api/app/services/provider_probe.py`，由桌面版 `/settings/providers/test` 调用；docker 版的现存 BYOK 入口也复用同一 helper。
  - 通过 → 显示「✓ 连接成功，模型可用」，「完成」按钮亮起
  - 失败 → 显示 httpx 错误码 + 友好提示（4xx → key 错；5xx / 超时 → 网络或 base_url 错；403 → 可能需要代理）
- 必须测试通过才能进入 Step 4。允许「跳过测试直接保存」（隐藏二级按钮），但会在主界面顶部挂一个黄色提示条。

**Step 4 · 偏好与隐私**
- 主题（跟随系统 / 浅色 / 深色）
- 语言（中文 / English；从 `navigator.language` 推断默认值）
- **崩溃报告**：opt-in，默认关闭。开启会启用 Sentry（`before_send` 过滤所有 user data，只留栈 + 版本 + 平台）。
- **自动检查更新**：opt-in，默认开启（每日检查一次 GitHub Releases）。
- 「完成」→ 前端 POST `/settings/bootstrap-complete`，后端写 `data/.bootstrap-done`，前端 `router.replace("/")` 进入主界面。

#### 6.7.2 收集的关键信息汇总

| 字段 | 落到哪 | 是否必填 |
|------|--------|----------|
| 数据目录路径 | `data/.lumen-data-root`（Tauri 侧维护） | 必（有默认值） |
| 起步方式选择 | 一次性流程变量，不持久化 | 必 |
| 供应商 name / base_url / purposes / 代理 / 验证模型 | SQLite `providers` 表第一行 | 必 |
| 供应商 api_key | OS keychain `provider:<name>` | 必（允许跳过测试，但缺 key app 无法生图） |
| 主题 / 语言 | `data/settings.json` | 有默认值 |
| 崩溃报告 opt-in | `data/settings.json` | 默认 false |
| 自动检查更新 | `data/settings.json` | 默认 true |

#### 6.7.3 边界与状态恢复
- 引导中途崩溃：`data/.bootstrap-done` 没写就重启时再次进入引导，已填的字段从内存丢失（不做 draft 持久化，避免半成品状态污染）。
- 用户在「设置」里点「重新引导」：清空 `data/.bootstrap-done`，下次启动重走（不会清数据）。这条入口放在「关于」页底部，方便重新选数据目录或重导。
- 完全卸载 → 默认保留 data root；「彻底卸载」选项会同时删数据目录 + keychain 项 + `.bootstrap-done`，下次安装等同首装。

---

## 7. 桌面壳（Tauri v2）

### 7.1 Cargo 依赖
```toml
# apps/desktop/Cargo.toml
[dependencies]
tauri = { version = "2", features = ["tray-icon"] }
tauri-plugin-shell = "2"
tauri-plugin-fs = "2"
tauri-plugin-dialog = "2"
tauri-plugin-notification = "2"
tauri-plugin-updater = "2"
tauri-plugin-single-instance = "2"
tauri-plugin-os = "2"
tauri-plugin-store = "2"      # 偏好持久化
keyring = "latest-compatible"  # 固定到当前验证过的主版本；P0 查 crates.io 后落锁
```

### 7.2 启动流程

```
main()
 ├── single_instance 检查（防止多开）
 ├── 解析 / 创建 LUMEN_DATA_ROOT（按 §6.7 Step 1 用户选择）
 ├── 生成本次会话的本地启动 token（32 字节随机，仅存内存，见 §11）
 ├── 分配空闲端口 (web_port, api_port, redis_port, worker_metrics_port)
 ├── 写 runtime env 文件：DATA_ROOT、PORTS、REDIS_URL、LUMEN_LOCAL_TOKEN、SESSION_SECRET
 ├── 从 keychain 生成临时 provider runtime file（0600 权限）
 ├── spawn sidecar: lumen-redis   （Garnet，requirepass，持久化 data/redis）
 │    └── 等待 PING + 命令兼容 smoke，超时 10s
 ├── spawn sidecar: lumen-api     （env 注入 DATA_ROOT、PORT、REDIS_URL、LOCAL_TOKEN）
 │    └── 等待 GET /healthz + /system/desktop-ready 返回 200，超时 30s
 ├── spawn sidecar: lumen-worker
 │    └── 等待 arq health / worker heartbeat，超时 60s
 ├── spawn sidecar: lumen-web     （Next standalone，env 注入 PORT、LUMEN_BACKEND_URL）
 │    └── 等待 GET /healthz，超时 20s
 └── 创建主窗口加载 http://127.0.0.1:<web_port>
```

注意：桌面版不持久化 `SESSION_SECRET`、`BYOK_API_KEY_MASTER_SECRET`。为了兼容现有 cookie/CSRF 中间件，可以每次启动生成随机 `SESSION_SECRET`；本地鉴权另由 `LUMEN_LOCAL_TOKEN` 强制覆盖。唯一持久化的密钥是用户输入的第三方 API key，由 §6.7 Onboarding 写入 keychain。

任一 sidecar 启动失败：弹出错误窗口，日志路径 + 「打开诊断目录」按钮。

### 7.3 端口分配
不固定 `3000` / `8000` / `6379`——避免与用户已有 Docker 版冲突。Rust 端预 bind `127.0.0.1:0` 拿到端口后传给 sidecar。为了避免 TOCTOU，优先让 sidecar 自己 bind `0` 后把实际端口写入 readiness pipe；做不到时才采用预分配端口并在失败后自动重试。

### 7.4 进程生命周期
- 主窗口关闭 → 不退出（macOS 习惯）；点 Dock 重开窗口。
- Tray menu 「退出 Lumen」 / Cmd+Q：
  - 先查 worker 是否有正在跑的长任务（查 arq / Redis runtime 的 in-flight 状态 + DB 任务状态交叉确认）
  - **无任务** → 给 worker 30s SIGTERM grace → 仍未退则 SIGKILL → 给 api 10s SIGTERM grace → 退 shell，整体 5–10s 内完成
  - **有任务** → 弹原生对话框「有 N 个生图任务进行中，预计还需 X 分钟。等待完成 / 终止并退出 / 取消」让用户决定。**不再使用 docker-compose 的 1830s 静默 grace**，桌面端用户不会接受按"退出"后等半小时
- 强制退出：对话框选「终止并退出」 → API 先把 running 任务标记为 interrupted，worker SIGTERM 5s 后 SIGKILL；下次启动提供“重试中断任务”
- 崩溃恢复：worker / web 异常退出自动重启（指数退避，3 次内静默恢复，超过通知用户）；api 或 redis 崩溃则暂停新任务、保存诊断包入口、尝试一次全栈有序重启

### 7.5 单实例 & 深链
- `lumen://` URL scheme 注册：用于将来分享深链、未来的 OAuth 回调。
- 单实例：第二次启动把命令行参数转发给已运行的实例。

### 7.6 系统托盘
- 状态指示：●（就绪）/ ◐（任务执行中）/ ✕（错误）
- 菜单：显示主窗口、最近任务、暂停 worker、退出。

---

## 8. 打包与构建管线

### 8.1 Python sidecar 打包
`apps/desktop/packaging/pyinstaller/lumen-api.spec`：

```python
# 关键点
hiddenimports = [
    "aiosqlite",
    "sqlite_vec",
    "alembic.runtime.migration",
    # 业务任务模块要显式列出 — PyInstaller 静态扫不到 dynamic import
    "app.tasks.generation",
    "app.tasks.completion",
    "app.tasks.memory_extraction",
    "app.tasks.outbox",
    "app.tasks.auto_title",
    "app.tasks.context_summary",
    ...
]
binaries = [
    # sqlite-vec 是 .dylib / .dll，必须显式带
    (sqlite_vec_lib_path(), "."),
]
datas = [
    ("apps/api/alembic/desktop", "alembic/desktop"),
    ("apps/api/alembic.ini", "."),
]
```

构建：
```bash
uv run pyinstaller --clean --noconfirm \
  --distpath build/dist apps/desktop/packaging/pyinstaller/lumen-api.spec
```

产物 `build/dist/lumen-api/lumen-api(.exe)` + `_internal/` 共享库目录（onedir 模式）。

Worker 同理一份 spec。

### 8.1.1 Web / Redis runtime 打包
- Web：使用 Next standalone 产物 + 固定版本 Node runtime，作为 Tauri resource 直接由 Rust supervisor 启动。不要依赖用户机器安装 Node，也不要再额外生成占位 `lumen-web` externalBin。
- `lumen-redis`：打包 Garnet 可执行文件和 license。macOS / Windows 分别使用对应平台二进制；CI 做 Redis 命令兼容 smoke。
- 所有 runtime binary 必须被 Tauri 签名链覆盖。Windows 下 `.exe` / `.dll` 全部签名；macOS 下 bundle 内 Mach-O 全部 codesign。

### 8.2 Tauri resource 绑定
`tauri.conf.json`：
```json
{
  "bundle": {
    "resources": [
      "resources/alembic/desktop/**/*",
      "resources/runtime/**/*",
      "resources/web/**/*",
      "resources/licenses/**/*"
    ]
  }
}
```

PyInstaller、Garnet、Node 和 Next standalone 产物统一复制进 `resources/runtime/` 与 `resources/web/`。Rust supervisor 从 bundle resource 路径解析并启动这些内置组件，避免额外的占位 sidecar 二进制和 target-triple 重命名步骤。

### 8.3 CI 矩阵

GitHub Actions 新增 `.github/workflows/desktop-release.yml`，与现有 `Docker Release` 在同一个 `v*` tag 上并行触发：

| Job | Runner | 产物 |
|-----|--------|------|
| `unit-docker-regression` | `ubuntu-latest` | Docker 默认路径测试，防止 desktop 条件分支破坏现有产品 |
| `desktop-smoke-mac-arm64` | `macos-14` | 真实代码 sidecar smoke：Next standalone + API + worker + Garnet + SQLite |
| `desktop-smoke-win-x64` | `windows-2022` | 同上，重点验证 WebView2 / Garnet / keychain / 文件锁 |
| `build-mac-arm64`   | `macos-14` (M1) | `Lumen-1.x.y-arm64.dmg` |
| `build-win-x64`     | `windows-2022`  | `Lumen-1.x.y-x64-setup.exe`（Tauri NSIS bundler） |
| `sign-notarize-mac` | `macos-14`      | 签名 + 公证后的 dmg |
| `sign-win`          | `windows-2022`  | `signtool` 用 OV/EV 证书签 `.exe` 主程序与安装器 |
| `install-e2e-mac-arm64` | `macos-14` | 安装签名 dmg，跑 onboarding / provider fake / 生成假任务 / 重启恢复 |
| `install-e2e-win-x64` | `windows-2022` | 安装 NSIS exe，跑同样 E2E，检查卸载保留/清理 |
| `publish`           | `ubuntu-latest` | 上传到 GitHub Release，更新 `latest.json` (Tauri updater manifest) |

构建时长预估：mac ~18-25min / win ~15-22min（含 PyInstaller、Next.js build、Garnet/Node runtime 打包、Tauri bundle、安装包 E2E）。这是高可靠方案接受的成本。

**Tauri 配置**：
```json
// apps/desktop/tauri.conf.json (节选)
{
  "bundle": {
    "targets": ["dmg", "nsis"],
    "macOS": { "minimumSystemVersion": "12.0" },
    "windows": {
      "nsis": {
        "installMode": "currentUser",      // 不要求管理员
        "languages": ["SimpChinese", "English"],
        "displayLanguageSelector": false,
        "installerIcon": "icons/icon.ico"
      }
    }
  }
}
```

NSIS 默认产物：`Lumen_1.x.y_x64-setup.exe`，自带卸载入口、开始菜单项、桌面快捷方式，不需要管理员权限（per-user 安装）。如果用户想要 portable 版需要等到 v1.3，那时再决定要不要付出额外 ~100MB 体积捆绑 WebView2。

### 8.4 签名与公证
- **macOS（arm64 only）**：
  - Developer ID Application 证书放 GitHub Secrets（p12 + 密码）
  - `codesign --deep --options runtime --entitlements lumen.entitlements`，签 `.app` bundle 内全部 Mach-O（含 PyInstaller 产出的 `_internal/*.dylib`）
  - `xcrun notarytool submit --wait` + `xcrun stapler staple`
  - Entitlements 至少需要：`com.apple.security.network.client`、`com.apple.security.files.user-selected.read-write`
- **Windows（x64 NSIS）**：
  - 首版用 OV 证书；6 个月内升 EV 证书去 SmartScreen 警告
  - `signtool sign /tr http://timestamp.digicert.com /td sha256 /fd sha256 /a`
  - 签三类目标：`Lumen.exe` 主程序、`_internal/*.dll`（PyInstaller 产出）、`Lumen_*_x64-setup.exe`（NSIS 安装器外壳）
  - Tauri 的 `tauri-plugin-updater` 期望 `.exe` 与 `.nsis.zip` 都有有效签名才会接受热更新

### 8.5 包大小预估
单架构、无 universal 合并，体积比双架构方案减半：

| 组件 | mac arm64 (.dmg) | win x64 (-setup.exe) |
|------|------------------|---------------------|
| Tauri shell + WebView 调用层 | ~12 MB | ~10 MB |
| Next.js standalone + static | ~25 MB | ~25 MB |
| Node runtime | ~45 MB | ~55 MB |
| Python 3.12 runtime + 依赖（PyInstaller onedir） | ~85 MB | ~95 MB |
| Garnet / Redis-compatible runtime | ~50 MB | ~60 MB |
| sqlite-vec + sqlite 扩展二进制 | ~1.5 MB | ~1.5 MB |
| NSIS 安装器外壳 / DMG 元数据 | — | ~3 MB |
| **合计** | **~220 MB** | **~250 MB** |

可接受。v1.2 的目标是“装上就稳”，不是最小体积。后续 v1.3 可在 static export、自研轻量 queue、压缩 Python 依赖上做减重，但不能阻塞首版可靠性。

---

## 9. 自动更新

### 9.1 渠道
- **stable**：跟随 GitHub Release `v*` tag（与 Docker 版同一个发布事件）
- **beta**：可选预发布（`v*-beta.N` tag），用户在设置里勾选才会收到

### 9.2 manifest 形式
Tauri updater 期望 `latest.json`：
```json
{
  "version": "1.2.0",
  "notes": "see https://github.com/cyeinfpro/lumen/releases/tag/v1.2.0",
  "pub_date": "2026-06-01T12:00:00Z",
  "platforms": {
    "darwin-aarch64": { "signature": "...", "url": "https://github.com/.../Lumen_1.2.0_aarch64.app.tar.gz" },
    "windows-x86_64": { "signature": "...", "url": "https://github.com/.../Lumen_1.2.0_x64-setup.nsis.zip" }
  }
}
```

`publish` job 在签名后用 `tauri signer sign` 生成 .sig，更新 manifest，提交到一个 `gh-pages` 分支或直接挂在 Release assets。

### 9.3 升级时的数据迁移
- 启动时只跑 desktop migration chain，且必须先获取 migration lock，防止 API/worker 双进程并发迁移。
- 任何 schema migration 前自动执行 `VACUUM INTO lumen.sqlite.bak.<from>-<to>`；备份成功后才升级。
- migration 失败：停止 API/worker，保留原库和失败日志，显示恢复窗口；不得半启动进入主界面。
- app binary 回滚不能自动回滚 schema；只有当 migration manifest 标记为 reversible 且备份存在时，才允许用户手动回滚数据。

---

## 10. 现有 Docker 用户的数据迁移

### 10.1 导出工具（docker 端）
`scripts/desktop-export.sh`：
```bash
docker exec lumen-pg pg_dump -Fc -U "$DB_USER" "$DB_NAME" > lumen-export.dump
tar -C /opt/lumendata/storage -czf lumen-storage.tar.gz .
# 把两个文件交给用户
```

### 10.2 导入向导（desktop 端）
首启动检测到空数据库 → 显示 onboarding：
- ① 全新使用
- ② 从 Docker 版迁移（选 `.dump` + `.tar.gz`）
- ③ 从其它桌面端备份恢复（选 `.lumen-backup` zip）

选 ② 时，桌面端调用内置 `pgloader` 风格的转换脚本：
`apps/desktop/packaging/migrate/pg-to-sqlite.py`，按表逐条迁移，处理类型差异（JSONB→JSON、向量列重建索引）。

**多用户 dump → 单用户的处理**：Docker 版的备份里可能有多个 `users` 行，迁移工具会让用户在 onboarding 里选择「以哪个用户的身份导入」，被选中的 user 改写成 `id="local-user"`，其它用户与其关联的对话、生成历史、模特库直接丢弃。导入完成后会显示丢弃的数据条数让用户确认。

**供应商池迁移**：Docker 版的供应商池在 `system_settings.providers` JSON 里（含明文 `api_key`）。迁移流程：
1. 读 `system_settings.providers`，按 `ProviderDefinition` 字段写入桌面端 `providers` 表（不含 key）。
2. 把每条 provider 的 `api_key` 写入 OS keychain（`account=provider:<name>`）。
3. 完成后从 `system_settings` 里删掉 `providers` 字段（DB 不留 key）。

**被丢弃的数据**（与已移除的 SaaS 功能一一对应，不导入桌面版）：
- `users`（除选中那一行）、`user_api_credentials`、`api_supplier_templates`（BYOK 模板）
- `invites`、`email_allowlist`
- `billing_*`、`wallets`、`redemption_codes`
- `tgbot_*`

> **风险**：postgres → sqlite 的转换不可能 100% 无损（如 `pg_trgm` 索引、特定排序规则）。文档要明确告诉用户这是单程迁移，建议保留 Docker 备份。

---

## 11. 安全模型

| 风险 | 缓解 |
|------|------|
| sidecar 端口被本机其它进程探测 / 跨站脚本调本地 API | 仅 bind `127.0.0.1`；每次随机端口；**本地启动 token 鉴权**——Tauri 进程启动时随机生成 32 字节 token，注入 web/api/worker；Next rewrites 给 API 附加 `X-Lumen-Local-Token`；API 拒绝无 token 请求 |
| API key 落盘 | OS keyring（macOS Keychain / Windows Credential Manager），由 OS 决定加密；DB 里完全不存 |
| sqlite 文件被复制走 | 默认不加密（性能）；提供「锁定数据库」开关，启用后用 SQLCipher（追加 P6） |
| CSP | WebView 只加载本地 Next origin；`connect-src 'self'` 优先。若调试阶段需要直连 API，再临时允许 `http://127.0.0.1:*`。所有上游 API 调用都由 sidecar 发起，webview 不直接连外部供应商 |
| 本地 Redis 被其它进程访问 | 随机端口 + 随机密码 + 127.0.0.1 only；不写入用户可读配置；退出时清理 runtime env 文件 |
| 自动更新中间人 | Tauri updater 强制校验 ed25519 签名；私钥放 1Password+GH secrets |
| 前端漏洞 → 本地 RCE | Tauri 默认禁用所有 plugin，逐项 allowlist；不开启 `tauri.allowlist.shell.open(*)`；只允许白名单 IPC 命令 |
| 单用户模型下"无登录"被误读为"无鉴权" | 文档与启动 splash 明确：本机进程边界即安全边界；其它本机用户能读 keychain 项就能拿到 key——多用户共享电脑场景**不在本期威胁模型内**，建议这类用户继续用 Docker 版 |

---

## 12. 可观测性

- 本地日志：rotating file，`data/logs/`，默认保留 14 天。每个 sidecar 单独日志：`supervisor.log`、`web.log`、`api.log`、`worker.log`、`redis.log`。
- 用户主动反馈：「设置 → 帮助 → 生成诊断包」打包 logs + sqlite schema dump（脱敏） + Redis INFO（脱敏） + 系统信息 + sidecar 版本 → `.zip`。
- supervisor 心跳：每 5s 记录 sidecar pid、端口、RSS、ready 状态、最近一次重启原因。
- 崩溃上报（可选，默认关闭，opt-in）：Sentry SDK 已经在用，桌面版默认 `before_send` 把所有 user data 过滤掉，只发栈、版本、平台。
- Prometheus 端口在桌面端关掉（普通用户不会用）。

---

## 13. 实施计划与里程碑

> 时间估算单位：人/周（一个全职开发）。可并行的环节用 `‖` 标注。

### Phase 0 — 真实代码可行性 Spike（1.5 周）
- [ ] Tauri v2 supervisor 同时拉起真实 Next standalone、真实 FastAPI、真实 worker、Garnet sidecar。
- [ ] macOS arm64 + Windows x64 验证 Garnet 命令兼容矩阵：arq、Stream、Pub/Sub、EVAL、持久化、崩溃恢复。
- [ ] PyInstaller --onedir 打包真实 API/worker，验证 sqlite-vec、Pillow、argon2、httpx、alembic desktop chain。
- [ ] 验证 keychain 库在 Windows Credential Manager / macOS Keychain 写读一致性（含中文 provider 名、重命名、删除）。
- [ ] static export 只作为减重实验，记录缺口，不进入 v1.2 主线。
- **Gate**：两个平台都能用真实 sidecar 启动 → fake provider 跑通 chat/completion → arq job 被 worker 消费 → SSE 回放正常 → 重启后队列/DB 不丢。

### Phase 1 — Runtime 边界与 Docker 零回归（2 周）
- [ ] `LUMEN_RUNTIME=desktop` profile：API router 条件注册、desktop CurrentUser/local token、desktop settings facade。
- [ ] Docker 默认 profile 跑现有 `bash scripts/test.sh -q`、web type-check/lint/build，确认无回归。
- [ ] provider config source adapter：Docker 读 `system_settings.providers`；desktop 读临时 provider runtime file。
- [ ] local token 鉴权：Next rewrites 注入 token，API 强校验 token，直接访问 API 端口返回 401/403。

### Phase 2 — Desktop SQLite 数据层（3 周）
- [ ] 新增 `apps/api/alembic/desktop/` baseline schema。
- [ ] ORM 类型 helper：JSON / string-list / vector / partial index 按 dialect 分支。
- [ ] SQLite 连接策略：WAL、busy_timeout、quick_check、migration lock、checkpoint、备份。
- [ ] account memory sqlite-vec 虚表、重建索引任务、召回准确性测试。
- [ ] sqlite 测试矩阵覆盖 desktop 功能全集，不要求覆盖 Docker-only 功能。

### Phase 3 — Frontend Desktop Profile（2 周）‖ Phase 2
- [ ] `next.config.desktop.ts` standalone 构建和本地 Next sidecar 启动。
- [ ] desktop 导航裁剪：登录注册、admin、计费、项目、模特库、样式库入口隐藏；直接访问有明确 unsupported。
- [ ] 新增 `/assets` 顶级资产图库，复用 `/images/*` 与 `/generations/feed` 后端。
- [ ] 新增 `/onboarding/*` 首启动向导。
- [ ] 收敛散点 `fetch()`，确保请求、SSE、图片 URL 都走统一 helper。

### Phase 4 — Rust Supervisor 与鲁棒性（3 周）
- [ ] `apps/desktop/` 主体：sidecar spawn、readiness pipe、崩溃重启、退避、托盘、退出确认、诊断窗口。
- [ ] 进程树清理：正常退出、强制退出、系统关机、崩溃后重启均无孤儿进程。
- [x] 睡眠保护：macOS `IOPMAssertion`，Windows `SetThreadExecutionState`，桌面 supervisor 轮询 `/system/desktop-activity`，仅任务运行期间开启。
- [ ] 诊断包：logs、schema、Redis INFO、runtime env 摘要、版本、系统信息，全部脱敏。

### Phase 5 — Packaging / CI / 安装包 E2E（2.5 周）
- [ ] PyInstaller spec + hooks，Next standalone + Node runtime，Garnet runtime，Tauri bundle。
- [ ] mac arm64 / win x64 安装包 E2E：安装、启动、onboarding、fake provider、任务、重启、卸载。
- [ ] 资源签名扫描：macOS bundle 内全部 Mach-O 已签，Windows 全 exe/dll 已签。
- [ ] 性能基线：窗口可见、ready 时间、idle RSS、任务峰值、退出耗时。

### Phase 6 — 签名 / 公证 / 自动更新（1.5 周）
- [ ] 申请 Apple Developer ID + Windows OV/EV 证书。
- [ ] CI 签名与公证脚本。
- [ ] Tauri updater 与 GitHub Releases 集成，灰度一次完整升级流程，含 migration 前备份和失败恢复窗口。

### Phase 7 — 数据迁移 + 桌面增强（2 周）
- [ ] Docker 导出 / 桌面导入转换器，只迁移桌面功能全集数据。
- [ ] `.lumen-backup.zip` 备份/恢复，使用 SQLite `VACUUM INTO` 和 manifest 校验。
- [ ] 文件拖拽、原生通知、保存对话框、打开数据目录。

### Phase 8 — Beta → 1.2.0 GA（2 周）
- [ ] 选 5–10 名社区用户做 closed beta，收集崩溃报告与可用性反馈。
- [ ] 文档：安装、迁移、FAQ、卸载（特别是数据目录清理）。
- [ ] 发 v1.2.0：Docker 版 + 桌面 mac/win 同步上线。

**总计**：~18 人周。一个全职开发约 4-5 个月；两个开发并行（后端/数据层 + 前端/Tauri/CI）约 10-12 周。这个估算包含安装包 E2E、签名、公证、迁移失败恢复和 Windows 稳定性验证。

---

## 14. 风险与开放问题

| 风险 | 严重性 | 缓解 |
|------|--------|------|
| 用户拿到 Intel Mac / Win ARM64 包后无法运行 | L | 下载页根据 `navigator.userAgentData.architecture` 自动识别；架构不匹配时显示「请使用 Docker 版」引导，不让用户下错包 |
| PyInstaller 漏 hidden import 导致运行时崩溃 | H | CI 跑 smoke test：装到干净 VM 里启动并跑 e2e；hooks 覆盖率 review |
| Garnet 与现有 Redis/arq 用法不完全兼容 | H | P0 命令矩阵 gate；不通过则不进入主线，切 P0-B 自研 runtime bus |
| Next standalone + Node 增大包体 | M | v1.2 接受；v1.3 再评估 static export 减重 |
| Postgres → SQLite 类型损失 | M | desktop 最小 schema + 专用迁移转换器；不可逆迁移在 UI 提醒 |
| 4K 长任务（1800s）在桌面端用户睡眠/熄屏时被打断 | M | macOS 用 `IOPMAssertion`，windows 用 `SetThreadExecutionState`，worker 跑任务时阻止系统休眠 |
| SQLite 多进程写导致 busy / lock | H | 单 writer discipline、busy_timeout、短事务、migration lock、写路径压测、崩溃恢复 quick_check |
| 多 sidecar 增加崩溃面 | M | Rust supervisor 做 readiness、重启退避、进程树清理、诊断包；安装包 E2E 覆盖 |
| 国内分发 GitHub 慢 | M | 提供镜像分发渠道（自建 OSS / Cloudflare R2）；updater manifest 支持多源 |
| 用户清理桌面端没清干净 keyring 项 | L | 卸载脚本 / 「设置→重置」按钮，调用 keyring delete |

### 开放问题（决策前需要确认）
1. **是否要做 Linux AppImage**？现状不做，但成本边际很低（CI 多一个 job），需要确认。
2. **是否允许桌面版连接远程后端**？严格按本期定位是「全本地」，但有用户可能想用桌面壳做远程 admin。建议：v1.2 不做，留到 v1.3 评估。
3. **多用户 / 工作区**？例如同一台 mac 上多人用、或一个人多个项目隔离。本期设计成单 user / 单 data root；多 workspace 留到 v1.3。
4. **Garnet 许可和分发策略**：P0 确认二进制分发许可、版本固定和安全更新流程。

> 注：早前列过「是否支持 LM Studio / Ollama」——已闭环。它们都暴露 OpenAI 兼容 `/v1`，等同任意 OpenAI 兼容上游，用户在「设置 → 供应商池」里加一条 `base_url = http://127.0.0.1:11434/v1`、`api_key = sk-anything` 即可，无需额外代码。Onboarding Step 3 的提示文案里加一句「本地模型？填 Ollama / LM Studio 的本地地址即可」。

---

## 15. 验收标准

桌面版 v1.2.0 GA 必须满足：

1. macOS 12+ Apple Silicon、Windows 10 1809+ x64 上一键安装。（Intel Mac / Win ARM64 不在本期支持范围）
   - 主窗口可见 <3s。
   - 正常非首次启动 sidecar ready <30s。
   - 首次迁移 / 导入允许更久，但必须显示进度和可诊断错误。
2. 完整跑通：首启动 onboarding（4 步：欢迎/数据目录 → 起步方式 → 供应商配置+测试连接 → 偏好）→ 创建对话 → 生成图 → 在 `/assets` 资产图库看到 → 保存到本地磁盘 → 重启 app 后状态不丢；全程无登录页、无密码输入。
3. 与同版本 Docker 部署在以下功能上行为一致（差集只允许出现在 §1.4 非目标里）：对话、图像生成、资产图库（`/images/*` + `/generations/feed`）、账户记忆、设置 → 供应商池。
4. 启动后 idle 总内存目标 <900MB；生成 4K 图时峰值目标 <2.5GB。GA 前以 CI 和 beta 实测值校准，不拍脑袋承诺过窄数字。
5. 关闭主窗口不退出；选择退出后，无任务时 10s 内所有 sidecar 进程退出，无僵尸进程。有任务时必须让用户选择等待 / 终止 / 取消。
6. worker / web 单进程崩溃可自动恢复；api / redis 崩溃能进入诊断或全栈重启，不产生静默坏状态。
7. migration 前自动备份；migration 失败不得进入主界面，必须提供恢复/导出诊断路径。
8. 卸载后默认保留用户数据；提供「彻底卸载」选项删除 data root + keychain 项。

---

## 16. 参考与背景文档
- [`docker-full-stack-cutover-plan.md`](./docker-full-stack-cutover-plan.md) — 镜像 tag 与发布约定
- [`DESIGN.md`](./DESIGN.md) — 现状 REST + SSE + 模块边界
- [`one-click-update-refactor-plan.md`](./one-click-update-refactor-plan.md) — Docker 版的自动更新模式，桌面版用 Tauri 更新替代
- [Tauri v2 docs](https://v2.tauri.app/)
- [Microsoft Garnet](https://github.com/microsoft/garnet)
- [sqlite-vec](https://github.com/asg017/sqlite-vec)
- [PyInstaller manual](https://pyinstaller.org/)
