# Poster Style Presets

Built-in visual style presets for the poster design workflow. Each subdirectory
(except `00_user_favorites/`) is one preset.

## Directory layout

```
poster-style-presets/
├── 00_user_favorites/       # placeholder for user-saved styles (no presets here)
├── 01_flat_illustration/
│   ├── meta.json            # required: preset metadata
│   ├── sample-01.webp       # optional: cover preview (synced from GitHub)
│   └── sample-01.thumb.webp
├── 02_3d_render/
├── 03_minimal_typography/
├── 04_retro_pop/
├── 05_chinese_traditional/
└── 06_editorial_photo/
```

## meta.json schema

```json
{
  "preset_id": "string, stable id used to track this preset",
  "title": "中文标题",
  "category": "illustration | 3d | minimal | retro | traditional | photo | other",
  "prompt_template": "english prompt fragment injected as style constraint",
  "palette": ["#hex", ...],
  "mood": "中文情绪关键词",
  "recommended_aspects": ["1:1", "9:16", "16:9", "3:4"],
  "tags": ["中文标签", ...],
  "version": 1
}
```

## Cover samples

Samples are optional in the local repo. The API will sync them from GitHub on
demand via `POST /poster-styles/sync-presets` (mirrors apparel-model-library).
If a preset directory has no sample image, the UI shows a generated palette
swatch as a placeholder until the user generates a real sample via the style
library generator.

## Adding a new preset

1. Create a new numbered directory (e.g. `07_watercolor/`).
2. Write `meta.json` following the schema above.
3. Add 1-3 `sample-NN.webp` cover images (optional but recommended).
4. Commit and push. On the next sync trigger the API will index the new preset
   into `poster_style_items` rows with `source=preset`.
