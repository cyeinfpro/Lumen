# Lumen Frontend Theme & Dialog Standards

This document is the implementation standard for new frontend features. It turns the May 2026 theme cleanup into reusable rules so new UI does not regress into hard-coded dark surfaces.

Source of truth:

- Theme tokens and utilities: `apps/web/src/app/globals.css`
- Current Web design system: `apps/web/DESIGN.md`
- Long-term memory rule: `MEMORY.md`, "Lumen UI Theme and Dialog Standards"

## 1. Token-First Rule

Use semantic CSS variables for every normal interface surface.

| Purpose | Use |
|---|---|
| Page background | `bg-[var(--bg-0)]` |
| Panels, cards, dialog bodies | `bg-[var(--bg-1)]` or `bg-[var(--bg-1)]/90` |
| Nested surfaces | `bg-[var(--bg-2)]` with `border-[var(--border)]` |
| Primary text | `text-[var(--fg-0)]` |
| Secondary text | `text-[var(--fg-1)]` |
| Tertiary/help text | `text-[var(--fg-2)]` |
| Borders | `border-[var(--border)]`, `border-[var(--border-subtle)]`, `border-[var(--border-strong)]` |
| Elevation | `shadow-[var(--shadow-2)]`, `shadow-[var(--shadow-3)]`, `shadow-lumen-card`, `shadow-lumen-pop` |
| Brand action | `bg-[var(--accent)] text-[var(--accent-on)]` |
| Danger action | `bg-[var(--danger)] text-[var(--danger-on)]` |

Do not create a new local palette unless the feature introduces a real semantic state that the design system does not cover.

## 2. Hard-Coded Dark Class Policy

Do not use these in normal UI:

- `bg-neutral-900`, `bg-neutral-950`, or opacity variants
- `bg-black/*`
- `text-white`, `hover:text-white`
- `text-neutral-100/200` as a substitute for primary text
- `border-white/*` as a substitute for semantic borders
- hard-coded dark gradients such as `#0b0b0d`

Allowed exceptions:

- Image, video, canvas, and lightbox surfaces where the content needs a dark stage.
- Small controls drawn on top of image thumbnails or lightbox media.
- Backdrop/scrim layers behind modals should use `var(--surface-scrim)`.
- Code blocks and terminal/log surfaces when a dark code theme is intentional.
- Destructive buttons and status badges on strongly colored backgrounds.
- White icons on success/danger/accent filled badges when contrast requires it.

If a new exception is needed, add a short code comment or document it in the PR description. The exception should describe the content background, not just say "looks better dark".

## 3. Dialog, Popover, Toast, Tooltip

All non-media floating UI follows the theme.

Panel shell:

```tsx
className="rounded-[var(--radius-dialog)] border border-[var(--border)] bg-[var(--bg-1)]/95 text-[var(--fg-0)] shadow-[var(--shadow-3)] backdrop-blur-xl"
```

Header/footer dividers:

```tsx
className="border-b border-[var(--border)]"
className="border-t border-[var(--border)] bg-[var(--bg-1)]/72"
```

Close/secondary buttons:

```tsx
className="text-[var(--fg-1)] hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)]"
```

Inputs inside dialogs:

```tsx
className="border border-[var(--border)] bg-[var(--bg-0)]/70 text-[var(--fg-0)] placeholder:text-[var(--fg-2)]"
```

Never ship a normal dialog with `bg-neutral-900/95`, `bg-neutral-950/95`, `text-white`, or `border-white/10`.

## 4. Mobile Dialog and Bottom Sheet Standard

Use the shared mobile safe-area utilities. Do not recalculate viewport height locally unless the shared utilities cannot express the layout.

Required structure:

```tsx
<div className="fixed inset-0 mobile-dialog-shell ...">
  <section className="mobile-dialog-panel ...">
    <div className="mobile-dialog-scroll ...">...</div>
    <footer className="mobile-dialog-footer ...">...</footer>
  </section>
</div>
```

For sheets:

```tsx
<section className="mobile-dialog-sheet ...">
```

Rules:

- Use `mobile-dialog-shell` on the fixed wrapper that needs safe-area spacing.
- Use `mobile-dialog-panel` for centered dialogs and `mobile-dialog-sheet` for bottom sheets.
- Put scrollable content in `mobile-dialog-scroll`.
- Put sticky action rows in `mobile-dialog-footer`.
- Keep footer backgrounds semantic: `bg-[var(--bg-1)]/72` or `bg-[var(--bg-0)]`.
- Scrims may remain dark: `bg-black/60`, `backdrop:bg-black/40`, or equivalent.

## 5. Page Shells and Public Pages

Page roots should start from:

```tsx
className="min-h-[100dvh] w-full flex-1 bg-[var(--bg-0)] text-[var(--fg-0)]"
```

Public share, login, invite, reset password, settings, and admin pages must not use hard-coded dark gradients or root-level `text-neutral-200`. If a page needs visual depth, use variable gradients:

```tsx
bg-[linear-gradient(180deg,var(--bg-0)_0%,var(--bg-1)_52%,var(--bg-0)_100%)]
```

## 6. Legacy Utility Migration

`globals.css` does not remap legacy dark utilities for the light theme. Older
components using `text-neutral-*`, `border-white/*`, `bg-white/*`, or dark
neutral panel classes must be migrated directly to semantic variables.

If you touch an old component, convert the nearby surface, text, and border
classes instead of adding global compatibility overrides.

Do not globally remap `bg-black/*`: it is also used for real scrims and media overlays. Convert ordinary panels and forms directly.

## 7. Pre-Delivery Audit

Before finishing any frontend feature with UI changes:

```bash
rg -n "bg-neutral-9|bg-neutral-950|bg-black/(20|30|35|40)|text-white|hover:text-white|text-neutral-100|text-neutral-200|border-white/10|#0b0b0d" apps/web/src/app apps/web/src/components
```

Classify remaining hits:

- OK: media/lightbox/image overlay
- OK: scrim/backdrop
- OK: code/terminal/log surface
- OK: destructive button or filled status badge
- Fix: normal page, dialog, popover, toast, tooltip, form, card, table, sidebar, settings/admin/share UI

Then run:

```bash
git diff --check
cd apps/web
npm run type-check
npm run lint
npm run build
```

For small documentation-only changes, `git diff --check` is enough.
