# Lumen 审计索引

审计报告是特定提交或工作树快照的历史证据，不是长期有效的缺陷清单。维护者在引用
任何条目前，必须先按报告记录的 baseline 在当前代码中重新验证。

版本化归档报告使用 YAML front matter 暴露 `baseline_commit`、`status`、
`resolved_by` 和 `superseded_by`，供脚本与文档索引读取。`archived` 只表示历史
快照且必须重新验证，不得推断为已修复；只有 `resolved` 才能填写 `resolved_by`。

## 当前整改基线

- 报告日期：2026-07-09
- 发布基线：`v1.2.44`
- 提交：`cc5f53b6e3587051ccb41260244f57ba11c8cc49`
- 状态：当前整改输入；修复尚未通过正式发布流程

## 已归档报告

| 报告 | 审计基线 | 状态 |
|---|---|---|
| `local/ISSUES.md` | `v1.0.43` / `aef4b05` | 本机归档，未纳入版本控制 |
| [BUG_REVIEW](archive/BUG_REVIEW.md) | 2026-05-16 未提交工作树 | 历史，需重新验证 |
| [DEEP_BUG_AUDIT](archive/DEEP_BUG_AUDIT.md) | 2026-05-25 工作树 | 历史，需重新验证 |
| [Server audit 2026-05-29](archive/SERVER_BUG_AUDIT_2026-05-29.md) | `v1.1.71` / `b6e4004` | 历史，需重新验证 |
| [Code review 2026-05-30](archive/CODE_REVIEW_2026-05-30.md) | `b6e4004` 后续工作树 | 历史，需重新验证 |
| [Video generation review](archive/video-generation-bug-audit.md) | 2026-06-04 未提交工作树 | 已修复记录 |
| [Video audit 2026-06-10](archive/VIDEO_BUG_AUDIT_2026-06-10.md) | 2026-06-10 工作树 | 历史，需重新验证 |
| [Uncommitted review 2026-06-29](archive/CODE_REVIEW_未提交改动.md) | 2026-06-29 未提交工作树 | 历史，需重新验证 |
| `local/CODE_AUDIT_2026-07-02.md` | 2026-07-02 工作树 | 本机归档，未纳入版本控制 |
| `local/bug_review_report.md` | 2026-07-02 未提交工作树 | 本机归档，未纳入版本控制 |

`local/` 中的生成型审计可能包含敏感实现细节，继续受 `.gitignore` 保护。可版本化的
历史报告统一放在 `archive/`，且每份报告顶部都标明归档状态。
