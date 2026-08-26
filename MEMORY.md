# MEMORY.md - Long-Term Memory

## Lumen Release Workflow
- For this repository, whenever the user asks to submit, push, deploy, or update code, treat it as a release update unless they explicitly say "main only" or "no version bump".
- Do not only push `main` for user-facing updates. Bump the product version, run `python3 scripts/version.py sync`, verify `python3 scripts/version.py check`, commit the version bump with the code change, push `main`, create and push the matching `vX.Y.Z` git tag, then wait for GitHub Actions `Docker Release` to complete successfully.
- The default update script channel is `stable`, which resolves GitHub latest release. Therefore production/default updates will keep using the previous release, such as `v1.0.10`, until a new release tag such as `v1.0.11` is published.
- Main-branch Docker images are useful for testing and carry `main`/`sha-<short-sha>` tags. They do not update `latest`; `latest` is only updated by a successful formal `v*` release tag.

## Apparel Model Library Update Workflow
- In this repo, when the user says "更新模特库", treat it as a request to scan local `assets/apparel-model-presets/` age-segment folders for newly added model photos.
- The user may manually place photos under numbered folders such as `00_user_favorites/`, `01_toddler/`, `02_child/`, `03_teen/`, `04_young_adult/`, `05_adult/`, `06_middle_aged/`, and `07_senior/`. Each folder must only contain `female/` and `male/` subfolders; do not create an unspecified gender folder. Keep the folder-based organization; do not require a manifest.
- Model library photo files can be `png`, `jpg/jpeg`, or `webp`; do not assume only webp.
- For new/unprocessed photos, inspect the image, infer 2-3 short Chinese tags describing the model's style/personality/visual direction, then rename the file to the established normalized naming format that includes age segment and descriptive tags. Preserve file extensions and avoid overwriting existing files.
- Use the rename state as the processing marker: on later scans, decide whether an image needs recognition by whether it already matches the normalized renamed-file rule. Already normalized files should be skipped unless the user explicitly asks to rescan.
- When users upload or favorite model images through the app, keep requiring a category selection: `user_favorites` or a real age segment. Do not offer an unspecified/unknown age option. Also require gender as exactly `female` or `male`, and map the item to `<category_folder>/<gender>`. Tags are optional; only include user-entered tags when they explicitly choose to fill tags.

## Lumen UI Theme and Dialog Standards
- For all new frontend features, follow `docs/frontend-theme-dialog-standards.md` and the Design System section in `docs/DESIGN.md`. Treat `apps/web/src/app/globals.css` theme variables as the source of truth.
- Normal UI, dialogs, popovers, toasts, tooltips, forms, sidebars, admin panels, share pages, and settings pages must use semantic tokens: `--bg-*`, `--fg-*`, `--border*`, `--accent`, `--danger`, `--success`, and `--shadow-*`.
- Do not hard-code dark styling in normal UI: avoid `bg-neutral-900/950`, `bg-black/*`, `text-white`, `hover:text-white`, and `border-white/*` unless the element is an intentional media/lightbox/image overlay, code surface, or semantic danger/success button.
- All mobile dialogs and sheets must use the safe-area utilities from `globals.css`: `mobile-dialog-shell`, `mobile-dialog-panel` or `mobile-dialog-sheet`, `mobile-dialog-scroll`, and `mobile-dialog-footer`.
- Before finishing UI work, scan for hard-coded dark utilities and classify any remaining hits. Remaining dark hits should be limited to media/lightbox overlays, code blocks, scrims, destructive buttons, and badges on colored backgrounds. Then run `git diff --check`, `npm run type-check`, `npm run lint`, and `npm run build` from `apps/web` when frontend behavior or styling changed.
