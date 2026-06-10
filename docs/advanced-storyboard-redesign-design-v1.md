# 高级故事板重设计执行文档 v1

**状态**：设计稿，待评审
**日期**：2026-06-11
**关联文件**：`apps/web/src/app/video/page.tsx`、`apps/api/app/routes/videos.py`、`packages/core/lumen_core/models.py`、`docs/video-generation-design.md`

---

## 1. 现状核心问题诊断

通读 7200 行的 `video/page.tsx` 和相关后端代码，现有高级故事板存在四类根本性缺陷。

### 1.1 数据层：无持久化，刷新即丢

所有项目状态（脚本、设定图审批、分镜顺序、关键帧绑定关系）全部活在 React `useState`，没有写任何持久化。刷新页面等于丢失全部工作。`ensureStoryboardConversation` 虽然创建了一个对话 session 用于 AI 生成，但这个 `conversationId` 本身也是 `useState`，同样不持久。当前产品根本不具备"项目"语义，只能叫"临时草稿"。

### 1.2 架构层：模式开关内嵌 + 巨型单文件

故事板作为一个"功能开关"塞在 `/video` 页的 `VideoWorkspaceMode = "storyboard" | "single"` 里，与单条生成器共享同一个 7200 行组件。项目中心 `/projects` 里的「分镜制作」入口只是一个跳转到 `/video` 的占位卡片。这导致：
- 没有项目列表（无法管理多个故事板项目）
- 功能入口层级混乱（项目中心是壳，真正内容在视频页的开关里）
- 单文件 prop drilling 严重（`StoryboardWorkspace` 组件接收 47 个 props）

### 1.3 UX 层：六阶段流程设计存在多处认知断层

```
想法 → 脚本 → 人物/场景 → 分镜 → 分镜图 → 视频
```

这六步设计在以下位置造成困惑：

**阶段跳转无约束**：六个阶段按钮可以自由跳转，用户可以在没有脚本的情况下直接点到"视频"阶段，看到空状态，不知道怎么回来。没有线性引导或前置条件门控。

**设定（assets）与分镜（shots）耦合不透明**：`assetIds` 字段决定哪些人物/场景参考图会被带入关键帧生成，但 UI 里这个绑定关系是通过 Shots 阶段里一个不起眼的 checkbox 列表来管理的（`StoryboardShotEditor` 第 5593 行起）。用户不知道"批准设定图"和"生成分镜图"之间有什么关系。

**"批准"概念过载**：同一个"批准"动作出现在四处（脚本锁定、设定图批准、生成段批准、关键帧批准），每次批准的含义和后果不同，但 UI 呈现方式几乎一样——一个绿色 badge + 一个按钮，没有任何说明为什么要批准、批准后会发生什么。

**成片缺失**：用户完成所有 N 个 15s 片段后，没有任何拼接/导出路径。产品在最关键的收尾步骤上断掉了。

### 1.4 技术层：对话池污染与关键帧队列串行

**AI 调用走普通对话**：`runStoryboardTextTask` 和 `submitStoryboardImageTask` 都通过 `postMessage`/conversation 接口发送，与用户正常聊天共用同一条对话链。虽然后端通过 `workflow_type` 过滤排除在会话列表外，但这些请求仍然占用对话配额、产生计费记录，且图片生成依赖 `conversationId` 绑定，不是独立调用。

**关键帧批量生成是串行的**：`generateAllStoryboardKeyframes`（第 2528 行）是一个 `for...of` 循环，逐个 `await generateStoryboardShotKeyframe`。10 个分镜段就是 10 次串行等待，每次图片生成约 10-20 秒，用户要等 2-3 分钟。

---

## 2. 目标产品形态

一个成熟的短视频故事板工具，参考 FrameIO 的项目管理模式 + Runway 的帧审批模式，适配 Lumen 的现有 AI 生成能力：

**核心体验**：用户进入「分镜制作」看到自己的项目列表。点进一个项目，进入专属工作区。工作区内每一步都有清晰的进入条件和完成标志。任何时候刷新，状态完整恢复。最终能生成完整视频序列并导出。

**与现有 apparel/poster workflow 的关系**：复用 `WorkflowRun/WorkflowStep` 数据模型（不新建表），但故事板的业务逻辑独立于 `workflows.py` 文件，新建 `routes/storyboards.py`，避免把已有的 9600 行文件继续膨胀。

---

## 3. 数据模型设计

### 3.1 复用 WorkflowRun（不新建表）

`WorkflowRun` 已有足够的通用字段：

```
type = "storyboard"
title = 项目名
user_prompt = 项目想法（idea）
status = "draft" | "in_progress" | "completed"
metadata_jsonb = {
  style: string,           # 视觉连续性描述
  script: string,          # 脚本正文
  script_confirmed: bool,
  script_revision: int,
  script_approved_revision: int,
  script_approved_at: string,
  aspect_ratio: string,
  resolution: string,
  generate_audio: bool,
  model: string,
  seed: int | null,
  conversation_id: string | null,   # 绑定 AI 对话
}
```

### 3.2 WorkflowStep 映射故事板各阶段资产

每个人物/场景/道具资产作为一个 `WorkflowStep`，`step_key = "asset:{uuid}"`：

```
step_key   = "asset:{id}"
status     = "waiting_input" | "generating" | "ready" | "approved"
input_json = {
  kind: "character" | "scene" | "prop",
  name, role, description, continuity, revision
}
output_json = {
  prompt: string,
  image_id: string | null,
  image_url: string | null,
  approved_at: string | null,
}
image_ids = [image_id]    # 便于后端按 generation.workflow_step_key 关联
```

每个分镜段也作为一个 `WorkflowStep`，`step_key = "shot:{uuid}"`：

```
step_key   = "shot:{id}"
status     = "draft" | "approved" | "keyframe_ready" | "keyframe_approved" | "generating" | "done"
input_json = {
  index: int,
  title, purpose, narration, visual,
  shot_type, camera_move, transition, reference_notes,
  duration_s: int,
  asset_ids: string[],
  keyframe_prompt: string,
  keyframe_source_hash: string | null,
}
output_json = {
  keyframe_image_id: string | null,
  keyframe_image_url: string | null,
  keyframe_approved_at: string | null,
  video_generation_id: string | null,
}
image_ids  = [keyframe_image_id]
task_ids   = [video_generation_id]
```

最终成片作为一个 step：`step_key = "assembly"`：

```
step_key   = "assembly"
status     = "waiting" | "compositing" | "done" | "failed"
output_json = {
  video_id: string | null,         # 合成产物 Video 表 id
  segment_ids: string[],           # 按顺序排列的片段 video_generation_id
}
task_ids   = [composite_job_id]
```

> **为什么不建新表**：`WorkflowStep` 的 `input_json/output_json/image_ids/task_ids` 已能承载所有需要持久化的状态。扁平化的 `step_key` 方案避免了 nested JSON 难以查询的问题——按 `workflow_run_id` + `step_key LIKE 'shot:%'` 即可拉所有分镜段。代价是 shots 不能在数据库层做排序，排序由 `input_json.index` 字段在应用层维护，这是合理的取舍（shots 数量上限 60，排序在 API 层一次读出后内存排序即可）。

### 3.3 新迁移文件

迁移只需在 `workflow_runs.type` 添加 `storyboard` 为合法值（如果有约束检查的话；当前 `type` 是 `String(64)` 无枚举约束，不需要改）。

**不需要新迁移**。

---

## 4. 后端 API 设计

新建 `apps/api/app/routes/storyboards.py`，在 `main.py` 注册 `prefix="/storyboards"`。

### 4.1 端点列表

| Method | Path | 说明 |
|---|---|---|
| `POST` | `/storyboards` | 创建项目（title + idea），返回 `StoryboardRunOut` |
| `GET` | `/storyboards` | 列表，cursor 分页，按 `updated_at` 降序 |
| `GET` | `/storyboards/{run_id}` | 详情，含所有 steps（assets + shots + assembly） |
| `PATCH` | `/storyboards/{run_id}` | 更新项目元数据（script/style/model 等 metadata 字段） |
| `DELETE` | `/storyboards/{run_id}` | 软删除项目 |
| `POST` | `/storyboards/{run_id}/assets` | 新增资产（character/scene/prop） |
| `PATCH` | `/storyboards/{run_id}/assets/{step_id}` | 更新资产字段 |
| `POST` | `/storyboards/{run_id}/assets/{step_id}/generate` | 触发资产设定图生成（调图片生成接口） |
| `POST` | `/storyboards/{run_id}/assets/{step_id}/approve` | 批准资产 |
| `DELETE` | `/storyboards/{run_id}/assets/{step_id}` | 删除资产 |
| `POST` | `/storyboards/{run_id}/shots/rebuild` | 按脚本重新拆分 shots（清空旧 shots） |
| `POST` | `/storyboards/{run_id}/shots` | 手动添加一个 shot |
| `PATCH` | `/storyboards/{run_id}/shots/{step_id}` | 更新 shot 字段 |
| `POST` | `/storyboards/{run_id}/shots/{step_id}/approve` | 批准 shot |
| `POST` | `/storyboards/{run_id}/shots/{step_id}/keyframe` | 生成该 shot 的关键帧（调图片生成） |
| `POST` | `/storyboards/{run_id}/shots/{step_id}/keyframe/approve` | 批准关键帧 |
| `POST` | `/storyboards/{run_id}/shots/{step_id}/submit` | 提交该 shot 的视频生成任务 |
| `POST` | `/storyboards/{run_id}/shots/submit-all` | 批量提交全部已就绪 shots |
| `POST` | `/storyboards/{run_id}/shots/keyframes/generate-all` | 批量触发全部未生成关键帧（并发） |
| `POST` | `/storyboards/{run_id}/assemble` | 触发成片合成（所有 shots 都 done 后可调） |
| `DELETE` | `/storyboards/{run_id}/shots/{step_id}` | 删除 shot |
| `POST` | `/storyboards/{run_id}/shots/{step_id}/move` | 调整顺序（`{"direction": 1 \| -1}`） |

### 4.2 关键实现注意点

**`/shots/keyframes/generate-all` 改为并发**：后端同时派发多个图片生成请求（使用 `asyncio.gather`），前端 SSE 逐个收到结果。不再串行等待。

**成片合成 `/assemble`**：在 `assembly` step 记录 `segment_ids`（按 `index` 排序的 `video_generation_id` 列表），然后派发一个 worker job，让 worker 用 ffmpeg 按顺序拼接各段 mp4（所有片段已落库，直接按 `storage_key` 读取），输出新 `Video` 记录。这个任务通过 OutboxEvent `kind="storyboard_assembly"` 触发，与视频生成 worker 类似。拼接进度通过 SSE `channel: storyboard:{run_id}` 推送。

**AI 生成请求不走对话**：资产设定图和关键帧图通过 `messages.py` 的图片生成接口发送时，使用一个绑定到本 `run_id` 的专用隐藏对话（`workflow_type=storyboard`、`hidden_from_conversations=True`），与 apparel workflow 的 `_get_or_create_workflow_conversation` 方案完全一致，但在后端完成，不暴露给前端。前端只需 POST 到对应 step 端点即可。

### 4.3 核心输出类型

```python
class StoryboardAssetOut(BaseModel):
    id: str                         # step.id
    kind: str                       # character | scene | prop
    name: str
    role: str
    description: str
    continuity: str
    revision: int
    status: str                     # waiting_input | generating | ready | approved
    image_id: str | None
    image_url: str | None
    generation_id: str | None       # 当前生成任务 id（用于 SSE 进度）
    approved_at: str | None

class StoryboardShotOut(BaseModel):
    id: str
    index: int
    title: str
    purpose: str
    narration: str
    visual: str
    shot_type: str
    camera_move: str
    transition: str
    reference_notes: str
    duration_s: int
    asset_ids: list[str]
    keyframe_prompt: str
    keyframe_source_hash: str | None
    status: str                     # draft | approved | keyframe_ready | keyframe_approved | generating | done
    keyframe_image_id: str | None
    keyframe_image_url: str | None
    keyframe_approved_at: str | None
    video_generation_id: str | None

class StoryboardAssemblyOut(BaseModel):
    status: str                     # waiting | compositing | done | failed
    video_id: str | None
    video_url: str | None
    poster_url: str | None
    segment_count: int

class StoryboardRunOut(BaseModel):
    id: str
    title: str
    idea: str
    style: str
    script: str
    script_confirmed: bool
    script_revision: int
    aspect_ratio: str
    resolution: str
    model: str
    generate_audio: bool
    status: str
    assets: list[StoryboardAssetOut]
    shots: list[StoryboardShotOut]   # 已按 index 排序
    assembly: StoryboardAssemblyOut | None
    created_at: str
    updated_at: str
```

---

## 5. 前端设计

### 5.1 路由结构

```
/projectsProjectFunctionHub（已有，分镜制作入口指向 /projects/storyboard）

/projects/storyboard
  StoryboardIndexPage
  - 项目列表，卡片式排布，显示项目名/当前阶段/最后更新时间/缩略预览
  - 右上角"新建项目"按钮 → 弹 StoryboardCreateDialog

/projects/storyboard/[runId]
  StoryboardDetailPage
  - 独立页，包含完整六阶段工作区
  - 路由层加载项目数据，所有状态来自服务端（乐观更新 + 本地 pending 覆盖层）
```

**`/video` 页恢复纯单条生成器**：移除 `workspaceMode` 切换，移除所有 `Storyboard*` 组件，`VideoWorkspaceMode` 类型删除。页面大小从 7200 行降至约 3500 行。

### 5.2 StoryboardDetailPage 整体布局

```
┌─────────────────────────────────────────────────────┐
│ StageRail（左侧竖向步骤条，固定宽度 220px）               │
│  ┌── 想法 ✓                                          │
│  ├── 脚本 ✓                                          │
│  ├── 设定 ●（进行中）                                  │
│  ├── 分镜 ○                                          │
│  ├── 分镜图 ○                                         │
│  ├── 视频 ○                                          │
│  └── 成片 ○                                          │
│                                                     │
│ MainContent（右侧，按当前阶段渲染对应面板）               │
│                                                     │
└─────────────────────────────────────────────────────┘
```

左侧 StageRail 每一步显示：步骤名、状态图标（未开始/进行中/完成/警告）、关键数字（如"3/5 已批准"）。点击已完成或当前阶段可切换，未解锁的阶段点击时弹一条 toast 说明前置条件。

### 5.3 阶段前置条件与解锁逻辑

每个阶段的解锁条件在前端和后端各维护一份：

| 阶段 | 解锁条件 | 完成标志 |
|---|---|---|
| 想法 | 无 | `idea` 非空 |
| 脚本 | 想法非空 | `script_confirmed = true` |
| 设定 | 脚本已确认 | 所有 assets `status = approved` |
| 分镜 | 无强制前置（可并行） | 所有 shots `status >= approved` |
| 分镜图 | shots 存在 | 所有 shots `keyframe_approved_at` 非空且不过期 |
| 视频 | 所有关键帧已批准 | 所有 shots `status = done` |
| 成片 | 所有视频段完成 | `assembly.status = done` |

"分镜"阶段允许在设定完成前进入，因为用户可能想先拆镜再补设定。但提交视频时仍需关键帧中已绑定批准的设定图。

### 5.4 改进的批准交互

**每个"批准"动作旁边增加一句说明**，格式：`批准后这张图将作为 X 的参考，修改后需重新批准`。
具体：
- 设定图批准：`"批准后将作为每个绑定分镜段的关键帧生成参考"`
- 生成段批准：`"批准后才能生成该段的关键帧"`
- 关键帧批准：`"批准后才能提交该段视频生成，修改关键帧会使批准失效"`

**关键帧过期状态用橙色 banner 而非 badge**：当关键帧因设定图更新而过期时，在 Shot 卡片顶部显示一条橙色提示条："绑定的设定图已更新，关键帧需要重新生成"，附"立即重新生成"按钮。当前只有一个小 badge，用户很容易忽略。

### 5.5 分镜图阶段：并发生成 + 批量审批

分镜图阶段展示一个瀑布流卡片网格（每行 3-4 个）。每张卡片显示该 Shot 的关键帧状态。"批量生成所有未生成关键帧"按钮触发后端 `/shots/keyframes/generate-all`，后端并发派发所有生成任务，前端通过 SSE `storyboard:{run_id}` 频道逐卡收到进度更新，各卡片独立显示进度条，不阻塞用户操作其他卡片。

"批量批准所有关键帧"只批准当前没有过期警告的关键帧，过期的用橙色卡片单独标出。

### 5.6 视频阶段：队列视图

视频阶段采用横向进度列表（类似 CI Pipeline 视图）：

```
SEG 01  [关键帧图缩略] [██████████  生成中 62%] [取消]
SEG 02  [关键帧图缩略] [██████████████████ 完成] [预览] [重试]
SEG 03  [关键帧图缩略] [○ 等待提交             ] [提交]
SEG 04  [关键帧图缩略] [○ 等待提交             ] [提交]
[全部提交]  [合成成片]（当所有段完成时高亮）
```

右侧 aside 面板仅保留模型/分辨率/比例/音频/seed 参数设置，不再混入参考图上传（参考图上传移到设定阶段和分镜段的 binding 界面）。

### 5.7 成片阶段

触发成片后显示一个进度视图：

```
成片合成中
▐███████░░░░░░░░░░░▌ 3/8 段已拼接

完成后将出现：
[▶ 预览完整视频]  [⬇ 下载 mp4]  [分享]
```

成片完成后，项目卡片（列表页）展示该视频的 poster 和时长。

### 5.8 状态数据流

```
服务端（WorkflowRun/WorkflowStep）↓ GET /storyboards/{id} 全量加载
前端 useQuery（TanStack Query，staleTime: 30s）
    ↓
本地 optimistic overlay（乐观更新：patch/approve 操作先在本地修改显示，然后 PATCH 到后端）
    ↓
SSE channel: storyboard:{run_id}（接收异步任务进度：图片生成、视频生成、成片合成）
    ↓
收到 terminal 事件后 invalidateQueries 强制重新拉取确认态
```

不使用 `useState` 管理任何业务状态，全部通过 query cache 管理。乐观更新用 TanStack Query 的 `onMutate/onSettled` 模式。

---

## 6. 成片合成后端实现

### 6.1 Worker Job

新增 OutboxEvent `kind = "storyboard_assembly"`，worker 新增 `tasks/storyboard_assembly.py`：

```python
async def run_storyboard_assembly(ctx, run_id: str):
    # 1. 加载 WorkflowRun + shot steps，按 index 排序
    # 2. 验证所有 shot.status == "done"，否则标记 assembly failed
    # 3. 按 segment_ids 顺序读取各段 Video.storage_key
    # 4. 用 ffmpeg concat demuxer 拼接：
    #    - 生成 concat list 文件（file 'path1.mp4'\nfile 'path2.mp4'）
    #    - ffmpeg -f concat -safe 0 -i list.txt -c copy output.mp4
    # 5. 生成 poster（取第一帧）
    # 6. 存储到 LocalStorage，写 Video 行（owner_generation_id = null）
    # 7. 更新 assembly step: status=done, output_json.video_id = video.id
    # 8. SSE publish storyboard.assembled
```

`-c copy` 不重编码，只做 container 拼接，速度快，文件质量无损。前提是各段 mp4 编码参数一致（同分辨率/帧率/编码格式）——Seedance 输出格式统一，这个前提成立。

### 6.2 SSE 事件

新增以下事件（复用已有 SSE 基础设施，只需在 `events.py` 的 ownership 校验加 `WorkflowRun` 支持）：

```
storyboard.asset_generating     # 资产图生成开始
storyboard.asset_ready          # 资产图生成完成
storyboard.keyframe_generating  # 关键帧生成开始
storyboard.keyframe_ready       # 关键帧生成完成
storyboard.shot_submitted       # 视频段提交
storyboard.shot_done            # 视频段完成
storyboard.assembling           # 成片合成开始
storyboard.assembled            # 成片完成
storyboard.assembly_failed      # 成片失败
```

---

## 7. 实施顺序

### 第一期（本次执行目标）

**阶段 1：后端 API 脚手架**（1-2 天）
- 新建 `apps/api/app/routes/storyboards.py`，实现项目的 CRUD（create/list/get/patch/delete）
- `STORYBOARD_WORKFLOW_TYPE = "storyboard"` 注册到 `main.py`
- 实现 `StoryboardRunOut` schema，含 assets/shots 解析逻辑
- events.py 的 ownership 校验增加 `WorkflowRun(type="storyboard")` 分支
- 写单测：创建/读取/删除项目，确认 `workflow_type` 过滤

**阶段 2：资产和分镜的持久化 API**（2-3 天）
- 实现 assets CRUD（add/patch/generate/approve/delete）
- 实现 shots CRUD（rebuild/add/patch/approve/move/delete）
- 实现 keyframe 生成和批准端点
- 实现 `/shots/keyframes/generate-all`（并发 asyncio.gather）
- 实现 `shots/{id}/submit` 和 `shots/submit-all`
- 写单测：资产 approval 流、shot 排序、keyframe stale 检测

**阶段 3：成片合成**（1 天）
- 实现 `/storyboards/{id}/assemble` 端点 + OutboxEvent
- 新建 `tasks/storyboard_assembly.py`（ffmpeg concat）
- 写 worker 单测（mock ffmpeg）

**阶段 4：前端路由和列表页**（1 天）
- 新建 `/projects/storyboard/page.tsx`（列表页）
- 新建 `/projects/storyboard/[runId]/page.tsx`（详情页骨架）
- 实现 `createStoryboard/listStoryboards/getStoryboard` apiClient 函数
- 更新 `ProjectFunctionHub.tsx` 的分镜制作卡片 `primaryHref`

**阶段 5：前端工作区核心**（3-4 天）
- 实现 StageRail 组件（竖向步骤条，含状态、前置条件门控）
- 实现各阶段面板（Idea/Script/Assets/Shots/Keyframes/Videos/Assembly）
- 实现乐观更新模式（TanStack Query mutate + optimistic overlay）
- SSE 订阅 `storyboard:{run_id}` 更新对应 step 状态

**阶段 6：清理 `/video` 页**（半天）
- 移除 `VideoWorkspaceMode`、所有 `Storyboard*` 组件和相关 state
- 确保 `npm run type-check && npm run build` 通过

### 第二期（非本次执行范围，留作规划）

- 多项目并发进度监控（首页 dashboard）
- 成片版本管理（每次 assemble 保留历史）
- 分镜段顺序拖拽排序（替代上下箭头）
- 分镜段导入（从已有图片/视频直接挂载）

---

## 8. 关键风险与应对

| 风险 | 应对 |
|---|---|
| WorkflowStep JSON 字段查询慢（大项目 60 个 shots） | 按 `workflow_run_id + step_key LIKE 'shot:%'` 加 `ix_workflow_steps_run_key` 索引，应用层内存排序，不走 JSON 列查询 |
| ffmpeg concat 输出格式不一致（帧率/分辨率不同段） | 提交视频时强制所有段使用相同 `resolution/aspect_ratio`；合成前校验，不一致时返回明确错误而不是生成损坏文件 |
| 并发关键帧生成打满图片生成并发上限 | `generate-all` 分批并发（每批 4 个），用 `asyncio.gather` + semaphore，不超过系统并发上限 |
| 乐观更新与 SSE 事件竞争（两个更新源） | 乐观更新 `onSettled` 始终 invalidate query，SSE 事件只 patch 特定 step 字段，不覆盖全量，冲突时以下一次 invalidate 后的服务端状态为准 |
| 旧版 `/video` 故事板项目迁移 | 不迁移。旧版状态在 localStorage 里本就无持久化，用户刷新就丢；新版独立路由，旧用户会自然切换到新入口 |
| `VIDEO_BUG_AUDIT` 中的 P0 计费问题（deadline 退款、成功免单） | 故事板成片的每个 shot 仍走 `POST /videos/generations`，沿用已有视频计费路径，与本文档无关；但建议与故事板同批次修复 P0-1 和 P0-2（在 `video_billing.py` 里成功必收费） |

---

## 9. 验收标准

```bash
# 后端
pytest tests/ -q -k "storyboard"   # 所有故事板单测通过
ruff check apps/api/app/routes/storyboards.py
mypy apps/api/app/routes/storyboards.py

# 前端
cd apps/web
npm run type-check
npm run lint
npm run build

# 功能
# 1. 新建项目 → 刷新页面 → 项目状态完整恢复
# 2. 脚本 → 拆镜 → 批量关键帧（并发）→ 批量提交视频 → 触发成片
# 3. 成片 mp4 可在浏览器 seek，时长 = 各段之和
# 4. /video 页不含任何 Storyboard 组件，type-check 通过
# 5. /projects 分镜制作卡片直接进入 /projects/storyboard 列表
```

---

这份文档覆盖了从数据模型到前端交互的完整重设计思路。现在最关键的问题是：开始执行前，需要确认 `apps/worker/app/tasks/video_generation.py` 中的 P0-1（deadline 退款）和 P0-2（成功免单）两个计费 bug——它们影响故事板的每一段视频。是否把这两个计费修复纳入本次故事板第一期？
