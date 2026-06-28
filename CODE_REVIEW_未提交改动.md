# 未提交改动审查报告

审查范围：当前工作区全部未提交修改与新增文件。
日期：2026-06-29

## 改动概览

本批改动主要包含：

1. BYOK 严格模式与数据保留：废弃管理员 provider 兜底；新增 BYOK 数据隐藏与可选软删除策略，并在会话、消息、图片、分享、生成历史和 worker 历史打包路径接入可见性过滤。
2. 管理后台用户操作：新增用户历史查看、管理员改密、软删除用户，并同步撤销会话和取消活跃任务。
3. 导航入口可见性：新增创作、视频、项目、资产入口开关；前端导航、命令面板、预取、SSR cookie 和 API guard 同步。
4. 生图画幅：新增 10:7 / 7:10，并将默认生图参数调整为 7:10。

## 已修复问题

### 1. 根目录 pytest 默认命令收集失败

`uv run pytest -q` 会因为 `packages/core/tests/test_video_billing.py` 和 `apps/worker/tests/test_video_billing.py` 同名，在默认导入模式下触发 import mismatch，导致发布前普通测试命令失败。

修复：在 `pyproject.toml` 中固定 `--import-mode=importlib`，使默认测试命令和完整复验一致。

### 2. BYOK 删除窗口可被非后台入口配置得早于隐藏窗口

后台 PATCH 已校验 `delete_days >= hide_days`，但环境变量或直接构造 `ByokRetentionPolicy` 可绕过，导致开启自动删除时数据在仍应可见的窗口内被软删除。

修复：在 `ByokRetentionPolicy.normalized()` 中下沉保护；当隐藏和删除都开启时，删除窗口不会小于隐藏窗口。新增 core 单测覆盖该绕过场景。

## 复核结论

- BYOK 自动软删除默认关闭，仅隐藏默认开启；破坏性删除需要管理员显式打开。
- 隐藏导航入口不再只是前端隐藏；对应主 API 前缀已有 `_NavFeatureGuardMiddleware` 返回 404。
- `since=<message_id>` 的锚点解析没有套用 retention filter，只对返回列表过滤，避免旧锚点造成 422。
- 公共分享的 BYOK 可见性判断已批量读取用户模式，未保留旧版逐图 N+1 查询。
- 多图分享、公开变体读取、我的分享列表均会过滤过期 BYOK 图片。

## 验证

- `git diff --check`
- `uv run ruff check`
- `uv run pytest -q`
- `npm run type-check`
- `npm run lint`
- `npm run test`
- `npm run build`

备注：`npm run lint` 仍报告既有 storyboard `<img>` 警告，未产生 lint error 或新增 UI governance finding。
