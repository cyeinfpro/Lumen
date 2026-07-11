import { apiFetch } from "./http";

// ============================================================================
// Poster Style Library（V1.1 海报工作流）
// 与 ApparelModelLibrary* 同构：DB 表 + GitHub 同步 + 用户生成。
// 后端路由：apps/api/app/routes/poster_styles.py（前缀 /poster-styles）
// schemas：packages/core/lumen_core/schemas.py 的 PosterStyle* 类
// ============================================================================

export type PosterStyleSource =
  | "preset"
  | "favorite"
  | "user_upload"
  | "generated";
export type PosterStyleVisibilityScope = "global_preset" | "user_private";
export type PosterStyleCategory =
  | "user_favorites"
  | "illustration"
  | "3d"
  | "minimal"
  | "retro"
  | "traditional"
  | "photo"
  | "other";
// list / filter 用：含 "all"
export type PosterStyleCategoryFilter = "all" | PosterStyleCategory;
export type PosterStyleSourceFilter = "all" | PosterStyleSource;
// 风格库生成允许的张数档位（对齐后端 POSTER_STYLE_GENERATE_MAX_COUNT=4）
export type PosterStyleGenerateCount = 1 | 2 | 3 | 4;
export type PosterStyleJobStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "partial";

// 类目中英映射（卡片徽标、tabs、筛选）
export const POSTER_STYLE_CATEGORY_LABEL: Record<
  PosterStyleCategoryFilter,
  string
> = {
  all: "全部",
  user_favorites: "收藏",
  illustration: "商业扁平插画",
  "3d": "3D",
  minimal: "极简",
  retro: "复古",
  traditional: "中式",
  photo: "杂志摄影",
  other: "其他",
};

// 来源中英映射
export const POSTER_STYLE_SOURCE_LABEL: Record<
  PosterStyleSourceFilter,
  string
> = {
  all: "全部",
  preset: "预设",
  favorite: "收藏",
  user_upload: "上传",
  generated: "生成",
};

// 类目下拉用选项（用户编辑/创建时可选项，排除 "all"）
export const POSTER_STYLE_CATEGORY_OPTIONS: PosterStyleCategory[] = [
  "user_favorites",
  "illustration",
  "3d",
  "minimal",
  "retro",
  "traditional",
  "photo",
  "other",
];

// 推荐宽高比可选项（与生成 aspect_ratio 一致）
export const POSTER_STYLE_ASPECT_OPTIONS: string[] = [
  "1:1",
  "4:5",
  "3:4",
  "9:16",
  "16:9",
];

export interface PosterStyleSample {
  index: number;
  image_id: string | null;
  image_url: string;
  display_url: string | null;
  thumb_url: string | null;
}

export interface PosterStyleSyncState {
  last_success_at: string | null;
  last_error: string | null;
  can_sync: boolean;
  github_contents_url?: string | null;
}

export interface PosterStyleItem {
  id: string;
  source: PosterStyleSource;
  visibility_scope: PosterStyleVisibilityScope;
  title: string;
  category: PosterStyleCategory;
  mood: string | null;
  prompt_template: string | null;
  palette: string[];
  recommended_aspects: string[];
  style_tags: string[];
  cover_image_url: string;
  display_url: string | null;
  thumb_url: string | null;
  cover_image_id: string | null;
  sample_image_ids: string[];
  samples: PosterStyleSample[];
  preset_id?: string | null;
  version?: number | null;
  library_folder?: string | null;
  download_filename?: string | null;
  auto_tagged_at: string | null;
  auto_tag_notes?: string | null;
  created_at: string;
  updated_at?: string | null;
}

export interface PosterStyleListOut {
  items: PosterStyleItem[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
  sync: PosterStyleSyncState;
}

export interface PosterStyleSyncOut {
  status: "ok" | "failed" | "skipped";
  added: number;
  updated: number;
  skipped: number;
  errors: string[];
  last_success_at: string | null;
  last_error: string | null;
}

export interface PosterStyleCreateIn {
  source?: "favorite" | "user_upload" | "generated";
  visibility_scope?: "user_private";
  cover_image_id: string;
  sample_image_ids?: string[];
  title: string;
  category?: PosterStyleCategory;
  mood?: string | null;
  prompt_template?: string | null;
  palette?: string[];
  recommended_aspects?: string[];
  style_tags?: string[];
  auto_tag?: boolean;
}

export interface PosterStylePatchIn {
  title?: string;
  category?: PosterStyleCategory;
  mood?: string | null;
  prompt_template?: string | null;
  palette?: string[];
  recommended_aspects?: string[];
  style_tags?: string[];
}

export interface PosterStyleBatchDeleteOut {
  ok: boolean;
  deleted: number;
  not_found: string[];
}

export interface PosterStyleGenerateIn {
  title: string;
  category?: PosterStyleCategory;
  prompt: string;
  prompt_template?: string | null;
  style_tags?: string[];
  palette?: string[];
  recommended_aspects?: string[];
  mood?: string | null;
  aspect_ratio?: string;
  count: PosterStyleGenerateCount;
  auto_tag?: boolean;
}

export interface PosterStyleGenerateOut {
  job_id: string;
  workflow_run_id: string;
  status: "queued" | "running";
  requested_count: number;
  task_ids: string[];
  created_at: string;
}

export interface PosterStyleJobOut {
  job_id: string;
  workflow_run_id: string;
  title: string;
  category: PosterStyleCategory;
  status: PosterStyleJobStatus;
  requested_count: number;
  finished_count: number;
  prompt: string | null;
  style_tags: string[];
  image_ids: string[];
  saved_item_id: string | null;
  error_message: string | null;
  created_at: string;
  updated_at: string | null;
}

export interface PosterStyleJobsOut {
  items: PosterStyleJobOut[];
  limit: number;
  offset: number;
  has_more: boolean;
}

export interface PosterStyleAutoTagOut {
  item_id: string;
  style_tags: string[];
  category: PosterStyleCategory | null;
  mood: string | null;
  palette: string[];
  notes: string | null;
}

export interface PosterStyleListOpts {
  category?: PosterStyleCategoryFilter;
  source?: PosterStyleSourceFilter;
  q?: string;
  tags?: string[];
  limit?: number;
  offset?: number;
}

// item_id 可能含 ":"（preset:xxx:v1 / user:uuid），需 encode
function encodePosterStyleId(id: string): string {
  return encodeURIComponent(id);
}

export function listPosterStyles(
  opts: PosterStyleListOpts = {},
): Promise<PosterStyleListOut> {
  const qs = new URLSearchParams();
  if (opts.category) qs.set("category", opts.category);
  if (opts.source) qs.set("source", opts.source);
  if (opts.q) qs.set("q", opts.q);
  if (opts.tags && opts.tags.length > 0) {
    for (const tag of opts.tags) qs.append("tags", tag);
  }
  if (opts.limit != null) qs.set("limit", String(opts.limit));
  if (opts.offset != null) qs.set("offset", String(opts.offset));
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return apiFetch<PosterStyleListOut>(`/poster-styles${suffix}`);
}

export function getPosterStyle(itemId: string): Promise<PosterStyleItem> {
  return apiFetch<PosterStyleItem>(
    `/poster-styles/${encodePosterStyleId(itemId)}`,
  );
}

export function patchPosterStyle(
  itemId: string,
  body: PosterStylePatchIn,
): Promise<PosterStyleItem> {
  return apiFetch<PosterStyleItem>(
    `/poster-styles/items/${encodePosterStyleId(itemId)}`,
    {
      method: "PATCH",
      body: JSON.stringify(body),
    },
  );
}

export function deletePosterStyle(itemId: string): Promise<{ ok: boolean }> {
  return apiFetch<{ ok: boolean }>(
    `/poster-styles/items/${encodePosterStyleId(itemId)}`,
    { method: "DELETE" },
  );
}

export function batchDeletePosterStyles(
  itemIds: string[],
): Promise<PosterStyleBatchDeleteOut> {
  return apiFetch<PosterStyleBatchDeleteOut>(
    "/poster-styles/items/batch-delete",
    {
      method: "POST",
      body: JSON.stringify({ item_ids: itemIds }),
    },
  );
}

export function syncPosterStylePresets(): Promise<PosterStyleSyncOut> {
  return apiFetch<PosterStyleSyncOut>("/poster-styles/sync-presets", {
    method: "POST",
  });
}

export function generatePosterStyle(
  body: PosterStyleGenerateIn,
): Promise<PosterStyleGenerateOut> {
  return apiFetch<PosterStyleGenerateOut>("/poster-styles/generate", {
    method: "POST",
    body: JSON.stringify({
      category: "user_favorites",
      style_tags: [],
      palette: [],
      recommended_aspects: [],
      aspect_ratio: "1:1",
      auto_tag: true,
      ...body,
    }),
  });
}

export interface PosterStyleJobsOpts {
  limit?: number;
  offset?: number;
}

export function listPosterStyleJobs(
  opts: PosterStyleJobsOpts = {},
): Promise<PosterStyleJobsOut> {
  const qs = new URLSearchParams();
  if (opts.limit != null) qs.set("limit", String(opts.limit));
  if (opts.offset != null) qs.set("offset", String(opts.offset));
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return apiFetch<PosterStyleJobsOut>(`/poster-styles/jobs${suffix}`);
}

export function triggerPosterStyleAutoTag(
  itemId: string,
): Promise<PosterStyleAutoTagOut> {
  return apiFetch<PosterStyleAutoTagOut>(
    `/poster-styles/items/${encodePosterStyleId(itemId)}/auto-tag`,
    { method: "POST" },
  );
}
