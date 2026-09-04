import { apiFetch } from "./http";
import {
  semanticJsonPostRequest,
  semanticPostRequest,
  withSemanticPostIdempotency,
} from "./semanticIdempotency";
import type { BackendGeneration, BackendImageMeta } from "./tasks";

export interface WorkflowStep {
  id: string;
  workflow_run_id: string;
  step_key: string;
  status: string;
  input_json: Record<string, unknown>;
  output_json: Record<string, unknown>;
  task_ids: string[];
  image_ids: string[];
  approved_at: string | null;
  approved_by: string | null;
  created_at: string;
  updated_at: string;
}

export interface ModelCandidate {
  id: string;
  workflow_run_id: string;
  candidate_index: number;
  portrait_image_id: string | null;
  front_image_id: string | null;
  side_image_id: string | null;
  back_image_id: string | null;
  contact_sheet_image_id: string | null;
  model_brief_json: Record<string, unknown>;
  task_ids: string[];
  status: string;
  selected_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface QualityReport {
  id: string;
  workflow_run_id: string;
  image_id: string;
  overall_score: number;
  product_fidelity_score: number;
  model_consistency_score: number;
  aesthetic_score: number;
  artifact_score: number;
  issues_json: Array<Record<string, unknown>>;
  recommendation: string;
  created_at: string;
  updated_at: string;
}

export interface PosterMaster {
  id: string;
  workflow_run_id: string;
  candidate_index: number;
  image_id: string | null;
  style_summary_json: Record<string, unknown>;
  task_ids: string[];
  status: string;
  selected_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface PosterRender {
  id: string;
  workflow_run_id: string;
  master_id: string | null;
  aspect_ratio: string;
  size: string;
  image_id: string | null;
  task_ids: string[];
  status: string;
  metadata_jsonb: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface WorkflowRun {
  id: string;
  conversation_id: string | null;
  user_id: string;
  type: string;
  status: string;
  title: string;
  user_prompt: string;
  product_image_ids: string[];
  current_step: string;
  quality_mode: string;
  metadata_jsonb: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  steps: WorkflowStep[];
  model_candidates: ModelCandidate[];
  quality_reports: QualityReport[];
  // 海报工作流（type=poster_design）使用；apparel 类型保持空数组。
  poster_masters?: PosterMaster[];
  poster_renders?: PosterRender[];
  product_images: BackendImageMeta[];
  generated_images: BackendImageMeta[];
  generations: BackendGeneration[];
}

export interface WorkflowRunListItem {
  id: string;
  conversation_id: string | null;
  type: string;
  status: string;
  title: string;
  user_prompt: string;
  product_image_ids: string[];
  current_step: string;
  quality_mode: string;
  metadata_jsonb: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  output_count: number;
  completion_percent: number;
  next_action: string;
}

export interface WorkflowRunListResponse {
  items: WorkflowRunListItem[];
  next_cursor?: string | null;
}

export interface CreateApparelWorkflowIn {
  conversation_id?: string | null;
  product_image_ids: string[];
  user_prompt: string;
  quality_mode?: "standard" | "premium";
  title?: string | null;
}

export interface PatchWorkflowIn {
  title?: string;
}

export interface CreateApparelWorkflowOut {
  workflow_run_id: string;
  status: string;
  current_step: string;
}

export interface ModelCandidatesIn {
  candidate_count?: 3;
  style_prompt: string;
  avoid?: string[];
  accessory_plan?: AccessoryPlan;
}

export interface AccessoryPlan {
  enabled: boolean;
  items: string[];
  strength: "subtle" | "medium" | "strong";
}

export interface ApproveModelCandidateIn {
  adjustments?: string;
  accessory_plan: AccessoryPlan;
  selected_accessory_image_id?: string | null;
}

export interface AccessoryPreviewIn {
  candidate_id: string;
  accessory_plan: AccessoryPlan;
  style_prompt?: string;
}

export interface AccessorySelectionIn {
  selected_accessory_image_id: string | null;
}

export type ModelLibraryAgeSegment =
  | "all"
  | "user_favorites"
  | "toddler"
  | "child"
  | "teen"
  | "young_adult"
  | "adult"
  | "middle_aged"
  | "senior";

export type ModelLibraryItemAgeSegment = Exclude<
  ModelLibraryAgeSegment,
  "all"
>;
export type ModelLibrarySource =
  | "preset"
  | "favorite"
  | "user_upload"
  | "generated";

export type ModelLibraryAppearance =
  | "all"
  | "asian"
  | "east_asian"
  | "southeast_asian"
  | "south_asian"
  | "european"
  | "latin"
  | "middle_eastern"
  | "african"
  | "mixed"
  | "other";

export const MODEL_LIBRARY_APPEARANCE_LABEL: Record<
  Exclude<ModelLibraryAppearance, "all">,
  string
> = {
  asian: "亚洲",
  east_asian: "东亚",
  southeast_asian: "东南亚",
  south_asian: "南亚",
  european: "欧洲/欧美",
  latin: "拉美/拉丁裔",
  middle_eastern: "中东",
  african: "非洲/非裔",
  mixed: "混血/多族裔",
  other: "其他",
};

export const MODEL_LIBRARY_APPEARANCE_SELECT_OPTIONS: Array<
  Exclude<ModelLibraryAppearance, "all" | "asian" | "other">
> = [
  "east_asian",
  "southeast_asian",
  "south_asian",
  "european",
  "latin",
  "middle_eastern",
  "african",
  "mixed",
];

export interface ApparelModelLibrarySyncState {
  last_success_at: string | null;
  last_error: string | null;
  can_sync: boolean;
  github_contents_url?: string | null;
}

export interface ApparelModelLibraryItem {
  id: string;
  source: ModelLibrarySource;
  visibility_scope: "global_preset" | "user_private";
  title: string;
  age_segment: ModelLibraryItemAgeSegment;
  gender: string | null;
  appearance_direction: string | null;
  style_tags: string[];
  image_url: string;
  display_url: string | null;
  thumb_url: string | null;
  image_id: string | null;
  preset_id?: string | null;
  version?: number | null;
  library_folder?: string | null;
  prompt_hint?: string | null;
  download_filename?: string | null;
  is_dual_race_bonus?: boolean;
  billing_free?: boolean;
  billing_label?: string | null;
  billing_exempt_reason?: string | null;
  usage_count: number;
  created_at: string;
  updated_at?: string | null;
}

export interface ApparelModelLibraryListResponse {
  items: ApparelModelLibraryItem[];
  sync: ApparelModelLibrarySyncState;
}

export interface ApparelModelLibrarySyncResponse {
  status: "ok" | "failed" | "skipped";
  added: number;
  updated: number;
  skipped: number;
  errors: string[];
  last_success_at: string | null;
  last_error: string | null;
}

export interface ApparelModelLibraryItemCreateIn {
  source: "favorite" | "user_upload" | "generated";
  visibility_scope?: "user_private";
  image_id: string;
  title: string;
  age_segment: ModelLibraryItemAgeSegment;
  gender?: string | null;
  appearance_direction?: string | null;
  style_tags?: string[];
  auto_tag?: boolean;
}

export interface ApparelModelLibraryBatchDeleteOut {
  ok: boolean;
  deleted: number;
  not_found: string[];
}

export interface ApparelModelLibrarySelectIn {
  library_item_id: string;
  mode?: "use_directly";
  style_prompt?: string;
  accessory_plan?: AccessoryPlan;
}

export interface ModelCandidateSaveToLibraryIn {
  title: string;
  age_segment: ModelLibraryItemAgeSegment;
  gender?: string | null;
  appearance_direction?: string | null;
  style_tags?: string[];
}

export interface CreateShowcaseImagesIn {
  template:
    | "white_ecommerce"
    | "premium_studio"
    | "urban_commute"
    | "lifestyle"
    | "daily_snapshot"
    | "natural_phone_snapshot"
    | "social_seed";
  shot_plan: Array<
    "front_full_body" | "natural_pose" | "detail_half_body" | "side_or_back"
  >;
  aspect_ratio:
    | "4:5"
    | "3:4"
    | "1:1"
    | "9:16"
    | "4:3"
    | "3:2"
    | "16:9"
    | "21:9";
  final_quality: "standard" | "high" | "4k";
  output_count: 1 | 2 | 4 | 8 | 16;
  scene_environment?: "indoor" | "outdoor";
  scene_strategy?: "balanced" | "natural_series" | "editorial_campaign";
  scene_variety?: "safe" | "rich" | "wild";
  scene_planner?: "gpt55_preflight" | "gpt55_batch_only" | "rules_fallback";
  continuity_anchor?: "none" | "accessory" | "pet" | "location_series";
  allow_pet?: boolean;
  allow_background_people?: boolean;
}

export interface ReviseWorkflowImageIn {
  instruction: string;
  scope: "full_image" | "local_repair";
}

type SemanticScope = Record<string, string>;

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object";
}

function nonEmptyStrings(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter(
        (item): item is string =>
          typeof item === "string" && item.length > 0,
      )
    : [];
}

function validateIdField<T>(
  value: T,
  field: string,
  message: string,
): T {
  if (
    !isRecord(value) ||
    typeof value[field] !== "string" ||
    value[field].length === 0
  ) {
    throw new TypeError(message);
  }
  return value;
}

function validateWorkflowTaskResponse(
  value: WorkflowRun,
  stepKey: string,
  source: "task_ids" | "active_task_ids" = "task_ids",
): WorkflowRun {
  if (
    !isRecord(value) ||
    typeof value.id !== "string" ||
    value.id.length === 0 ||
    !Array.isArray(value.steps)
  ) {
    throw new TypeError("malformed workflow task response");
  }
  const step = value.steps.find(
    (item) => isRecord(item) && item.step_key === stepKey,
  );
  const taskIds =
    source === "task_ids"
      ? nonEmptyStrings(step?.task_ids)
      : nonEmptyStrings(
          isRecord(step?.input_json)
            ? step.input_json.active_task_ids
            : undefined,
        );
  if (taskIds.length === 0) {
    throw new TypeError("malformed workflow task response");
  }
  return value;
}

function semanticWorkflowPost<T>(
  path: string,
  scope: SemanticScope,
  payload: unknown,
  validate: (value: T) => T,
): Promise<T> {
  return withSemanticPostIdempotency(scope, payload, (idempotencyKey) =>
    apiFetch<T>(
      path,
      semanticJsonPostRequest(payload, idempotencyKey),
    ).then(validate),
  );
}

function semanticEmptyWorkflowPost<T>(
  path: string,
  scope: SemanticScope,
  validate: (value: T) => T,
): Promise<T> {
  const payload = {};
  return withSemanticPostIdempotency(scope, payload, (idempotencyKey) =>
    apiFetch<T>(path, semanticPostRequest(idempotencyKey)).then(validate),
  );
}

export function listWorkflows(
  opts: { type?: string; limit?: number } = {},
): Promise<WorkflowRunListResponse> {
  const qs = new URLSearchParams();
  if (opts.type) qs.set("type", opts.type);
  if (opts.limit != null) qs.set("limit", String(opts.limit));
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return apiFetch<WorkflowRunListResponse>(`/workflows${suffix}`);
}

export function getWorkflow(id: string): Promise<WorkflowRun> {
  return apiFetch<WorkflowRun>(`/workflows/${id}`);
}

export function patchWorkflow(
  id: string,
  body: PatchWorkflowIn,
): Promise<WorkflowRun> {
  return apiFetch<WorkflowRun>(`/workflows/${id}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export function deleteWorkflow(id: string): Promise<{ ok: boolean }> {
  return apiFetch<{ ok: boolean }>(`/workflows/${id}`, { method: "DELETE" });
}

export function createApparelWorkflow(
  body: CreateApparelWorkflowIn,
): Promise<CreateApparelWorkflowOut> {
  const payload = {
    quality_mode: "premium" as const,
    ...body,
  };
  return semanticWorkflowPost<CreateApparelWorkflowOut>(
    "/workflows/apparel-model-showcase",
    { operation: "workflow.apparel.create" },
    payload,
    (value) =>
      validateIdField(
        value,
        "workflow_run_id",
        "malformed apparel workflow response",
      ),
  );
}

export function approveProductAnalysis(
  workflowId: string,
  corrections: Record<string, unknown> = {},
): Promise<WorkflowRun> {
  return apiFetch<WorkflowRun>(
    `/workflows/${workflowId}/steps/product-analysis/approve`,
    {
      method: "POST",
      body: JSON.stringify({ corrections }),
    },
  );
}

export function createModelCandidates(
  workflowId: string,
  body: ModelCandidatesIn,
): Promise<WorkflowRun> {
  const payload = {
    candidate_count: 3 as const,
    avoid: [],
    ...body,
  };
  return semanticWorkflowPost<WorkflowRun>(
    `/workflows/${workflowId}/model-candidates`,
    {
      operation: "workflow.apparel.model_candidates.create",
      workflowId,
    },
    payload,
    (value) => validateWorkflowTaskResponse(value, "model_candidates"),
  );
}

export function approveModelCandidate(
  workflowId: string,
  candidateId: string,
  body: ApproveModelCandidateIn,
): Promise<WorkflowRun> {
  return apiFetch<WorkflowRun>(
    `/workflows/${workflowId}/model-candidates/${candidateId}/approve`,
    {
      method: "POST",
      body: JSON.stringify({
        adjustments: "",
        ...body,
      }),
    },
  );
}

export function reopenModelSelection(
  workflowId: string,
): Promise<WorkflowRun> {
  return apiFetch<WorkflowRun>(
    `/workflows/${workflowId}/model-candidates/reopen`,
    {
      method: "POST",
    },
  );
}

export function createAccessoryPreviews(
  workflowId: string,
  body: AccessoryPreviewIn,
): Promise<WorkflowRun> {
  return semanticWorkflowPost<WorkflowRun>(
    `/workflows/${workflowId}/model-candidates/accessory-previews`,
    {
      operation: "workflow.apparel.accessory_previews.create",
      workflowId,
    },
    body,
    (value) => validateWorkflowTaskResponse(value, "model_approval"),
  );
}

export function saveAccessorySelection(
  workflowId: string,
  body: AccessorySelectionIn,
): Promise<WorkflowRun> {
  return apiFetch<WorkflowRun>(
    `/workflows/${workflowId}/model-candidates/accessory-selection`,
    {
      method: "POST",
      body: JSON.stringify(body),
    },
  );
}

export function listApparelModelLibrary(
  opts: {
    age_segment?: ModelLibraryAgeSegment;
    source?: "all" | ModelLibrarySource;
    appearance?: ModelLibraryAppearance;
    q?: string;
  } = {},
): Promise<ApparelModelLibraryListResponse> {
  const qs = new URLSearchParams();
  if (opts.age_segment) qs.set("age_segment", opts.age_segment);
  if (opts.source) qs.set("source", opts.source);
  if (opts.appearance) qs.set("appearance", opts.appearance);
  if (opts.q) qs.set("q", opts.q);
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return apiFetch<ApparelModelLibraryListResponse>(
    `/workflows/apparel-model-library${suffix}`,
  );
}

export function syncApparelModelLibraryPresets(): Promise<ApparelModelLibrarySyncResponse> {
  return apiFetch<ApparelModelLibrarySyncResponse>(
    "/workflows/apparel-model-library/sync-presets",
    { method: "POST" },
  );
}

export function createApparelModelLibraryItem(
  body: ApparelModelLibraryItemCreateIn,
): Promise<ApparelModelLibraryItem> {
  const payload = {
    visibility_scope: "user_private" as const,
    style_tags: [],
    ...body,
  };
  return semanticWorkflowPost<ApparelModelLibraryItem>(
    "/workflows/apparel-model-library/items",
    { operation: "workflow.apparel.model_library.item.create" },
    payload,
    (value) =>
      validateIdField(
        value,
        "id",
        "malformed apparel model library item response",
      ),
  );
}

export function deleteApparelModelLibraryItem(
  itemId: string,
): Promise<{ ok: boolean }> {
  return apiFetch<{ ok: boolean }>(
    `/workflows/apparel-model-library/items/${encodeURIComponent(itemId)}`,
    { method: "DELETE" },
  );
}

export function deleteApparelModelLibraryItems(
  itemIds: string[],
): Promise<ApparelModelLibraryBatchDeleteOut> {
  return apiFetch<ApparelModelLibraryBatchDeleteOut>(
    "/workflows/apparel-model-library/items/batch-delete",
    {
      method: "POST",
      body: JSON.stringify({ item_ids: itemIds }),
    },
  );
}

export function selectApparelModelLibraryItem(
  workflowId: string,
  body: ApparelModelLibrarySelectIn,
): Promise<WorkflowRun> {
  return apiFetch<WorkflowRun>(`/workflows/${workflowId}/model-library/select`, {
    method: "POST",
    body: JSON.stringify({ mode: "use_directly", ...body }),
  });
}

export function saveModelCandidateToLibrary(
  workflowId: string,
  candidateId: string,
  body: ModelCandidateSaveToLibraryIn,
): Promise<ApparelModelLibraryItem> {
  const payload = { style_tags: [], ...body };
  return semanticWorkflowPost<ApparelModelLibraryItem>(
    `/workflows/${workflowId}/model-candidates/${candidateId}/save-to-library`,
    {
      operation: "workflow.apparel.model_candidate.save_to_library",
      workflowId,
      candidateId,
    },
    payload,
    (value) =>
      validateIdField(
        value,
        "id",
        "malformed apparel model library item response",
      ),
  );
}

// ——— Apparel model library: standalone library generator + tasks ———

export type ApparelModelLibraryJobOrigin =
  | "library_generate"
  | "project_candidate";
export type ApparelModelLibraryJobStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "partial";
export type ApparelModelLibraryGenerateCount = 1 | 2 | 4 | 16;
export type ApparelModelLibraryGenerateMode = "text" | "reference_image";

export interface ApparelModelLibraryExtractedProfile {
  age_segment?: ModelLibraryItemAgeSegment | null;
  gender?: string | null;
  appearance_direction?: string | null;
  style_tags?: string[];
  notes?: string | null;
}

export interface ApparelModelLibraryJobItem {
  image_id: string;
  image_url: string;
  display_url: string | null;
  thumb_url: string | null;
  saved_item_id: string | null;
  style_tags: string[];
  appearance_direction: string | null;
  gender: string | null;
  download_filename: string | null;
  is_dual_race_bonus?: boolean;
  billing_free?: boolean;
  billing_label?: string | null;
  billing_exempt_reason?: string | null;
}

export interface ApparelModelLibraryJob {
  job_id: string;
  origin: ApparelModelLibraryJobOrigin;
  workflow_run_id: string;
  // 仅 origin=project_candidate 时返回；library_generate 永远是 null
  project_title: string | null;
  status: ApparelModelLibraryJobStatus;
  requested_count: number;
  finished_count: number;
  age_segment: ModelLibraryItemAgeSegment | null;
  gender: string | null;
  appearance_direction: string | null;
  extra_requirements: string | null;
  reference_image_id: string | null;
  reference_image_url: string | null;
  extracted_profile: ApparelModelLibraryExtractedProfile | null;
  items: ApparelModelLibraryJobItem[];
  // dual_race 模式下另一路 provider 产出的图，展示在候选区，也可按需保存到模特库
  candidates: ApparelModelLibraryJobItem[];
  error_message: string | null;
  created_at: string;
  updated_at: string | null;
}

export interface ApparelModelLibraryJobsList {
  items: ApparelModelLibraryJob[];
  limit: number;
  offset: number;
  has_more: boolean;
}

export interface ApparelModelLibraryJobsOpts {
  limit?: number;
  offset?: number;
}

export interface ApparelModelLibraryGenerateIn {
  mode?: ApparelModelLibraryGenerateMode;
  reference_image_id?: string | null;
  age_segment?: ModelLibraryItemAgeSegment | null;
  gender?: string | null;
  genders?: Array<"female" | "male">;
  appearance_direction?: string | null;
  extra_requirements?: string | null;
  style_tags?: string[];
  count: ApparelModelLibraryGenerateCount;
  auto_tag: boolean;
}

export interface ApparelModelLibrarySaveJobItemIn {
  title: string;
  age_segment: ModelLibraryItemAgeSegment;
  gender: string;
  appearance_direction?: string | null;
  style_tags?: string[];
  auto_tag: boolean;
}

export interface ApparelModelLibraryAutoTagOut {
  item_id: string;
  style_tags: string[];
  appearance_direction: string | null;
  age_segment: ModelLibraryItemAgeSegment | null;
  gender: string | null;
  notes: string | null;
}

export function generateApparelModelLibrary(
  body: ApparelModelLibraryGenerateIn,
): Promise<ApparelModelLibraryJob> {
  const payload = {
    mode: "text" as const,
    reference_image_id: null,
    style_tags: [],
    appearance_direction: null,
    extra_requirements: null,
    ...body,
  };
  return semanticWorkflowPost<ApparelModelLibraryJob>(
    "/workflows/apparel-model-library/generate",
    { operation: "workflow.apparel.model_library.generate" },
    payload,
    (value) => {
      validateIdField(
        value,
        "job_id",
        "malformed apparel model library job response",
      );
      return validateIdField(
        value,
        "workflow_run_id",
        "malformed apparel model library job response",
      );
    },
  );
}

export function getApparelModelLibraryJobs(
  opts: ApparelModelLibraryJobsOpts = {},
): Promise<ApparelModelLibraryJobsList> {
  const qs = new URLSearchParams();
  if (opts.limit != null) qs.set("limit", String(opts.limit));
  if (opts.offset != null) qs.set("offset", String(opts.offset));
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return apiFetch<ApparelModelLibraryJobsList>(
    `/workflows/apparel-model-library/jobs${suffix}`,
  );
}

export function deleteApparelModelLibraryJob(
  workflowRunId: string,
): Promise<{ ok: boolean }> {
  return apiFetch<{ ok: boolean }>(
    `/workflows/apparel-model-library/jobs/${encodeURIComponent(workflowRunId)}`,
    { method: "DELETE" },
  );
}

export function clearApparelModelLibraryJobs(): Promise<{
  ok: boolean;
  deleted: number;
}> {
  return apiFetch<{ ok: boolean; deleted: number }>(
    "/workflows/apparel-model-library/jobs",
    { method: "DELETE" },
  );
}

export function saveApparelModelLibraryJobItem(
  workflowRunId: string,
  imageId: string,
  body: ApparelModelLibrarySaveJobItemIn,
): Promise<ApparelModelLibraryItem> {
  const payload = { style_tags: [], ...body };
  return semanticWorkflowPost<ApparelModelLibraryItem>(
    `/workflows/apparel-model-library/jobs/${encodeURIComponent(workflowRunId)}/items/${encodeURIComponent(imageId)}/save`,
    {
      operation: "workflow.apparel.model_library.job_item.save",
      workflowRunId,
      imageId,
    },
    payload,
    (value) =>
      validateIdField(
        value,
        "id",
        "malformed apparel model library item response",
      ),
  );
}

export function autoTagApparelModelLibraryItem(
  itemId: string,
): Promise<ApparelModelLibraryAutoTagOut> {
  return semanticEmptyWorkflowPost<ApparelModelLibraryAutoTagOut>(
    `/workflows/apparel-model-library/items/${encodeURIComponent(itemId)}/auto-tag`,
    {
      operation: "workflow.apparel.model_library.item.auto_tag",
      itemId,
    },
    (value) =>
      validateIdField(
        value,
        "item_id",
        "malformed apparel model library auto-tag response",
      ),
  );
}

export function createShowcaseImages(
  workflowId: string,
  body: CreateShowcaseImagesIn,
): Promise<WorkflowRun> {
  return semanticWorkflowPost<WorkflowRun>(
    `/workflows/${workflowId}/showcase-images`,
    { operation: "workflow.apparel.showcase.create", workflowId },
    body,
    (value) =>
      validateWorkflowTaskResponse(
        value,
        "showcase_generation",
        "active_task_ids",
      ),
  );
}

export function reviseWorkflowImage(
  workflowId: string,
  imageId: string,
  body: ReviseWorkflowImageIn,
): Promise<WorkflowRun> {
  return semanticWorkflowPost<WorkflowRun>(
    `/workflows/${workflowId}/images/${imageId}/revise`,
    {
      operation: "workflow.apparel.showcase.revise",
      workflowId,
      imageId,
    },
    body,
    (value) =>
      validateWorkflowTaskResponse(
        value,
        "showcase_generation",
        "active_task_ids",
      ),
  );
}

export function completeWorkflowDelivery(
  workflowId: string,
): Promise<WorkflowRun> {
  return apiFetch<WorkflowRun>(`/workflows/${workflowId}/delivery/complete`, {
    method: "POST",
  });
}
