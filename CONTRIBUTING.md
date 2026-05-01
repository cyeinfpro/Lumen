# Contributing to Lumen

欢迎参与 Lumen 的开发。本文档约定项目通用协作规则与上游 API 调用红线。

## 通用约定

- **代码组织**：所有改动需明确归属到 `apps/api`、`apps/worker`、`apps/web`、`packages/core` 之一；跨模块改动需在 PR 描述说明影响面。
- **依赖管理**：Python 用 `uv`（根目录 `pyproject.toml` + `uv.lock`），前端用各自 `package.json`。不要手动改 `uv.lock`。
- **测试**：与生成路径、上游调用、计费相关的改动必须带回归测试；UI 改动至少在移动端 Safari + 桌面 Chrome 上人工验证。统一入口 `bash scripts/test.sh`，会按 worker / api / core 三个子进程分别跑（同进程合跑会因 `apps/api/app` 与 `apps/worker/app` 同名 package 引发 module cache 与 PIL/Prometheus 全局状态污染）。
- **提交信息**：遵循现有 commit message 风格（动词起首，描述变化与原因），不要塞 emoji。
- **生产数据**：不擅自回填或改动历史数据；任何 migration 必须 dry-run 后再上。
- **环境变量**：`apps/web` 的 Next.js build 只读 `apps/web/.env*`，不要把根 `.env` 当成 single source of truth。
- **部署**：rsync 必须按 `deploy/` 下示例排除整个 `apps/worker/var/` 目录，不要只靠后缀 filter。

## 上游调用约定

**核心红线**（修改任何与上游 `/v1/responses` 相关的代码前必读）：
- 不要在 `apps/worker/app/upstream.py` 之外直接 httpx 调用上游
- 不要在 instructions / tools 数组里加抖动字段（破坏 prompt cache）
- 不要尝试 `previous_response_id` 或 `store: true`（上游 HTTP 不支持）
- 不要改动 `deploy/nginx.conf.example` 的 buffering / timeout / body size 配置

**新增上游调用时**：
- 走 `apps/worker/app/upstream.py` 现有 public function
- 走 `apps/api/app/metrics_upstream.py` 的 4 个 record helper 上报
- 错误用 `packages/core/lumen_core/constants.py:GenerationErrorCode` 分类
