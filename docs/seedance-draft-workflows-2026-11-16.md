# Seedance 草稿与成片流程

## 两种模式

- 官方草稿模式：仅官方 native volcano 渠道的标准 Seedance 2.5，480p 草稿成功后手动生成 1080p 成片。提示词、原素材、时长、比例、种子和音频由上游复用，不重传。创建与转正分别计费。
- 参考草稿模式：普通视频生成加工作流标记，可选择 Flash 等模型。转成片时保留可编辑提示词和原素材、追加生成视频，走新的视频参考生成任务，不是官方转正，也不保证画面一致。

## 数据与安全

工作流字段保存在 VideoGeneration.upstream_request 并通过响应显式输出，不回填历史任务。官方转正只接受本系统任务 ID，服务端检查当前用户归属、成功状态、原模型/渠道和有效期，再取得原生 task ID。worker 再校验原端点与路由指纹，不能换渠道提交。转正不重新加载旧媒体，但保留原素材快照用于计费。参考成片要求附加源任务实际生成、未删除的视频。新任务仍沿用既有幂等键和钱包/Outbox 原子事务。

2026-10-28 10:00 UTC+08 起草稿有效期为14天，之前7天，优先使用上游 created_at，缺失时用最早可信本地创建或提交时间。官方转正固定1080p，普通标准2.5支持480/720/1080，Flash仅480/720且不支持官方草稿。

## Flash

官方模型 ID doubao-seedance-2-5-flash-261115；4–30秒或自动时长，10图/5视频/5音频。独立价格key，不套标准2.5价格。0073迁移补充模型映射、独立价格和保守预留估算，保留运营者定制配置，尚未执行生产迁移。

## 官方依据

- https://docs.volcengine.com/docs/ark/seedance-2-5?lang=zh
- https://docs.volcengine.com/docs/ark/create-video-generation-task-api?lang=zh
- https://console.volcengine.com/ark/region:cn-beijing/docs/model-pricing

## 验证注意

当前虚拟环境 editable core 指向另一旧工作目录，测试必须显式设置 PYTHONPATH 为当前 packages/core 与当前 apps/api 或 apps/worker；API与worker必须分进程运行。测试使用 mock/内存迁移，没有付费生成或生产发布。真实上游出片一致性未经付费实测。

最终验收：API草稿/选路27项、既有API回归179项、worker104项、Flash核心94项与内存迁移3项通过；前端TypeScript、范围ESLint、5组草稿流程测试及精确路径diff检查通过。前端新增复杂度已拆分并通过≤15校验。后续发布前本地生产 build 已完整通过（编译、TypeScript、36页生成与build traces，exit 0）。未进行浏览器端付费真实生成。
