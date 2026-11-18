# Lumen 第一性原理全栈深度优化方案（v1.2.189 基线）

- **制定日期**：2026-11-18
- **代码基线**：`VERSION = 1.2.189`（仅落盘设计方案，未触发版本变更与发布标签）
- **适用范围**：`deploy/`、`apps/web`、`apps/api`、`apps/worker`、`apps/agent-runtime`、`apps/tgbot`、`packages/core`、`Dockerfile.python`、`scripts/`

---

## 0. 基线认知与第一性原理坐标系

截至 `v1.2.189`，Lumen 已完成多轮高强度工程治理：
- **已完成闭环（本方案不再重复建议）**：`docs/refactors/known-defects.json` 中的 13 项 P0/P1 历史缺陷已全部关闭；前端复杂度基线（`apps/web/scripts/complexity-baseline.json`）与 UI 语义治理基线（`apps/web/scripts/ui-governance-baseline.json`）均已清零；Outbox 持久投递、SSE 断线回放、Pi Agent 能力信封（Capability Envelope）、不可变镜像摘要（`name@sha256`）与 `update.sh` 事务日志 v2 均已就位。

在此高水位基线上继续深挖，必须回归**自托管多模态 AI 创作工作台的第一性原理**——系统的终极物理约束只有六个：
1. **网络跳数与字节熵守恒**：任何不改变业务语义的中间态代理转发、未压缩文本传输、重复连接握手都是纯粹的延迟与内存税。
2. **协作式事件循环非阻塞定律**：单线程 `asyncio` / V8 主线程一旦执行毫秒级以上的同步像素编解码、大文本反序列化或全图 `JSON.stringify`，就会成倍放大同进程所有并发流（SSE、租约续期、交互帧）的尾延迟。
3. **多模态内容寻址与零拷贝定律**：图像与视频一旦落盘即为不可变字节块，后续流转应传引用、走内核 `sendfile` 或复用定长缩略变体，严禁在应用层反复整块读入内存做 Base64 往返。
4. **上下文信噪比与算子下推定律**：向量相似度属于高维规约运算，必须在靠近数据的存储引擎（SIMD / HNSW）内完成过滤与排序，绝不能把高维浮点文本拉回解释型语言逐行解析；且非关键路径外部调用不得串入首字延迟（TTFT）关键路径。
5. **全局槽位守恒与读写职责分离（CQRS）**：各微服务连接池峰值之和加上蓝绿切换冗余必须严格小于数据库物理连接上限；高频 `GET` 读路径不得承担排他锁（`FOR UPDATE`）写修复。
6. **构建图与运行时状态正交隔离**：不同技术栈或职责的服务不得共享易污染的构建层；进程生命周期资源必须由显式容器持有，彻底消除模块级隐式全局可变单例。

---

## 1. 全景优化矩阵（12 项核心机会点）

| 编号 | 优先级 | 归属维度 | 核心问题简述 | 关键取证路径 | 预期量化收益 | 并行轨道 |
| --- | :---: | --- | --- | --- | --- | :---: |
| **OPT-01** | **P0** | 边缘与协议 | Nginx 根块重复下发严格 CSP 引发多头交集拦截风险；全局 `gzip off` 导致全站 JS/CSS/JSON 零压缩 | `deploy/nginx.conf.example`、`apps/web/next.config.ts` | 消除按模板部署的 CSP 水合阻断隐患；公网静态与大 JSON 传输体积下降 **65%~80%** | Track A |
| **OPT-02** | **P0** | 边缘与协议 | `/api/*` 与 `/events` 经 Next.js `proxy.ts` 二次代理，带来双倍 Socket、Node `80MB` 上传缓冲并阻断 `X-Accel-Redirect` | `deploy/nginx.conf.example`、`apps/web/src/proxy.ts`、`.env.example` | 消除 Node 层 API/SSE 双跳开销与 80MB 内存峰值；默认解锁 Nginx `sendfile` 零拷贝读图/读视频 | Track A |
| **OPT-04** | **P0** | 媒体与事件循环 | Worker 在主 `asyncio` 循环同步执行 `PIL` 解码与 WebP(`method=4`) 编码（镜像已装 `pyvips` 未用），且逐图新建 `AsyncClient` 串行探活/上传 | `apps/worker/app/upstream_parts/reference_images.py`、`Dockerfile.python` | 消除生图/改图期间 Worker 事件循环 `100~500ms` 假死；参考图预处理提速 **4~6x**、峰值内存降 **60%+** | Track B |
| **OPT-06** | **P0** | 记忆与向量检索 | `user_memories.embedding` 退化为无索引 `TEXT`：触发 TOAST 读放大、Top-128 静态截断失忆、Python 解析 39.3 万浮点数、同步无缓存 Embedding 阻塞 TTFT 及 N+1 审计查询 | `apps/api/alembic/versions/0019_embedding_to_text.py`、`apps/worker/app/tasks/memory_prompt_storage.py`、`prompt_assembly.py` | 记忆向量存储降 **90%+**；突破 128 条召回断崖；每轮对话省去 ~8MB TOAST 传输与 `150~600ms` 首字阻塞 | Track C |
| **OPT-08** | **P0** | 数据库与连接池 | 蓝绿发布或扩容时 `Worker(48) + API-Blue(30) + API-Green(30) = 108` 突破 PG 默认可用连接上限（97），且缺容器内存与高周转表 Autovacuum 调优 | `docker-compose.yml`、`docker-compose.bluegreen.yml`、`apps/{api,worker}/app/config.py` | 根除切流/高峰期 `TooManyConnectionsError`；抑制 `outbox_events` / `generations` 死元组膨胀 | Track D |
| **OPT-03** | **P1** | 实时多路复用 | 每个浏览器 `/events` 标签页独占一条 Redis `PubSub` 连接并以 `1.0s` 超时轮询，缺失 `sse_id` 时还在消费端反向 `XADD` | `apps/api/app/realtime/connection_hub.py`、`replay.py`、`sse_publish.py` | Redis 订阅连接数从 `O(在线标签页数)` 恒定收敛至 **`O(API进程数)=2`**，消除 1Hz 空转唤醒与读侧写竞争 | Track A |
| **OPT-05** | **P1** | 多轮视觉 I/O | 多轮对话加载历史图片时逐张串行 `2N` 查库、循环内 `commit()`、串行读盘并在主循环同步 Base64 编码 | `apps/worker/app/tasks/completion_parts/context_loading.py` | 含 5~10 张历史图的视觉会话上下文装载延迟降低 **70%+**（尤其在 CIFS/NAS 存储场景） | Track B |
| **OPT-07** | **P1** | 上下文经济学 | `SYSTEM_PROMPT_DUPLICATION_FACTOR = 2` 在单次发送 `instructions` 后仍对摘要态上下文按双倍预扣系统提示词预算 | `packages/core/lumen_core/context_window.py`、`apps/worker/app/tasks/completion_parts/context.py` | 长会话摘要态立即可多保留数千 Token 的真实近期对话消息，减少过早二次压缩 | Track C |
| **OPT-09** | **P1** | CQRS 与索引 | Canvas `read_repair.py` 在 `GET` 读请求中执行无索引 JSONB 扫描、加 `FOR UPDATE` 行锁及 `2N` 串行任务查询；`conversations` 缺侧边栏复合索引 | `apps/api/app/canvas_services/read_repair.py`、`packages/core/lumen_core/model_entities/{tasks,conversations}.py` | 消除并发查看画布时的行锁互卡与全表 JSONB 扫描；会话侧边栏列表查询稳定走 Index Scan | Track D |
| **OPT-10** | **P1** | 存储与灾备 | `backup.sh` 仅覆盖 PG+Redis，缺少媒体存储增量钩子、`fast` 更新轻量元数据锚点及主存储孤儿媒体对账回收（GC） | `scripts/backup.sh`、`apps/api/app/routes/admin_storage.py`、`apps/worker/app/main.py` | 补齐“数据库-队列-媒体块”三元一致性恢复闭环，安全回收软删/中断残留的暗数据 | Track E |
| **OPT-11** | **P1** | 前端帧预算 | 重型创作依赖（`konva` / `@xyflow/react` / `highlight.js`）按需隔离空间、Canvas 紧急草稿同步 `localStorage` 全图序列化阻塞及全局媒体字典单调增长 | `apps/web/package.json`、`apps/web/src/lib/canvas/persistence.ts`、`apps/web/src/store/useChatStore.ts` | 降低 Studio 首屏 JS 解析负担；百节点级画布高频编辑零主线程长任务掉帧；长时间跨会话浏览内存平稳 | Track F |
| **OPT-12** | **P2** | 构建与治理收官 | `Dockerfile.python` 混拷 `apps/agent-runtime/` 击穿缓存且全服务共用含 `ffmpeg` 胖镜像；剩余 13 个全局可变单例、7 个门面、5 个超参函数与 `release_proof` 闭环 | `Dockerfile.python`、`scripts/complexity-baseline.json`、`docs/refactors/*.json` | 跨栈改动零无效重建、`api`/`tgbot` 镜像大幅瘦身；架构治理得分从 `9.833` 冲刺 **`10.0/10`** | Track E/F |

---

## 2. 维度一：边缘反代、传输压缩与实时多路复用（Track A）

### OPT-01（P0）：修复 `deploy/nginx.conf.example` 双重 CSP 冲突与全站未压缩税

#### 现状取证
1. **双重 CSP 交集锁死隐患**：
   - `apps/web/next.config.ts:108-128` 动态构建了完整的应用层 `Content-Security-Policy`（包含 Next.js 生产水合必需的 `script-src 'self' 'unsafe-inline'`、`style-src 'self' 'unsafe-inline'`、`img-src 'self' data: blob:`、`worker-src 'self' blob:`）。
   - 然而 `deploy/nginx.conf.example:91` 在 `server {}` 顶层同时声明了：
     ```nginx
     add_header Content-Security-Policy "default-src 'self'; frame-ancestors 'none';" always;
     ```
   - 由于 `location /`（`nginx.conf.example:212-221`）未定义子级 `add_header` 也未 `proxy_hide_header`，Nginx 会将根块 CSP 与 Next.js 上游返回的 CSP **同时下发**。根据 W3C CSP 多策略规范，浏览器强制取所有 CSP 头的**最严交集**，等价于剥夺 `'unsafe-inline'`、`data:` 与 `blob:`，导致严格照搬示例配置部署的实例出现前端水合脚本拦截、Inpaint 画布 `blob:` Worker 失效与内联预览裂图。
2. **局部约束全局化导致全站零压缩**：
   - `deploy/nginx.conf.example:98-101` 为避免 `/events` SSE 被缓冲在 gzip 块边界，直接在 `server {}` 根块设置 `gzip off;`。
   - 这导致 `/_next/static/chunks/*.js`、50KB 级的 `globals.css`、HTML 文档以及 `/api/conversations/*`、`/api/canvas/*` 的大型 JSON 响应全部以裸字节在公网传输。

#### 第一性原理方案
- **单一安全策略源（SSOT）**：删除 `deploy/nginx.conf.example` 根块的 `Content-Security-Policy`（保留 `frame-ancestors` 由 Next.js CSP 统一输出，或在 `/api/` 非 HTML 响应上按需加最小头），并在 `scripts/lumenctl/` 的 `nginx-optimize` 校验脚本中加入“禁止双发冲突 CSP”的静态断言。
- **按 MIME 与路径正交控制压缩**：
  - 在 `server {}` 层启用 `gzip on; gzip_min_length 1024; gzip_comp_level 5; gzip_types text/plain text/css application/javascript application/json image/svg+xml application/manifest+json;`。
  - 仅在 `location /events` 与 `location ~ ^/_internal_storage/` 内显式声明 `gzip off; proxy_buffering off;`，既严格保护 SSE 帧零缓冲不变量，又恢复全站静态资源与大 JSON 的高倍率压缩。

---

### OPT-02（P0）：Nginx 直连 `lumen_api` 消除 Next.js 双跳代理与 80MB 缓冲，默认激活零拷贝 `sendfile`

#### 现状取证
- `deploy/nginx.conf.example:39-43, 120, 151` 注释掉了 `upstream lumen_api`，将 `location /events` 与 `location /api/` 统一指向 `http://lumen_web`（`127.0.0.1:3000`），再由 `apps/web/src/proxy.ts:92-116`（`NextResponse.rewrite`）二次转发到 `api:8000`。
- 为容忍 64MiB 参考视频上传穿过 Next.js，`apps/web/next.config.ts:102-105` 被迫启用实验特性 `experimental.proxyClientMaxBodySize: "80mb"`；`.env.example:56-61` 也明确记录：不改 Nginx 直连时不可开启 `LUMEN_INTERNAL_REDIRECT_ENABLED=1`（否则 `X-Accel-Redirect` 无法由外层 Nginx 正确接管并返回 404）。
- 此外，`apps/web/src/proxy.ts:119-124` 的 `matcher` 仅排除了 `_next/static|_next/image|favicon.ico|robots.txt`，导致 `/fonts/*.woff2`、`/sw.js`、`/manifest.webmanifest` 等请求仍无谓进入中间件执行 URL 与头部拷贝。

#### 第一性原理方案
1. **反代数据面直连化**：
   - 在 `deploy/nginx.conf.example` 与 `lumenctl nginx-optimize` 模板中默认解开 `upstream lumen_api { server 127.0.0.1:8000; keepalive 64; }`。
   - `location = /events` 与 `location /api/` 直接 `proxy_pass http://lumen_api;`（注意保留 `/api/` 前缀剥离语义：`proxy_pass http://lumen_api/;` 与 `src/proxy.ts` 中 `pathname.slice(4)` 严格对齐，并配对端到端路由契约测试）。
   - `apps/web/src/proxy.ts` 完整保留，仅服务于本地开发（`npm run dev`）或未配反代的纯内网 `:3000` 直连模式；同时收紧 `proxy.ts` 的 `matcher`，排除 `/fonts/:path*`、`/sw.js`、`/manifest.webmanifest`。
2. **全媒体零拷贝闭环**：
   - 当 Nginx 直连 `lumen_api` 后，生产模板可默认开启 `LUMEN_INTERNAL_REDIRECT_ENABLED=1`。
   - 将当前仅覆盖图片二进制/变体的 `X-Accel-Redirect`（`/_internal_storage/`）同步扩展至已通过鉴权校验的 `/api/videos/*/binary`（支持 HTTP `Range` 字节范围请求由 Nginx `sendfile` 原生处理），彻底把 Python `uvicorn` 从多 MB 视频拖动播放的字节流中解放出来。

---

### OPT-03（P1）：`connection_hub.py` 进程级 Redis Pub/Sub 多路复用与写路径统一赋号

#### 现状取证
- `apps/api/app/realtime/connection_hub.py:518-560` 中，每条 `/events` SSE 请求都会调用 `state.redis.pubsub()` 独占一条底层 Redis TCP 连接，并在 `live_events`（`lines 471-482`）中以 `timeout=1.0s` 循环执行唤醒检查。若 80 名活跃用户各开 3 个标签页，仅 SSE 就长占 240 条不可池化的 Redis 连接，并产生每秒 240 次协程唤醒。
- `connection_hub.py:304-312, 335-343` 在收到未带 `sse_id` 的广播（含 `context.compaction` 桥接频道）时，会在每个订阅端协程里调用 `stream_id_for_pubsub_event` 反向 `XADD` 写回用户 Stream（`replay.py:92-128`），多标签页并发收到同一消息时会重复向 Stream 追加事件。

#### 第一性原理方案
- **单进程单订阅连接扇出（`ProcessRealtimeMultiplexer`）**：在 FastAPI `lifespan` 启动时，每个 Uvicorn Worker 仅维护 **1 条** Redis Pub/Sub 连接（按当前进程活跃订阅集合引用计数执行动态 `subscribe` / `unsubscribe`），内部维护 `dict[channel, set[asyncio.Queue]]` 内存路由表。客户端 SSE 协程只 `await asyncio.wait_for(client_queue.get(), timeout=15.0)`（直接与 `KEEPALIVE_INTERVAL_SECONDS=15` 对齐，消除 1Hz 空轮询）。
- **发布端唯一赋号铁律**：审计并收敛所有旁路发布点（特别是 `compaction` 进度事件），统一经由 `sse_durable` 原子执行 `XADD -> 获取 sse_id -> PUBLISH`，将消费端 `stream_id_for_pubsub_event` 降级为仅记录告警指标的只读兜底。

---

## 3. 维度二：多模态媒体管线与事件循环非阻塞（Track B）

### OPT-04（P0）：激活 `pyvips` 异步线程管线与长连接并行上传，消除 Worker 事件循环卡顿

#### 现状取证
- `apps/worker/app/upstream_parts/reference_images.py:412-445`（`_resolve_reference_image_urls`）在 `async def` 中同步调用 `_normalize_reference_image(raw)`（`lines 37-119`），使用 `PIL.Image` 解码原图并以 `WEBP (quality=90, method=4)` 编码。在多参考图生图/改图场景下，主事件循环会被单核 CPU 编码阻塞数百毫秒，拖慢同进程所有 `run_completion` 流式增量推送与分布式锁心跳。
- 对比 `Dockerfile.python:91, 108-114`：生产镜像早已专门安装了 `libvips42` 并在构建期校验 `pyvips >= 8.14`，但 `reference_images.py` 完全未利用 `pyvips`。
- 同时，`_reference_url_is_live`（`lines 296-300`）与 `_push_reference_to_image_job`（`lines 389-407`）对每张图都新建并关闭一次性 `httpx.AsyncClient`，且外层 `for raw in images:` 完全串行。

#### 第一性原理方案
1. **切换 `pyvips` + 线程池隔离**：
   - 使用 `pyvips.Image.new_from_buffer(raw, "", access="sequential")` 校验尺寸像素上限、剥离元数据（`strip=True`）并输出 WebP（`Q=90, effort=3`），通过 `await asyncio.to_thread(...)` 执行。`libvips` 在 C 层释放 Python GIL 且采用扫描线流式内存模型，单张 4K 参考图峰值内存可从百 MB 级降至十 MB 级，耗时缩短 75%+。
2. **连接池复用与受控并发（Bounded Fan-Out）**：
   - 在 `ImageUpstreamRuntime` 中持有生命周期绑定的 `image_job` 专用 `httpx.AsyncClient`（启用 HTTP/1.1 Keep-Alive 连接池）及探测客户端。
   - 在 `_reference_cache_get` 命中且距离上次写入/验证未超过安全窗口（如 `< 60s` 且远早于 `expires_at`）时，免除每次必发的网络 `HEAD` 探活；对多张输入参考图使用 `asyncio.Semaphore(4)` + `asyncio.gather` 并行完成归一化与上传。

---

### OPT-05（P1）：多轮视觉上下文历史附图批量预取与存储并发读取

#### 现状取证
- `apps/worker/app/tasks/completion_parts/context_loading.py:60-179` 中，每当用户在一个包含多张历史图片的会话里继续追问时，`_build_input_from_packed_context` 串行遍历消息与附件，逐张执行 `session.get(Image)` + `select(ImageVariant ... kind == 'preview1024')`，并在 `_attachment_to_data_url` 内每张图执行一次 `await commit()`（`line 86`），再串行调用 `storage_get_bytes(key)` 和主线程 `base64.b64encode(raw)`。

#### 第一性原理方案
- **两阶段批处理（Batch Resolve -> Parallel Read）**：
  1. 扫描 `packed._recent_rows` 提取去重后的 `image_ids` 列表，用 **1 条** SQL 查询批量取出 `Image` 及其 `preview1024` `ImageVariant` 的 `(image_id, storage_key, mime, sha256)`，随后一次性结束 DB 读事务（释放数据库连接回池，绝不在磁盘/NAS I/O 期间占住连接）。
  2. 通过 `asyncio.Semaphore(4)` 并行在 `asyncio.to_thread` 内完成“文件读取 + Base64 编码”（将 `base64.b64encode` 一并放入工作线程，避免大字符串分配卡顿主循环）。
  3. 可选在进程内挂载基于 `sha256` 键控的轻量字节上限 LRU（例如上限 `32MB`、TTL `300s`），让连续多轮追问同一组参考图时实现 **0 磁盘 I/O**。

---

## 4. 维度三：上下文经济学与账号记忆向量下推（Track C）

### OPT-06（P0）：基于 `pgvector` `halfvec` + HNSW 原生下推重构账号记忆检索管线

#### 现状取证
- **存储与索引退化**：`docker-compose.yml:40` 运行着完整的 `pgvector/pgvector:pg16` 容器，但 `apps/api/alembic/versions/0019_embedding_to_text.py` 因早期 `asyncpg` 字符串参数未显式 `CAST` 触发 `DatatypeMismatchError`，把 `user_memories.embedding` 和 `user_memory_staging.embedding` 退回为普通 `TEXT`。
- **连锁性能与质量代价**：
  1. **TOAST 读放大与 CPU 空耗**：3072 维浮点数的 JSON 文本表示每行约 `40~65KB`，全部落入 PostgreSQL TOAST 表；`memory_prompt_storage.py:48-80` 每轮查询 `LIMIT 128` 时需跨进程传输数 MB 向量文本，再在 `prompt_assembly.py:45-53` 主线程里执行 `128 × 3072 ≈ 39.3 万` 次浮点解析与纯 Python 余弦计算。
  2. **128 条召回断崖（Recall Cliff）**：SQL 仅按 `(pinned, confidence, updated_at)` 截断前 128 条，一旦重度创作者累积数百条项目/角色/风格记忆，高语义相关但较早创建的记忆永远进不了候选集。
  3. **首字延迟（TTFT）同步阻塞**：`prompt_assembly.py:42` 对每条 `>= 5` 字符的用户输入同步等待远程 `text-embedding-3-large` API（超时达 `15s`，无缓存），直接推高每次聊天的首包响应时间。
  4. **N+1 审计查询**：`prompt_assembly.py:173-201` 在循环内逐条对 `MemoryAudit JOIN Message` 执行 `COUNT(*)`。

#### 第一性原理方案
1. **`halfvec` 列类型演进与双数据库兼容封装**：
   - 在 `packages/core/lumen_core/sqltypes.py` 定义自定义 `EmbeddingVectorType(TypeDecorator)`：在 SQLite 测试环境下映射为 `Text`；在 PostgreSQL 方言下通过 `bind_expression` 生成 `CAST(:param AS halfvec(3072))`（直接根治 `0019` 遇到的 `asyncpg` 类型绑定报错，无需引入额外 ORM 重耦合）。
   - 新增 Alembic 在线迁移：将 `user_memories.embedding` 与 `user_memory_staging.embedding` 转为 `halfvec(3072)`（每行从 `~60KB` 骤降至 **`6KB`**，脱离大块 TOAST 膨胀），并创建局部 HNSW 索引：
     ```sql
     CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_user_memories_embedding_hnsw
       ON user_memories USING hnsw (embedding halfvec_cosine_ops)
       WHERE disabled = false AND superseded_by IS NULL AND embedding IS NOT NULL;
     ```
   - *进阶收益*：`text-embedding-3-large` 原生支持 Matryoshka 降维参数 `dimensions=1536`（语义保留率 >99%），若在后续 re-embed 任务切换至 `halfvec(1536)`，单行仅 **`3KB`**，检索再快一倍。
2. **SQL 向量算子下推与零向量回传**：
   - 查询拆为极轻量的两步或 `UNION ALL`：先精确拉取必带集合（`type IN ('profile', 'avoid') OR pinned = true`，不 `SELECT embedding` 列！），再用 `ORDER BY (1 - (embedding <=> CAST(:q AS halfvec(3072)))) * (1 + 0.1 * positive_signal - 0.15 * negative_signal) DESC LIMIT 16` 直接在 PG 内召回全量记忆库中最相关的候选，仅返回 `(id, type, content, confidence, positive_signal, updated_at)`，彻底消灭 128 条召回上限与 Python 端向量解析。
3. **Embedding 软超时熔断 + Redis 查询缓存 + 审计批量聚合**：
   - 对归一化 `sha256(user_text)` 在 Redis 缓存查询向量（TTL `10min`，用紧凑 `float16` bytes 存储仅 3~6KB），避免重试/重新生成/短语重复请求打上游。
   - 主对话路径为 `embedding_vector()` 施加严格软超时（例如 `600~800ms`）：一旦上游抖动超时，立即无感降级为注入 `profile + avoid + pinned + 最近活跃 Top-8`，保证主对话 TTFT 永不被 Embedding 拖垮。
   - 将 `pick_confirmation_candidate` 内的 `MemoryAudit` 检查合并为 1 条 `SELECT memory_id FROM memory_audit JOIN messages ... WHERE memory_id IN (...) GROUP BY memory_id` 预取集合。

---

### OPT-07（P1）：消除 `SYSTEM_PROMPT_DUPLICATION_FACTOR = 2` 预算虚扣

#### 现状取证
- `packages/core/lumen_core/context_window.py:52, 97-105` 因早期协议兼容，将系统提示词预算乘以 `SYSTEM_PROMPT_DUPLICATION_FACTOR = 2`。
- 当前 `apps/worker/app/tasks/completion_parts/context_loading.py:143-179` 已不在 `input_list` 中重复塞入 `system` 消息；未压缩路径通过 `context_runtime.py:55-66`（`estimate_system_prompt_tokens_once`）打补丁减回了 1 倍，但摘要路径 `context.py:100-103`（`_pack_with_existing_summary`）仍沿用 2 倍估算，导致携带自定义长角色设定与账号记忆的会话在摘要后白白少装一倍系统提示词长度的近期上下文。

#### 第一性原理方案
- 将 `estimate_system_prompt_tokens` 基准回归真实单份计数（`duplication_factor: int = 1`），移除 `estimate_system_prompt_tokens_once` 补丁式相减逻辑；如特定旧版上游适配器确需双写，由该适配器显式传入 `duplication_factor=2`。

---

## 5. 维度四：数据库并发数学、CQRS 读路径纯化与存储闭环（Track D & Track E）

### OPT-08（P0）：PostgreSQL 全局连接槽位守恒与高周转表 Autovacuum 调优

#### 现状取证
- **连接数学越界**：
  - `apps/worker/app/config.py:88-89`：`db_pool_size=32, db_max_overflow=16` → 单 Worker 峰值 **48** 连接。
  - `apps/api/app/config.py:146-147` + `docker-compose.yml:117`：单 Uvicorn Worker 峰值 `5 + 10 = 15` 连接，默认 `--workers 2` → **30** 连接。
  - 在运行 `docker-compose.bluegreen.yml` 蓝绿发布期间，`api`（30）+ `api-green`（30）+ `worker`（48）= **108** 连接（尚未计入 `tgbot`、`alembic`、`backup.sh` 的 `pg_dump`），而 `docker-compose.yml:39-66` 中 `lumen-pg` 使用默认 `max_connections=100`（普通用户可用仅 **97**），峰值并发下必然触发连接拒绝。
- **高周转 MVCC 死元组积压**：`outbox_events`（每秒认领更新 `claim_owner`/`published_at`）、`generations`、`completions`、`canvas_node_executions` 状态翻转极频繁，默认 PG `autovacuum_vacuum_scale_factor = 0.2`（表行数变化 20% 才触发清理）会导致索引与堆表随历史行累积越来越慢。

#### 第一性原理方案
1. **建立硬性连接守恒预算**：
   - 在 `docker-compose.yml` 为 `postgres` 显式传入调优参数：
     ```yaml
     command:
       - "postgres"
       - "-c"
       - "max_connections=200"
       - "-c"
       - "shared_buffers=256MB"
       - "-c"
       - "effective_cache_size=768MB"
       - "-c"
       - "work_mem=8MB"
       - "-c"
       - "random_page_cost=1.1"
     ```
   - 同时收敛 `apps/worker/app/config.py` 默认连接池为 `db_pool_size=20, db_max_overflow=12`（Worker 64 个协程绝大部分生命周期处于等待上游 HTTP/SSE I/O，已落实短事务模式，无需长占 48 个物理连接），并在 `tests/` 增加跨配置契约测试校验 `2 * API_MAX + WORKER_MAX + TGBOT_MAX + 15 < PG_MAX_CONNECTIONS`。
2. **表级激进 Autovacuum 与 Outbox 已发布记录生命周期裁剪**：
   - 通过 Alembic 为 `outbox_events`、`generations`、`completions`、`canvas_node_executions` 设置表级存储参数 `autovacuum_vacuum_scale_factor = 0.02, autovacuum_analyze_scale_factor = 0.01, fillfactor = 90`（利用 PostgreSQL HOT Update 减少索引写放大）。
   - 在 Worker 每日低峰 Cron 中增加已发布 `outbox_events`（如 `published_at < now() - interval '7 days'`）的分批归档删除（`LIMIT 5000` 循环），让 `ix_outbox_unpublished_created` 与主表体积永远保持紧凑。

---

### OPT-09（P1）：Canvas `read_repair.py` 读写分离（剥离 `GET` 排他锁）与补齐高频复合索引

#### 现状取证
1. **读路径重型写副作用**：`apps/api/app/canvas_services/read_repair.py` 在处理画布读取时：
   - `_repair_missing_links`（`lines 69-106`）使用 `Generation.upstream_request["canvas_execution_id"].as_string().in_(missing_ids)` 扫描 JSONB，而 `generations` / `video_generations` 无对应表达式索引，退化为用户级全历史行扫描。
   - `_auto_select` 与 `_reconcile_execution`（`lines 310-335, 398-425`）在 `GET` 事务中对 `CanvasDocument`、`CanvasNodeSelection`、`CanvasExecutionTask` 执行 `with_for_update()` 排他锁，并在 `for task in tasks:` 内串行发起 `2N` 次 `Generation`/`Image`/`Video` 单行查询。
2. **侧边栏会话列表缺失覆盖复合索引**：`packages/core/lumen_core/model_entities/conversations.py:29-64` 中 `Conversation` 表未定义针对 `(user_id, archived, pinned, last_activity_at) WHERE deleted_at IS NULL` 的复合索引。

#### 第一性原理方案
- **CQRS 化改造**：
  - 将 `_project_generation` / `_project_video` 改造为一次 `WHERE id IN (...)` + `WHERE owner_generation_id IN (...)` 批量投影（从 `2N+1` 次查询压至固定 `2~4` 次查询）。
  - `GET` 请求仅在内存中计算最新只读视图返回给前端；若检测到持久化状态滞后（存在未落盘收敛的终态任务），通过 Redis 去重键（`SET canvas:repair_due:{canvas_id} 1 EX 15 NX`）异步投递给已有的 `canvas_execution_reconcile` Worker 任务完成加锁落库，彻底消除 `GET` 路径的 `FOR UPDATE` 锁竞争。
- **补齐两类高收益索引（`CREATE INDEX CONCURRENTLY`）**：
  - `ix_conversations_user_sidebar_active`：`conversations (user_id, archived, pinned DESC, last_activity_at DESC, id DESC) WHERE deleted_at IS NULL`。
  - `ix_generations_canvas_exec_id` / `ix_video_generations_canvas_exec_id`：局部表达式索引 `(user_id, ((upstream_request->>'canvas_execution_id'))) WHERE (upstream_request ? 'canvas_execution_id')`。

---

### OPT-10（P1）：存储-数据库双向一致性巡检（Orphan Media GC）与增量灾备闭环

#### 现状取证
- `scripts/backup.sh` 仅打包 PostgreSQL 与 Redis，`README.md:174-199` 指出 `/opt/lumendata/storage` 需外部自行快照，且默认 `LUMEN_UPDATE_MODE=fast` 跳过更新前备份。
- `image-job` 子项目已具备完善的孤儿文件扫描状态机（`image-job/image_job/persistence_parts/retention_orphans.py`），但主项目 `/opt/lumendata/storage/u/*` 尚缺周期性的“数据库引用 vs 物理文件”双向对账，长期运行后因上传中断、硬删残余或库回滚会产生悬空文件或幽灵元数据。

#### 第一性原理方案
- **`fast` 更新轻量锚点**：在 `update.sh` 的 `fast` 模式中，即使跳过耗时的全库 `pg_dump`，仍用 `<100ms` 自动保存当前 `alembic current`、`system_settings` 脱敏摘要与 `.env` 校验指纹至更新日志目录，提升故障诊断与快速回退确定性。
- **可选媒体备份钩子**：在 `backup.sh` 尾部预留受控的 `LUMEN_STORAGE_BACKUP_HOOK`（支持对接 `restic` / `rclone` / ZFS-Btrfs 快照），并在 `.backup.last-success.json` 中记录该时刻 `storage` 文件计数与总字节水位，恢复时自动提示媒体时间窗偏差。
- **两阶段宽限期孤儿回收（Grace-Period Mark & Sweep）**：在 Worker 低峰任务与 Admin Storage 面板增加只读巡检模式（对比 `mtime < now - 48h` 的物理文件与 `images` / `image_variants` / `videos` / `model_library_items` / `poster_style_items` 引用表），先移入隔离回收目录观察 7 天再物理清除，确保零误删。

---

## 6. 维度五：前端帧预算、按需拆包与画布扩展性（Track F）

### OPT-11（P1）：重型创作引擎按需隔离、Canvas 异步增量草稿与全局媒体字典 LRU 回收

#### 现状取证
- **重依赖面**：`apps/web/package.json:45-64` 包含 `konva` + `react-konva`（蒙版绘制）、`@xyflow/react`（无限画布）、`highlight.js`（代码高亮）、`framer-motion` 等大型包，且构建脚本固定 `next build --webpack`。
- **同步 `localStorage` 序列化阻塞**：`apps/web/src/lib/canvas/persistence.ts:148-180`（`putCanvasEmergencyDraft`）在主线程对完整 `draft`（含整棵 `CanvasGraph` 与 `operations`）执行同步 `JSON.stringify` 并读写 `localStorage`（上限达 512KB / 总计 2MB）。在百节点大型分镜/工作流画布中，同步序列化与字符串拼接易突破 `16.6ms` 帧预算。
- **长会话内存驻留**：`apps/web/src/store/useChatStore.ts:152-176` 在 `setCurrentConv` 切换会话时合并保留全局 `generations` 与 `imagesById` 以承接后台任务，但对早已进入终态且不在最近缓存会话（`CONVERSATION_HISTORY_CACHE_LIMIT = 32`）内的历史条目缺乏容量淘汰。

#### 第一性原理方案
1. **严格交互级代码分割（Code Splitting）与构建预算门禁**：
   - 为 `apps/web/scripts/` 增加构建产物 Chunk 体积门禁（首屏共享 JS Gzip 预算棘轮），确保 `konva` 仅在打开 Inpaint 编辑器时加载、`@xyflow/react` 仅在进入 `/projects/canvas` 时加载、`highlight.js` 仅按需加载常见语言子集并在检测到代码块时异步注入。
   - 评估并验证 Next.js 16 原生打包器对比 `--webpack` 的构建耗时与产物体积收益。
2. **Canvas 草稿纯异步化**：
   - 日常编辑期间仅写 IndexedDB（`save-batches` 增量操作流，异步非阻塞主线程）；`localStorage` 紧急区仅存 `<1KB` 的最新 `dirty_revision + client_id` 元数据指针，仅在 `pagehide` 阶段且 IndexedDB 未确认落盘时才写入精简增量差集（Delta Ops），彻底避免主线程全图 `JSON.stringify`。
3. **全局媒体状态有界化**：
   - 在 `useChatStore` 的 `scheduleBase64Eviction` 周期内同步执行“终态非活跃条目 GC”：保留当前会话、32 个近期缓存会话及所有非终态（`queued`/`running`）关联的 `generations`/`imagesById`，其余终态历史对象按 LRU 上限（如 1,000 条）平滑驱逐。

---

## 7. 维度六：容器构建缓存解耦与架构治理满分收官（Track E / Track F）

### OPT-12（P2）：`Dockerfile.python` 分层解耦瘦身与架构治理技术债清零（冲刺 `10.0/10`）

#### 现状取证
1. **跨栈构建缓存污染与胖镜像**：
   - `Dockerfile.python:61` 执行了 `COPY apps/agent-runtime/ apps/agent-runtime/`——将纯 Node/TypeScript 服务源码拷入了 Python 镜像，导致仅修改 `agent-runtime` 也会使 `api`、`worker`、`tgbot` 的 Docker 构建缓存失效并产生全新镜像 Digest。
   - 同时 `api` 与 `tgbot` 容器均基于同一个安装了 `ffmpeg`、`libvips42`、`openssh-client`、`sshpass` 及全套 Workspace Python 包的单体 `runtime` 阶段构建。
2. **可量化的剩余治理债清单（距离零债务仅一步之遥）**：
   - `.audit_state/governance-score.pre-release.md`：总分 `9.833/10`，唯一未过硬门禁为发版前本地/CI `release_proof` 证据自动归档。
   - `docs/refactors/module-runtime-state-ledger.json`：剩余 **13 处**模块级可变全局状态（`arq_pool._pool_state`、`redis_client._redis_state`、`provider_pool._RUNTIME`、`context_window._TOKEN_COUNTER_RUNTIME` 等）。
   - `docs/refactors/compatibility-facade-retirement.json`：剩余 **7 个**兼容门面（其中 `worker` 的 2 个门面仅剩 **1 处**调用者，随时可零风险退役；`api/services/billing_cache.py` 仅剩 **5 处**调用者）。
   - `scripts/complexity-baseline.json`：仅剩 **5 个**超参函数（最突出为 `apps/api/app/services/message_submission.py::create_assistant_task` 的 **29 个参数**）。
   - `docs/refactors/runtime-coupling-inventory.json`：全仓仅剩最后 **2 处** `module-contextvar`。

#### 第一性原理方案
1. **容器构建正交化**：
   - 立即从 `Dockerfile.python` 删除无关的 `COPY apps/agent-runtime/`（同步补充 `.dockerignore` 规则并校验 `update.sh` 的 release 文件提取清单不依赖该目录）。
   - 将 `Dockerfile.python` 拆分为共享 Python `.venv` 编译底座 + 轻量 `runtime-slim`（供 `tgbot` / `migrate` / `bootstrap` 使用，不含 `ffmpeg`）与多媒体 `runtime-media`（供 `api` / `worker` 使用），显著缩小镜像拉取体积与系统软件 CVE 暴露面。
2. **治理棘轮分批清零**：
   - **速赢清退**：先退役仅剩 1~5 个调用者的 3 个门面（`default_runtime.py`、`entrypoints.py`、`api/services/billing_cache.py`），并将 `create_assistant_task` 等 5 个超参函数收敛为冻结 `dataclass` 命令对象，使 `scripts/complexity-baseline.json` 实现与前端一致的 **0 违规清零**。
   - **生命周期收口**：将剩余 13 个模块级可变单例逐步迁入 `apps/api/app/runtime/container.py`、`apps/worker/app/runtime/container.py` 与 `TgBot` 启动上下文，同步下调 `module-runtime-state-ledger.json` 的 `max_total` 棘轮直至归零。

---

## 8. 多工作流并行推进路线图（零文件冲突切分）

为最大化利用多 Agent / 多 Worktree 并行开发效率，上述 12 项优化按**文件所有权完全正交**切分为三条推进波次、六个并行工作流：

### Wave 1：高收益零侵入速赢波（可 4 路完全并行，无 DB 迁移依赖）
- **Workstream A1（边缘与反代）**：落盘 `OPT-01` + `OPT-02`（修改 `deploy/nginx.conf.example`、`scripts/lumenctl/` 反代模板、`apps/web/src/proxy.ts` matcher，补齐视频二进制 `X-Accel-Redirect` 支持）。
- **Workstream B1（Worker 媒体与上下文 I/O）**：落盘 `OPT-04` + `OPT-05`（重构 `apps/worker/app/upstream_parts/reference_images.py` 启用 `pyvips` + 线程池 + 连接池并发；重构 `apps/worker/app/tasks/completion_parts/context_loading.py` 批量查库与并行读盘）。
- **Workstream C1（预算校准与审计聚合）**：落盘 `OPT-07` + `OPT-06` 的非迁移部分（修正 `context_window.py` 双倍系统提示词系数、合并 `prompt_assembly.py` N+1 审计查询、接入 Redis 查询向量短缓存与软超时降级）。
- **Workstream D1（连接池守恒与构建缓存）**：落盘 `OPT-08` 配置部分 + `OPT-12` 构建部分（校准 `docker-compose.yml` PG 参数与 Worker 连接池上限、清理 `Dockerfile.python` 的 `agent-runtime` 混拷、清退 `scripts/complexity-baseline.json` 5 个超参函数）。

### Wave 2：核心数据层与实时扇出升级波（含在线非破坏性 Alembic 迁移）
- **Workstream C2（`pgvector` `halfvec` 下推）**：完成 `OPT-06` 数据库迁移（`EmbeddingVectorType` + `halfvec(3072)` 在线转换 + HNSW 局部索引 + SQL 向量 Top-K 下推）。
- **Workstream D2（Canvas CQRS 与热表索引/Autovacuum）**：完成 `OPT-09` + `OPT-08` 迁移部分（剥离 `read_repair.py` 的 `GET` 写锁、批量化任务投影、补齐 `conversations` 与 `canvas_execution_id` 索引、配置热表 Autovacuum 参数与已发布 Outbox 定期裁剪）。
- **Workstream A2（SSE 进程级多路复用）**：完成 `OPT-03`（`ProcessRealtimeMultiplexer` 单连接扇出与发布端全量 `sse_id` 赋号校验）。

### Wave 3：长效自治与体验精修波
- **Workstream F1（前端拆包、异步画布草稿与状态回收）**：完成 `OPT-11`（Chunk 体积门禁、Canvas 紧急草稿 Delta 化、`useChatStore` 终态媒体 LRU 淘汰）。
- **Workstream E1（存储对账 GC 与治理清零）**：完成 `OPT-10` + `OPT-12` 收尾（`fast` 更新轻量锚点、孤儿媒体宽限期对账回收、退役剩余兼容门面与模块全局单例，达成治理评分 `10.0/10`）。

---

## 9. 验收门禁与回归契约

任一波次合入前必须通过仓库既有硬性门禁及对应专项基准：
1. **全栈功能与契约门禁**：`bash scripts/test.sh -q`
2. **架构分层与耦合棘轮**：`uv run python scripts/check_architecture.py`、`uv run python scripts/check_complexity.py`、`uv run python scripts/module_runtime_state_audit.py`
3. **Alembic 在线迁移安全门禁**：`uv run python scripts/lint_alembic_breaking.py`（确保所有新索引使用 `CONCURRENTLY` 或遵循 expand-contract 规范）
4. **前端质量与治理门禁**：`cd apps/web && npm run type-check && npm run lint && npm run build`
5. **性能基线对比验证**：复跑 `perf/wave0`、`perf/wave2`、`perf/wave3` 采集脚本，记录 P95 TTFT、Worker 事件循环最大阻塞延迟（Event-Loop Lag）、Redis 活跃连接数及数据库锁等待耗时对比证据。
