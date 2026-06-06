# Video Page UX Follow-Up

## Goal

Keep `/video` consistent with the rest of Lumen's workspace pages. The page
should feel like a production tool: clear entry points, short labels, stable
media preview, visible cost context, and predictable task feedback.

This note applies to `apps/web/src/app/video/page.tsx`.

## Direction

- Use the same page rhythm as project and settings pages: thin desktop header,
  compact mobile header, bordered tool sections, and restrained copy.
- Keep normal UI on semantic Lumen tokens from `docs/DESIGN.md` section 15 and
  `docs/frontend-theme-dialog-standards.md`.
- Use media surfaces only where they frame video content. Avoid hard-coded dark
  surfaces for forms, cards, dialogs, and page chrome.
- Prefer short Chinese labels over explanatory slogans. Avoid marketing terms
  and role-play metaphors in visible product copy.
- Keep layout dimensions stable across empty, loading, active, failed, and
  completed states.
- Use motion only for state changes that benefit from it, such as progress
  width changes.

## Product Shape

- Header: `Video / 视频`, enable state, active count, completed count.
- Left column: new video form with mode, description, optional media input,
  parameters, cost estimate, and submit state.
- Right column: preview, active tasks, recent completed videos, and history.
- Mode switch: three compact entries for text, first frame, and reference media.
- Reuse action: completed or historical tasks can hydrate the form through
  `套用参数`.

## Copy Rules

- Use `描述` for visible labels and toasts.
- Use `参数` for the parameter section.
- Use `任务`, `最近`, `历史`, and `预览` for section titles.
- Use `套用参数` for the reuse action.
- Disabled submit reasons stay short:
  - `正在读取配置`
  - `视频生成未启用`
  - `没有可用模型`
  - `先填写描述`
  - `需要上传首帧或填写图片 ID`
  - `先添加参考素材`
  - `缺少预扣估算`
- Stage details stay short:
  - queued: `等待开始。`
  - submitting: `正在提交。`
  - submitted: `等待处理。`
  - rendering/running: `正在生成。`
  - fetching: `正在取回文件。`
  - storing: `正在保存。`
  - billing: `正在结算。`
  - succeeded/finished: `已保存。`
  - failed: `失败，可重试。`
  - canceled: `已取消。`
  - expired: `已过期。`

## State Matrix

| State | Required behavior |
|---|---|
| Enabled | Form is usable and shows available model count for the selected mode. |
| Disabled | Form shell remains visible; submit state explains why it cannot run. |
| Loading options | Submit state says configuration is loading; layout does not jump. |
| Missing description | Submit is disabled with `先填写描述`. |
| Missing first frame | First-frame mode asks for upload or image ID. |
| Missing reference media | Reference mode asks for at least one media item. |
| Missing estimate | Submit is disabled with `缺少预扣估算`. |
| Empty history | Preview, tasks, recent, and history show compact empty states. |
| Active task | Row shows stage label, short detail, smooth progress, and actions. |
| Failed task | Row keeps retry/copy/reuse available without taking over the page. |
| Completed video | Latest video appears in preview and can be reused. |
| Mobile | Sticky submit panel stays above the tab bar and does not cover content. |

## Verification

From the repository root:

```bash
git diff --check
rg -n "bg-neutral-9|bg-neutral-950|bg-black/(20|30|35|40)|text-white|hover:text-white|text-neutral-100|text-neutral-200|border-white/10|#0b0b0d" apps/web/src/app apps/web/src/components
```

From `apps/web`:

```bash
npm run type-check
npm run lint
npm run build
```

Then render `/video` at desktop and mobile widths. If backend services are not
available, verify the disabled or empty states and record that limitation.
