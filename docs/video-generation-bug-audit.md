# 视频生成功能 Bug 审查与修复记录

- **审查范围**: 本次未提交的视频生成功能改动(API / worker / core / web)
- **审查日期**: 2026-06-04
- **当前状态**: 审查列出的 6 项问题已修复,并补了回归测试

## 摘要

| # | 严重度 | 状态 | 问题 | 修复 |
|---|--------|------|------|------|
| 1 | 🔴 严重 | 已修复 | 提交中途取消 → 上游成本泄漏 + 孤儿任务 | API 只在 `QUEUED` 且未提交上游时立即取消; worker 先查 submit 缓存,再决定是否 pre-submit cancel; lease 在早退点释放 |
| 2 | 🟠 中 | 已修复 | 提交成功后持久化失败 → 上游重复提交、重复扣费 | submit 成功后先写 Redis 缓存; 后续重试复用缓存的 `provider_task_id`,不再重提上游 |
| 3 | 🟠 中 | 已修复 | 早返回路径泄漏 lease,造成最长 120s 停滞 | `run_video_generation` / `run_video_poll` 的早退点都显式释放 lease |
| 4 | 🟡 中 | 已修复 | 历史列表 N+1 查询 | 列表页一次性批量加载 `Video` 再拼装输出 |
| 5 | 🟡 低 | 已修复 | `_request_fingerprint` 重复计算两次 | 创建时先算一次,复用到诊断和持久化字段 |
| 6 | 🟡 低 | 已修复 | options 暴露时长×分辨率笛卡尔积,但部分组合无预估价 | 前端提交按钮现在要求有效 estimate; 无预扣组合不能提交 |

## 修复说明

### #1 提交中途取消
已把“是否可以立即取消”的判断收紧到真正还在队列里的任务,避免 `SUBMITTING` 窗口被当成未提交。worker 侧也先看 submit 缓存,再决定是否走 pre-submit cancel,这样不会把已经发出去的上游任务误回收。

### #2 提交后持久化失败
上游返回后会先把 `provider_task_id` 和原始响应写到 Redis 缓存,再做数据库持久化。若 DB 这步失败,后续 worker 重跑会直接复用缓存结果,不会再次调用上游 submit。

### #3 lease 泄漏
`run_video_generation` 和 `run_video_poll` 的所有早退路径都补了 lease 释放,不再依赖 TTL 兜底。

### #4 历史列表 N+1
列表接口现在会先批量取回当前页所有视频,再在内存里按 `owner_generation_id` 组装返回值,避免逐行额外查一次。

### #5 fingerprint 重算
创建视频任务时只计算一次 `request_fingerprint`,同一个值同时写入 `diagnostics` 和持久化字段。

### #6 组合预估价
前端提交按钮现在会检查当前组合是否有有效预估价。没有 estimate 的组合不会进入提交流程,避免用户点进去才收到后端 503。

## 回归验证

- `uv run pytest apps/worker/tests/test_bug_audit_worker_regressions.py apps/api/tests/test_videos_media.py`
- `npm run type-check` in `apps/web`
- `npm run lint` in `apps/web`
- `npm run build` in `apps/web`
