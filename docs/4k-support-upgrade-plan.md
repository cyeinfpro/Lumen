# 4K Output Upgrade Plan

## 背景

项目当前仍以约 `1.57M` 像素预算作为尺寸规划前提，这个假设来自此前对 `api.example.com` 网关的早期测试与集成文档：

- `packages/core/lumen_core/constants.py`
- `packages/core/lumen_core/sizing.py`
- `apps/web/src/lib/sizing.ts`
- `apps/worker/app/config.py`
- `apps/api/app/config.py`
- `docs/DESIGN.md`
- `image-gateway-test-summary.md`
- `responses-image-integration-guide.md`

其中最核心的限制是：

- `PIXEL_BUDGET = 1_572_864`
- 前后端尺寸解析器只会优先给出不超过该预算的预设尺寸
- 用户即使输入更大的固定尺寸，也会被自动回退到旧预算内的尺寸

这意味着项目当前不是“上游一定不能 4K”，而是“项目内部仍按旧预算主动收缩请求”。

## 现状确认

2026-04-23 已对目标网关做了直接实测：

- Base URL: `https://api.example.com`
- Endpoint: `POST /v1/images/generations`
- Model: `gpt-image-2`
- Requested size: `3840x2160`
- Result: 成功返回 `200`
- Returned image size: `3840x2160`

本地验证产物：

- 测试图片: `flux-4k-test.png`（已不再随仓库提供）

结论：

- 该网关当前可以通过 direct image endpoint 成功生成 4K 横图
- 旧的 `1.57M` 预算结论已经不适合作为项目的全局硬限制

## 关键判断

这个项目的图片生成实现已经是 direct image path，不是通过 `/v1/responses` 做图片工具调用。

当前实现：

- 文生图：`apps/worker/app/upstream.py` -> `POST /v1/images/generations`
- 图生图：`apps/worker/app/upstream.py` -> `POST /v1/images/edits`
- 默认模型：`gpt-image-2`

因此，本次升级的核心不是“把生成链路从 responses 改到 images”，而是：

1. 去掉项目内部基于旧测试结论设置的 `1.57M` 硬预算
2. 允许显式固定尺寸通过到上游
3. 为 4K 提供明确、可验证的尺寸预设和校验逻辑

## 升级目标

目标分为两层：

### 1. 默认策略

普通请求仍可保持保守默认，避免所有用户默认都请求大图造成延迟和成本上升。

建议：

- 普通默认尺寸继续使用当前稳定中等尺寸策略
- 仅在用户显式要求大图或选择 4K 预设时，才发送 4K 固定尺寸

### 2. 显式 4K 支持

当用户明确要求 4K 时，项目应允许以下尺寸直接透传到上游：

- 横图：`3840x2160`
- 竖图：`2160x3840`
- 方图如需支持：`2048x2048` 可先作为过渡；是否开放更大方图另行评估

## 推荐设计

### A. 拆分“默认预算”与“最大允许尺寸”

当前问题在于系统只维护了一个 `pixel budget` 概念，并把它同时用于：

- 默认尺寸推导
- 固定尺寸合法性判断
- UI 预设回退

建议拆成两个概念：

- `default_pixel_budget`
  - 用于普通请求的默认尺寸推导
  - 可以继续保守
- `max_explicit_size_policy`
  - 用于显式固定尺寸校验
  - 应按当前上游真实能力定义，而不是沿用旧预算

### B. 固定尺寸校验改为基于官方约束

显式尺寸不应再只看 `w * h <= 1_572_864`。

建议改为以下约束：

- 最长边 `<= 3840`
- 宽高都必须是 `16` 的倍数
- 总像素在 `655,360` 到 `8,294,400` 之间
- 宽高比不超过 `3:1`

然后增加 4K 预设：

- `3840x2160`
- `2160x3840`

### C. 仅对“显式 fixed size”放宽，不改变普通 auto 行为

为了降低回归风险：

- `size_mode = auto` 时，仍沿用当前默认尺寸规划
- `size_mode = fixed` 且尺寸满足新约束时，直接透传给上游
- 只有 fixed size 非法时才回退或报错

### D. 对 UI 明确暴露 4K 预设

当前前端会基于旧预算给出预设或自动回退。

建议前端增加：

- `4K Landscape (3840x2160)`
- `4K Portrait (2160x3840)`

并在说明文案中区分：

- 默认尺寸：速度更快
- 4K：更慢、更耗成本，但输出更大

## 需要修改的文件

### 核心尺寸逻辑

- `packages/core/lumen_core/constants.py`
- `packages/core/lumen_core/sizing.py`
- `apps/web/src/lib/sizing.ts`

### 配置与运行时设置

- `apps/worker/app/config.py`
- `apps/api/app/config.py`
- `packages/core/lumen_core/runtime_settings.py`
- `apps/web/src/app/admin/_panels/SettingsPanel.tsx`

### 请求入口与交互层

- `packages/core/lumen_core/schemas.py`
- `apps/api/app/routes/messages.py`
- `apps/web/src/components/ui/AspectRatioPicker.tsx`
- `apps/web/src/store/useChatStore.ts`
- `apps/web/src/lib/types.ts`

### 文档

- `docs/DESIGN.md`
- `README.md`
- 旧的上游网关集成说明文档中关于 `1.57M` 的结论

## 实施建议

建议按两步做：

### Phase 1: 最小可用升级

- 保留当前默认尺寸策略
- 新增显式 4K fixed size 通道
- 更新前后端校验逻辑
- 增加 4K 预设
- 用真实接口做回归测试

### Phase 2: 默认策略重估

- 评估是否需要抬高默认预算
- 评估 2K / 4K 在不同场景的延迟、失败率、成本
- 再决定是否调整自动推荐尺寸

## 验证标准

升级完成后，至少验证以下场景：

1. 文生图 `3840x2160` 返回成功，实际尺寸为 `3840x2160`
2. 文生图 `2160x3840` 返回成功，实际尺寸为 `2160x3840`
3. 普通 `auto` 请求仍保持当前稳定行为
4. 非法 fixed size 会被明确拒绝，而不是静默改成别的尺寸
5. 聊天记录 / lightbox / 历史任务中显示的 `size_requested` 与 `size_actual` 正确

## 风险

- 4K 任务耗时更长，可能触发现有超时边界
- 单图字节体积更大，存储、预览图生成、下载链路都要复查
- 旧文档和管理员面板中仍存在 `pixel_budget` 话术，容易误导后续维护者
- 如果网关能力再次变化，单纯依赖静态阈值仍可能失真，因此应保留实际返回尺寸校验

## 结论

这次升级的正确方向不是继续围绕 `1.57M` 做折中，而是：

- 承认 `1.57M` 是旧的项目内假设
- 基于已验证成功的 direct `gpt-image-2` 4K 能力更新尺寸策略
- 让“默认保守”和“显式 4K”两条策略并存

这样改动最小，也最符合当前项目真实链路。
