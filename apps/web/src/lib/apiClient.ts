import {
  API_BASE,
  ApiError,
  apiFetch,
  apiFetchNoContent,
  handle401,
  readCookie,
} from "./api/http";
import type { NoContent } from "./api/http";
import type {
  Intent,
  ImageParams,
  AllowedEmailOut,
  AdminRequestEventsOut,
  AdminContextHealthOut,
  AdminModelsOut,
  AdminUserOut,
  UsageOut,
  ShareOut,
  PublicShareOut,
  InviteLinkOut,
  InviteLinkPublicOut,
  SystemSettingsOut,
  ProviderItemIn,
  ProviderProxyIn,
  ProvidersOut,
  ProvidersProbeOut,
  ProviderStatsOut,
  SessionOut,
} from "./types";
export { API_BASE, ApiError, apiFetch, apiFetchNoContent } from "./api/http";
export type { NoContent } from "./api/http";

// —————————————————— 领域接口 ——————————————————

export interface AuthUser {
  id: string;
  email?: string;
  name?: string;
  default_system_prompt_id?: string | null;
}

export function login(email: string, password: string): Promise<AuthUser> {
  return apiFetch<AuthUser>("/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
}

export function signup(
  email: string,
  password: string,
  invite_token?: string,
): Promise<AuthUser> {
  const body: { email: string; password: string; invite_token?: string } = {
    email,
    password,
  };
  if (invite_token) body.invite_token = invite_token;
  return apiFetch<AuthUser>("/auth/signup", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function logout(): Promise<NoContent> {
  return apiFetchNoContent("/auth/logout", { method: "POST" });
}

export function getMe(): Promise<AuthUser> {
  return apiFetch<AuthUser>("/auth/me");
}

// 对齐后端 ConversationOut (packages/core/lumen_core/schemas.py)。
// 时间字段是后端 datetime 的 ISO 8601 字符串（不是 Unix ms）。
export interface ConversationSummary {
  id: string;
  title: string;
  pinned: boolean;
  archived: boolean;
  last_activity_at: string;
  default_params: Record<string, unknown>;
  default_system?: string | null;
  default_system_prompt_id?: string | null;
  created_at: string;
}

export interface ListConversationsOpts {
  cursor?: string;
  q?: string;
  limit?: number;
}

export interface ConversationListResponse {
  items: ConversationSummary[];
  next_cursor?: string | null;
}

export function listConversations(
  opts: ListConversationsOpts = {},
): Promise<ConversationListResponse> {
  const qs = new URLSearchParams();
  if (opts.cursor) qs.set("cursor", opts.cursor);
  if (opts.q) qs.set("q", opts.q);
  if (opts.limit != null) qs.set("limit", String(opts.limit));
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return apiFetch<ConversationListResponse>(`/conversations${suffix}`);
}

export interface CreateConversationIn {
  title?: string;
  default_system?: string | null;
  default_system_prompt_id?: string | null;
  default_params?: Record<string, unknown> | null;
}

export function createConversation(
  body: CreateConversationIn = {},
  opts: { signal?: AbortSignal } = {},
): Promise<ConversationSummary> {
  return apiFetch<ConversationSummary>("/conversations", {
    method: "POST",
    signal: opts.signal,
    body: JSON.stringify({
      title: body.title ?? "",
      default_system: body.default_system ?? null,
      default_system_prompt_id: body.default_system_prompt_id ?? null,
      default_params: body.default_params ?? null,
    }),
  });
}

export function getConversation(id: string): Promise<ConversationSummary> {
  return apiFetch<ConversationSummary>(`/conversations/${id}`);
}

export interface PatchConversationIn {
  title?: string;
  pinned?: boolean;
  archived?: boolean;
  default_params?: Record<string, unknown>;
  default_system?: string | null;
  default_system_prompt_id?: string | null;
}

export function patchConversation(
  id: string,
  body: PatchConversationIn,
): Promise<ConversationSummary> {
  return apiFetch<ConversationSummary>(`/conversations/${id}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export function renameConversation(
  id: string,
  title: string,
): Promise<ConversationSummary> {
  return patchConversation(id, { title });
}

export function deleteConversation(id: string): Promise<NoContent> {
  return apiFetchNoContent(`/conversations/${id}`, { method: "DELETE" });
}

export interface ListMessagesOpts {
  cursor?: string;
  // `since` 接受 ISO 8601 时间戳字符串或 message_id（后端自动判别）。
  since?: string;
  limit?: number;
  // 切会话时取消上一次的 in-flight 请求，避免旧 conv 数据覆盖新 conv
  signal?: AbortSignal;
  // include=tasks → 后端附带返回 generations/completions/images，用于刷新后恢复 store
  include?: Array<"tasks">;
}

// 对齐后端 MessageOut (packages/core/lumen_core/schemas.py)。
// 注意：content 是 dict，用户消息形如 {text, attachments: [{image_id}]}；
// 助手消息初始 {}，成功后可能带 {text}。created_at 是 ISO 8601 字符串。
export interface BackendMessageContent {
  text?: string;
  attachments?: Array<{ image_id: string }>;
  [key: string]: unknown;
}

export type BackendMessageRole = "user" | "assistant" | "system";

export interface BackendMessage {
  id: string;
  conversation_id: string;
  role: BackendMessageRole;
  content: BackendMessageContent;
  intent?: string | null;
  status?: string | null;
  parent_message_id?: string | null;
  created_at: string;
}

export type GenerationTaskStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "canceled";
export type CompletionTaskStatus =
  | "queued"
  | "streaming"
  | "succeeded"
  | "failed"
  | "canceled";
export type TaskStatus = GenerationTaskStatus | CompletionTaskStatus;

// 对齐后端 GenerationOut / CompletionOut / ImageOut（packages/core/lumen_core/schemas.py）。
export interface BackendGeneration {
  id: string;
  message_id: string;
  action: string;
  prompt: string;
  size_requested: string;
  aspect_ratio: string;
  input_image_ids: string[];
  primary_input_image_id: string | null;
  status: GenerationTaskStatus;
  progress_stage: string;
  attempt: number;
  error_code: string | null;
  error_message: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface BackendCompletion {
  id: string;
  message_id: string;
  model: string;
  input_image_ids: string[];
  text: string;
  tokens_in: number;
  tokens_out: number;
  status: CompletionTaskStatus;
  progress_stage: string;
  attempt: number;
  error_code: string | null;
  error_message: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface BackendImageMeta {
  id: string;
  source: string;
  parent_image_id: string | null;
  owner_generation_id?: string | null;
  width: number;
  height: number;
  mime: string;
  blurhash: string | null;
  url: string;
  display_url?: string | null;
  preview_url?: string | null;
  thumb_url?: string | null;
  metadata_jsonb?: Record<string, unknown> | null;
}

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

export interface CreateShowcaseImagesIn {
  template:
    | "white_ecommerce"
    | "premium_studio"
    | "urban_commute"
    | "lifestyle"
    | "social_seed";
  shot_plan: Array<
    "front_full_body" | "natural_pose" | "detail_half_body" | "side_or_back"
  >;
  aspect_ratio: "4:5" | "3:4" | "1:1" | "16:9" | "9:16";
  final_quality: "standard" | "high" | "4k";
  output_count: number;
}

export interface ReviseWorkflowImageIn {
  instruction: string;
  scope: "full_image" | "local_repair";
}

export interface MessageListResponse {
  items: BackendMessage[];
  next_cursor?: string | null;
  generations?: BackendGeneration[] | null;
  completions?: BackendCompletion[] | null;
  images?: BackendImageMeta[] | null;
}

export interface ConversationContextStats {
  input_budget_tokens: number;
  total_target_tokens: number;
  response_reserve_tokens: number;
  estimated_input_tokens: number;
  estimated_history_tokens: number;
  estimated_system_tokens: number;
  included_messages_count: number;
  truncated: boolean;
  percent: number;
  compression_enabled?: boolean;
  summary_available?: boolean;
  summary_tokens?: number;
  summary_up_to_message_id?: string | null;
  summary_updated_at?: string | null;
  summary_first_user_message_id?: string | null;
  summary_compression_runs?: number;
  compressible_messages_count?: number;
  compressible_tokens?: number;
  estimated_tokens_freed?: number;
  summary_target_tokens?: number;
  compressed?: boolean;
  last_fallback_reason?: string | null;
  manual_compact_available?: boolean;
  manual_compact_reset_seconds?: number;
  manual_compact_min_input_tokens?: number;
  manual_compact_cooldown_seconds?: number;
  manual_compact_unavailable_reason?: string | null;
}

export function getConversationContext(
  convId: string,
): Promise<ConversationContextStats> {
  return apiFetch<ConversationContextStats>(`/conversations/${convId}/context`);
}

// —— 结构化项目 / 工作流 ——

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
  return apiFetch<CreateApparelWorkflowOut>("/workflows/apparel-model-showcase", {
    method: "POST",
    body: JSON.stringify({
      quality_mode: "premium",
      ...body,
    }),
  });
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
  return apiFetch<WorkflowRun>(`/workflows/${workflowId}/model-candidates`, {
    method: "POST",
    body: JSON.stringify({
      candidate_count: 3,
      avoid: [],
      ...body,
    }),
  });
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

export function reopenModelSelection(workflowId: string): Promise<WorkflowRun> {
  return apiFetch<WorkflowRun>(`/workflows/${workflowId}/model-candidates/reopen`, {
    method: "POST",
  });
}

export function createAccessoryPreviews(
  workflowId: string,
  body: AccessoryPreviewIn,
): Promise<WorkflowRun> {
  return apiFetch<WorkflowRun>(
    `/workflows/${workflowId}/model-candidates/accessory-previews`,
    {
      method: "POST",
      body: JSON.stringify(body),
    },
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

export function createShowcaseImages(
  workflowId: string,
  body: CreateShowcaseImagesIn,
): Promise<WorkflowRun> {
  return apiFetch<WorkflowRun>(`/workflows/${workflowId}/showcase-images`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function reviseWorkflowImage(
  workflowId: string,
  imageId: string,
  body: ReviseWorkflowImageIn,
): Promise<WorkflowRun> {
  return apiFetch<WorkflowRun>(`/workflows/${workflowId}/images/${imageId}/revise`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function completeWorkflowDelivery(workflowId: string): Promise<WorkflowRun> {
  return apiFetch<WorkflowRun>(`/workflows/${workflowId}/delivery/complete`, {
    method: "POST",
  });
}

// 手动压缩会话上下文（P0-3）
//
// 后端契约（与 apps/api/app/routes/conversations.py 的 compact_conversation 路由对齐）：
//   POST /api/conversations/{conversationId}/compact
//   Body: {} 或 { "extra_instruction"?: string }
//   200 实际产生压缩：
//     { status: "ok", compacted: true, summary: CompactSummary }
//   200 未达预算阈值（短对话不必压缩，后端没调上游也没改库）：
//     { status: "ok", compacted: false, reason: "below_budget",
//       estimated_input_tokens: number, input_budget_tokens: number, safety_margin: number }
//   404:  { detail: "conversation not found" }
//   409:  { detail: "no messages to compact" }
//   503:  { detail: "compression unavailable", reason: "lock_busy"|"circuit_open"|"upstream_error" }

export type CompactSummaryStatus =
  | "created"
  | "cached"
  | "cas_reused"
  | "created_local_fallback"
  | "cached_after_lock_wait";

export interface CompactSummary {
  summary_created: boolean;
  summary_used: boolean;
  summary_up_to_message_id: string;
  summary_up_to_created_at: string; // ISO8601
  tokens: number;
  source_message_count: number;
  source_token_estimate?: number;
  image_caption_count?: number;
  tokens_freed?: number;
  fallback_reason?: string | null;
  compressed_at: string; // ISO8601
  status: CompactSummaryStatus;
}

export interface CompactConversationIn {
  extra_instruction?: string | null;
  // Why: backend short-circuits with { compacted: false, reason: "below_budget" }
  // when force=false (default) and history has not crossed the input-budget gate.
  // For the user-facing manual button we always pass force=true so a click
  // actually invokes upstream — letting users test compaction on short
  // conversations instead of staring at "暂无需压缩".
  force?: boolean;
  background?: boolean;
}

export type CompactSkippedReason = "below_budget";
export type CompactPendingReason = "pending";

export interface CompactConversationCompacted {
  status: "ok";
  compacted: true;
  summary: CompactSummary;
}

export interface CompactConversationSkipped {
  status: "ok";
  compacted: false;
  reason: CompactSkippedReason;
  estimated_input_tokens: number;
  input_budget_tokens: number;
  safety_margin: number;
}

export interface CompactConversationPending {
  status: "pending";
  compacted: false;
  reason: CompactPendingReason;
  job_id: string;
  retry_after_seconds?: number;
}

export interface CompactConversationFailed {
  status: "failed";
  compacted: false;
  reason: CompactUnavailableReason;
  job_id?: string;
}

// Why: 后端在 below_budget 分支不返回 summary，旧的"summary 必填"假设会让
// 组件读 result.summary.status 直接抛 TypeError → React error boundary 把
// 整页打成"出了点问题"。下游消费方必须先看 compacted 再决定如何展示。
export type CompactConversationResponse =
  | CompactConversationCompacted
  | CompactConversationSkipped;

export type CompactConversationApiResponse =
  | CompactConversationResponse
  | CompactConversationPending
  | CompactConversationFailed;

// 503 时 ApiError.payload 形如 { detail, reason }；这里给消费方一个稳定的常量集合便于分支。
export type CompactUnavailableReason =
  | "lock_busy"
  | "circuit_open"
  | "upstream_error";

export function compactConversation(
  convId: string,
  body: CompactConversationIn = {},
): Promise<CompactConversationApiResponse> {
  const payload: Record<string, unknown> = {};
  const extra = body.extra_instruction;
  if (typeof extra === "string" && extra.length > 0) {
    payload.extra_instruction = extra;
  }
  if (body.force === true) {
    payload.force = true;
  }
  if (body.background === true) {
    payload.background = true;
  }
  return apiFetch<CompactConversationApiResponse>(
    `/conversations/${convId}/compact`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

export function getCompactConversationStatus(
  convId: string,
  jobId: string,
): Promise<CompactConversationApiResponse> {
  const q = new URLSearchParams({ job_id: jobId });
  return apiFetch<CompactConversationApiResponse>(
    `/conversations/${convId}/compact/status?${q.toString()}`,
  );
}

export function listMessages(
  convId: string,
  opts: ListMessagesOpts = {},
): Promise<MessageListResponse> {
  const q = new URLSearchParams();
  if (opts.cursor) q.set("cursor", opts.cursor);
  if (opts.since) q.set("since", opts.since);
  if (opts.limit) q.set("limit", String(opts.limit));
  if (opts.include && opts.include.length > 0) q.set("include", opts.include.join(","));
  const suffix = q.toString() ? `?${q.toString()}` : "";
  return apiFetch<MessageListResponse>(
    `/conversations/${convId}/messages${suffix}`,
    opts.signal ? { signal: opts.signal } : undefined,
  );
}

export interface PostMessageIn {
  idempotency_key: string;
  text: string;
  attachment_image_ids?: string[];
  intent?: Intent;
  image_params?: ImageParams;
  chat_params?: Record<string, unknown>;
}

export interface PostMessageOut {
  user_message: BackendMessage;
  assistant_message: BackendMessage;
  completion_id?: string | null;
  generation_ids?: string[];
}

export function postMessage(
  convId: string,
  body: PostMessageIn,
  opts: { signal?: AbortSignal } = {},
): Promise<PostMessageOut> {
  return apiFetch<PostMessageOut>(`/conversations/${convId}/messages`, {
    method: "POST",
    signal: opts.signal,
    body: JSON.stringify(body),
  });
}

export interface RegenerateMessageIn {
  intent: Exclude<Intent, "auto">;
  idempotency_key: string;
}

export interface RegenerateMessageOut {
  assistant_message_id: string;
  completion_id: string | null;
  generation_ids: string[];
}

export function regenerateMessage(
  convId: string,
  messageId: string,
  body: RegenerateMessageIn,
): Promise<RegenerateMessageOut> {
  return apiFetch<RegenerateMessageOut>(
    `/conversations/${convId}/messages/${messageId}/regenerate`,
    {
      method: "POST",
      body: JSON.stringify(body),
    },
  );
}


// —— 系统提示词库 ——

export interface SystemPrompt {
  id: string;
  name: string;
  content: string;
  is_default: boolean;
  created_at: string;
  updated_at: string;
}

export interface SystemPromptListResponse {
  items: SystemPrompt[];
  default_id?: string | null;
}

export interface CreateSystemPromptIn {
  name: string;
  content: string;
  make_default?: boolean;
}

export interface PatchSystemPromptIn {
  name?: string;
  content?: string;
  make_default?: boolean;
}

export function listSystemPrompts(): Promise<SystemPromptListResponse> {
  return apiFetch<SystemPromptListResponse>("/system-prompts");
}

export function createSystemPrompt(
  body: CreateSystemPromptIn,
): Promise<SystemPrompt> {
  return apiFetch<SystemPrompt>("/system-prompts", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function patchSystemPrompt(
  id: string,
  body: PatchSystemPromptIn,
): Promise<SystemPrompt> {
  return apiFetch<SystemPrompt>(`/system-prompts/${id}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export function deleteSystemPrompt(id: string): Promise<NoContent> {
  return apiFetchNoContent(`/system-prompts/${id}`, { method: "DELETE" });
}

export function setDefaultSystemPrompt(id: string): Promise<SystemPrompt> {
  return apiFetch<SystemPrompt>(`/system-prompts/${id}/default`, {
    method: "POST",
  });
}

// —— 图像上传 / 反代 ——

export interface UploadedImage {
  id: string;
  width: number;
  height: number;
  url: string;
  mime?: string;
}

export function uploadImage(file: File): Promise<UploadedImage> {
  const fd = new FormData();
  fd.append("file", file);
  return apiFetch<UploadedImage>("/images/upload", {
    method: "POST",
    body: fd,
  });
}

export function imageBinaryUrl(imageId: string): string {
  return `${API_BASE.replace(/\/$/, "")}/images/${imageId}/binary`;
}

export function imageVariantUrl(
  imageId: string,
  kind: "display2048" | "preview1024" | "thumb256",
): string {
  return `${API_BASE.replace(/\/$/, "")}/images/${imageId}/variants/${kind}`;
}

// —— 任务 ——

export type TaskKind = "generations" | "completions";
export type TaskResponse<K extends TaskKind = TaskKind> =
  K extends "generations" ? BackendGeneration : BackendCompletion;

export interface TaskActionResponse {
  status: TaskStatus;
}

export interface TaskItemResponse {
  kind: "generation" | "completion";
  id: string;
  message_id: string;
  status: TaskStatus;
  progress_stage: string;
  started_at: string | null;
}

export function getTask(kind: "generations", id: string): Promise<BackendGeneration>;
export function getTask(kind: "completions", id: string): Promise<BackendCompletion>;
export function getTask(kind: TaskKind, id: string): Promise<TaskResponse>;
export function getTask(kind: TaskKind, id: string): Promise<TaskResponse> {
  const seg = kind === "generations" ? "generations" : "completions";
  return apiFetch<TaskResponse>(`/${seg}/${id}`);
}

export function getGeneration(id: string): Promise<BackendGeneration> {
  return getTask("generations", id);
}

export function getCompletion(id: string): Promise<BackendCompletion> {
  return getTask("completions", id);
}

export function cancelTask(
  kind: TaskKind,
  id: string,
): Promise<TaskActionResponse> {
  // 后端：POST /generations/{id}/cancel 或 /completions/{id}/cancel（tasks.py:59/140）
  return apiFetch<TaskActionResponse>(`/${kind}/${id}/cancel`, { method: "POST" });
}

export function retryTask(
  kind: TaskKind,
  id: string,
): Promise<TaskActionResponse> {
  // 后端：POST /generations/{id}/retry 或 /completions/{id}/retry（tasks.py:85/169）
  return apiFetch<TaskActionResponse>(`/${kind}/${id}/retry`, { method: "POST" });
}

export interface TaskListOpts {
  status?: string;
  mine?: boolean;
}

export function listTasks(opts: TaskListOpts = {}): Promise<TaskItemResponse[]> {
  const q = new URLSearchParams();
  if (opts.status) q.set("status", opts.status);
  if (opts.mine) q.set("mine", "1");
  const suffix = q.toString() ? `?${q.toString()}` : "";
  return apiFetch<TaskItemResponse[]>(`/tasks${suffix}`);
}

// 用户级中心任务列表：返回当前登录用户**所有**会话的进行中任务完整字段，
// 用于前端启动 / SSE 重连后一次性 hydrate，避免 GlobalTaskTray 按会话碎片化。
export interface ActiveTasksResponse {
  generations: BackendGeneration[];
  completions: BackendCompletion[];
}

export function listMyActiveTasks(): Promise<ActiveTasksResponse> {
  return apiFetch<ActiveTasksResponse>(`/tasks/mine/active`);
}

// —— SSE URL 构造（供 useSSE 使用） ——

export function sseUrl(channels: string[]): string {
  const q = new URLSearchParams({ channels: [...channels].sort().join(",") });
  return `${API_BASE.replace(/\/$/, "")}/events?${q.toString()}`;
}

// —— 静默生成（不创建用户消息） ——

export interface SilentGenerationIn {
  idempotency_key: string;
  parent_message_id: string;
  intent: "text_to_image" | "image_to_image";
  image_params?: ImageParams;
  prompt?: string;
  attachment_image_ids?: string[];
}

export interface SilentGenerationOut {
  assistant_message: BackendMessage;
  generation_ids: string[];
}

export function createSilentGeneration(
  convId: string,
  body: SilentGenerationIn,
): Promise<SilentGenerationOut> {
  return apiFetch<SilentGenerationOut>(
    `/conversations/${convId}/generations`,
    {
      method: "POST",
      body: JSON.stringify(body),
    },
  );
}

// —— 提示词增强（streaming） ——

function createSSEDataParser(onData: (data: string) => void): {
  feed: (chunk: string) => void;
  flush: () => void;
} {
  let buffer = "";
  let dataLines: string[] = [];
  let pendingCR = false;

  const dispatch = () => {
    if (dataLines.length === 0) return;
    const data = dataLines.join("\n");
    dataLines = [];
    onData(data);
  };

  const processLine = (line: string) => {
    if (line === "") {
      dispatch();
      return;
    }
    if (line.startsWith(":")) return;

    const colonIdx = line.indexOf(":");
    const field = colonIdx === -1 ? line : line.slice(0, colonIdx);
    let value = colonIdx === -1 ? "" : line.slice(colonIdx + 1);
    if (value.startsWith(" ")) value = value.slice(1);

    if (field === "data") dataLines.push(value);
  };

  const feed = (chunk: string) => {
    let text = chunk;
    if (pendingCR) {
      if (text.startsWith("\n")) text = text.slice(1);
      pendingCR = false;
    }

    buffer += text;
    let start = 0;
    for (let i = 0; i < buffer.length; i += 1) {
      const code = buffer.charCodeAt(i);
      if (code !== 10 && code !== 13) continue;

      processLine(buffer.slice(start, i));
      if (code === 13) {
        if (i + 1 < buffer.length && buffer.charCodeAt(i + 1) === 10) {
          i += 1;
        } else if (i + 1 === buffer.length) {
          pendingCR = true;
        }
      }
      start = i + 1;
    }
    buffer = buffer.slice(start);
  };

  const flush = () => {
    if (buffer) {
      processLine(buffer);
      buffer = "";
    }
    pendingCR = false;
    dispatch();
  };

  return { feed, flush };
}

export async function enhancePrompt(
  text: string,
  onDelta: (text: string) => void,
  signal?: AbortSignal,
): Promise<void> {
  const url = `${API_BASE.replace(/\/$/, "")}/prompts/enhance`;
  const csrf = readCookie("csrf");
  const res = await fetch(url, {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(csrf ? { "X-CSRF-Token": csrf } : {}),
    },
    body: JSON.stringify({ text }),
    signal,
  });
  if (res.status === 401) {
    handle401();
    throw new ApiError({ code: "unauthorized", message: "未登录", status: 401 });
  }
  if (!res.ok) {
    throw new ApiError({ code: "enhance_failed", message: `HTTP ${res.status}`, status: res.status });
  }
  const reader = res.body?.getReader();
  if (!reader) {
    throw new ApiError({ code: "enhance_empty_response", message: "empty response", status: 502 });
  }
  const decoder = new TextDecoder();
  let hasText = false;
  let streamDone = false;
  const parser = createSSEDataParser((payload) => {
    const data = payload.trim();
    if (data === "[DONE]") {
      streamDone = true;
      return;
    }
    try {
      const evt = JSON.parse(data) as { text?: string; error?: string };
      if (evt.error) throw new ApiError({ code: evt.error, message: evt.error, status: 502 });
      if (evt.text) {
        hasText = true;
        onDelta(evt.text);
      }
    } catch (e) {
      if (e instanceof ApiError) throw e;
      // 非 ApiError（JSON.parse / onDelta 抛出）不应被静默吞掉，
      // 否则会导致 hasText 状态不一致并可能误抛 502。记录后中止流。
      try {
        console.error("[enhancePrompt] parser error:", e);
      } catch {
        /* console 不可用时忽略 */
      }
      throw new ApiError({
        code: "enhance_parse_error",
        message: "Failed to parse enhancement response",
        status: 502,
      });
    }
  });
  while (true) {
    const { done, value } = await reader.read();
    if (done) {
      const tail = decoder.decode();
      if (tail) parser.feed(tail);
      parser.flush();
      break;
    }
    parser.feed(decoder.decode(value, { stream: true }));
    if (streamDone) {
      if (!hasText) {
        throw new ApiError({ code: "enhance_empty_response", message: "empty response", status: 502 });
      }
      try {
        await reader.cancel();
      } catch {
        // ignore
      }
      return;
    }
  }
  if (streamDone && hasText) return;
  if (!hasText) {
    throw new ApiError({ code: "enhance_empty_response", message: "empty response", status: 502 });
  }
}

// ——————————————————————————————————————————————————————————————
// V1 收尾：Admin / Usage / Shares
// 与后端 Agent B 契约对齐；写操作走 apiFetch 自带的 CSRF；
// 公共 share endpoint 不发 credentials，直接用 fetch。
// ——————————————————————————————————————————————————————————————

// ——— Admin: allowed emails ———

export function listAllowedEmails(): Promise<{ items: AllowedEmailOut[] }> {
  return apiFetch<{ items: AllowedEmailOut[] }>("/admin/allowed_emails");
}

export function addAllowedEmail(email: string): Promise<AllowedEmailOut> {
  return apiFetch<AllowedEmailOut>("/admin/allowed_emails", {
    method: "POST",
    body: JSON.stringify({ email }),
  });
}

export function removeAllowedEmail(id: string): Promise<NoContent> {
  return apiFetchNoContent(`/admin/allowed_emails/${id}`, { method: "DELETE" });
}

// ——— Admin: users ———

export function listAdminUsers(
  params: { limit?: number; cursor?: string } = {},
): Promise<{ items: AdminUserOut[]; next_cursor?: string }> {
  const q = new URLSearchParams();
  if (params.limit != null) q.set("limit", String(params.limit));
  if (params.cursor) q.set("cursor", params.cursor);
  const suffix = q.toString() ? `?${q.toString()}` : "";
  return apiFetch<{ items: AdminUserOut[]; next_cursor?: string }>(
    `/admin/users${suffix}`,
  );
}

export function listAdminRequestEvents(
  params: {
    limit?: number;
    kind?: "all" | "generation" | "completion";
    status?: string;
    range?: "24h" | "7d" | "30d";
  } = {},
): Promise<AdminRequestEventsOut> {
  const q = new URLSearchParams();
  if (params.limit != null) q.set("limit", String(params.limit));
  if (params.kind && params.kind !== "all") q.set("kind", params.kind);
  if (params.status) q.set("status", params.status);
  if (params.range) q.set("range", params.range);
  const suffix = q.toString() ? `?${q.toString()}` : "";
  return apiFetch<AdminRequestEventsOut>(`/admin/request_events${suffix}`);
}

// ——— Admin: backups ———

export interface BackupItem {
  timestamp: string;
  created_at: string;
  pg_size: number;
  redis_size: number;
}

export function listBackups(): Promise<{ items: BackupItem[]; total: number }> {
  return apiFetch<{ items: BackupItem[]; total: number }>("/admin/backups");
}

export function backupNow(): Promise<{
  ok: boolean;
  timestamp?: string | null;
  stderr_tail?: string | null;
}> {
  return apiFetch<{ ok: boolean; timestamp?: string | null; stderr_tail?: string | null }>(
    "/admin/backups/now",
    { method: "POST", body: JSON.stringify({}) },
  );
}

export function restoreBackup(
  timestamp: string,
): Promise<{ accepted: boolean; timestamp: string; note: string }> {
  return apiFetch<{ accepted: boolean; timestamp: string; note: string }>(
    "/admin/backups/restore",
    { method: "POST", body: JSON.stringify({ timestamp }) },
  );
}

// ——— Admin: one-click Lumen update ———

// 后端阶段枚举：保持开放（string）以容忍后端新增 phase 不破坏前端类型。
// UI 侧用一个映射表把已知 phase 翻成中文；未知 phase 直接显示原始 key。
export type UpdatePhase =
  | "prepare"
  | "fetch"
  | "link_shared"
  | "containers"
  | "deps_python"
  | "migrate_db"
  | "deps_node"
  | "build_web"
  | "switch"
  | "restart"
  | "health_post"
  | "cleanup"
  | "rollback";

export interface UpdateStepRecord {
  phase: UpdatePhase | string;
  status: "running" | "done";
  started_at: string;
  ended_at?: string | null;
  rc?: number | null;
  dur_ms?: number | null;
  info?: Record<string, string>;
}

export interface ReleaseInfo {
  id: string;
  created_at: string;
  sha?: string | null;
  branch?: string | null;
  alembic_head_expected?: string | null;
  alembic_head_applied?: string | null;
  is_current: boolean;
  is_previous: boolean;
}

// 扩展现有 AdminUpdateStatusOut（保留旧字段；新字段全部可选，旧消费者仍可工作）。
export interface AdminUpdateStatusOut {
  running: boolean;
  pid?: number | null;
  unit?: string | null;
  started_at?: string | null;
  log_tail: string;
  phases?: UpdateStepRecord[];
  current_release?: ReleaseInfo | null;
  previous_release?: ReleaseInfo | null;
  releases?: ReleaseInfo[];
}

export interface AdminUpdateTriggerOut {
  accepted: boolean;
  pid?: number | null;
  unit?: string | null;
  started_at: string;
  proxy_name?: string | null;
  log_path: string;
  note: string;
}

export interface AdminRollbackOut {
  accepted: boolean;
  target: ReleaseInfo;
  started_at: string;
}

export function getAdminUpdateStatus(): Promise<AdminUpdateStatusOut> {
  return apiFetch<AdminUpdateStatusOut>("/admin/update/status");
}

export function triggerAdminUpdate(): Promise<AdminUpdateTriggerOut> {
  return apiFetch<AdminUpdateTriggerOut>("/admin/update", {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export function listAdminReleases(): Promise<ReleaseInfo[]> {
  // 后端契约：返回 top 10 release。直接返回数组（无 envelope）。
  return apiFetch<ReleaseInfo[]>("/admin/release");
}

export function rollbackAdminRelease(
  release_id: string,
): Promise<AdminRollbackOut> {
  return apiFetch<AdminRollbackOut>("/admin/release/rollback", {
    method: "POST",
    body: JSON.stringify({ release_id }),
  });
}

// SSE 端点。EventSource 不允许自定义 header，但 cookie 由 withCredentials 自动带；
// 后端用 cookie 鉴权 + CSRF 不适用于 GET。
export function adminUpdateStreamUrl(): string {
  return `${API_BASE.replace(/\/$/, "")}/admin/update/stream`;
}

// ——— Me: usage ———

export function getMyUsage(): Promise<UsageOut> {
  return apiFetch<UsageOut>("/me/usage");
}

// ——— Shares ———

export function createShare(
  imageId: string,
  opts: { show_prompt?: boolean; expires_at?: string } = {},
): Promise<ShareOut> {
  return apiFetch<ShareOut>(`/images/${imageId}/share`, {
    method: "POST",
    body: JSON.stringify(opts),
  });
}

export function createMultiShare(
  imageIds: string[],
  opts: { show_prompt?: boolean; expires_at?: string } = {},
): Promise<ShareOut> {
  return apiFetch<ShareOut>("/images/share", {
    method: "POST",
    body: JSON.stringify({
      image_ids: imageIds,
      ...opts,
    }),
  });
}

export function revokeShare(shareId: string): Promise<NoContent> {
  return apiFetchNoContent(`/shares/${shareId}`, { method: "DELETE" });
}

export function listMyShares(): Promise<{ items: ShareOut[] }> {
  return apiFetch<{ items: ShareOut[] }>("/me/shares");
}

// 公共 endpoint：不带 cookie/CSRF。任何 token 泄露也只暴露该图片元信息。
export async function getPublicShare(token: string): Promise<PublicShareOut> {
  const url = `${API_BASE.replace(/\/$/, "")}/share/${encodeURIComponent(token)}`;
  let res: Response;
  try {
    res = await fetch(url, { method: "GET" });
  } catch (err) {
    throw new ApiError({
      code: "network_error",
      message: err instanceof Error ? err.message : "network error",
      status: 0,
    });
  }
  const ct = res.headers.get("content-type") ?? "";
  const isJson = ct.includes("application/json");
  const data: unknown = isJson
    ? await res.json().catch(() => null)
    : await res.text().catch(() => null);
  if (!res.ok) {
    let code = "http_error";
    let message = `HTTP ${res.status}`;
    if (
      data &&
      typeof data === "object" &&
      data !== null &&
      "error" in data &&
      typeof (data as { error: unknown }).error === "object"
    ) {
      const e = (data as { error: { code?: string; message?: string } }).error;
      if (e.code) code = e.code;
      if (e.message) message = e.message;
    }
    throw new ApiError({ code, message, status: res.status, payload: data });
  }
  return data as PublicShareOut;
}

export function publicShareImageUrl(token: string): string {
  return `${API_BASE.replace(/\/$/, "")}/share/${encodeURIComponent(token)}/image`;
}

export function publicShareItemImageUrl(token: string, imageId: string): string {
  return `${API_BASE.replace(/\/$/, "")}/share/${encodeURIComponent(token)}/images/${encodeURIComponent(imageId)}`;
}

// ——————————————————————————————————————————————————————————————
// Invite Links / 系统设置 / 会话管理 / 隐私
// ——————————————————————————————————————————————————————————————

// ——— Admin: invite links ———

export function listInviteLinks(): Promise<{ items: InviteLinkOut[] }> {
  return apiFetch<{ items: InviteLinkOut[] }>("/admin/invite_links");
}

export function createInviteLink(body: {
  email?: string | null;
  expires_in_days?: number;
  role?: "admin" | "member";
}): Promise<InviteLinkOut> {
  const payload: {
    email: string | null;
    expires_in_days: number;
    role: "admin" | "member";
  } = {
    email: body.email ?? null,
    expires_in_days: body.expires_in_days ?? 7,
    role: body.role ?? "member",
  };
  return apiFetch<InviteLinkOut>("/admin/invite_links", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function revokeInviteLink(id: string): Promise<NoContent> {
  return apiFetchNoContent(`/admin/invite_links/${id}`, { method: "DELETE" });
}

// ——— Public: invite info ———
// 不带 cookie；invite token 即凭证。
export async function getPublicInvite(
  token: string,
): Promise<InviteLinkPublicOut> {
  const url = `${API_BASE.replace(/\/$/, "")}/invite/${encodeURIComponent(token)}`;
  let res: Response;
  try {
    res = await fetch(url, { method: "GET" });
  } catch (err) {
    throw new ApiError({
      code: "network_error",
      message: err instanceof Error ? err.message : "network error",
      status: 0,
    });
  }
  const ct = res.headers.get("content-type") ?? "";
  const isJson = ct.includes("application/json");
  const data: unknown = isJson
    ? await res.json().catch(() => null)
    : await res.text().catch(() => null);
  if (!res.ok) {
    let code = "http_error";
    let message = `HTTP ${res.status}`;
    if (
      data &&
      typeof data === "object" &&
      data !== null &&
      "error" in data &&
      typeof (data as { error: unknown }).error === "object"
    ) {
      const e = (data as { error: { code?: string; message?: string } }).error;
      if (e.code) code = e.code;
      if (e.message) message = e.message;
    }
    throw new ApiError({ code, message, status: res.status, payload: data });
  }
  return data as InviteLinkPublicOut;
}

// ——— Admin: system settings ———

export function getSystemSettings(): Promise<SystemSettingsOut> {
  return apiFetch<SystemSettingsOut>("/admin/settings");
}

export function updateSystemSettings(
  items: { key: string; value: string }[],
): Promise<SystemSettingsOut> {
  return apiFetch<SystemSettingsOut>("/admin/settings", {
    method: "PUT",
    body: JSON.stringify({ items }),
  });
}

export function getAdminModels(): Promise<AdminModelsOut> {
  return apiFetch<AdminModelsOut>("/admin/models");
}

export function getAdminContextHealth(): Promise<AdminContextHealthOut> {
  return apiFetch<AdminContextHealthOut>("/admin/context/health");
}

// ——— Admin: providers ———

export function getProviders(): Promise<ProvidersOut> {
  return apiFetch<ProvidersOut>("/admin/providers");
}

export function updateProviders(
  payload: ProviderItemIn[] | { items: ProviderItemIn[]; proxies?: ProviderProxyIn[] },
): Promise<ProvidersOut> {
  const body = Array.isArray(payload)
    ? { items: payload, proxies: [] }
    : { items: payload.items, proxies: payload.proxies ?? [] };
  return apiFetch<ProvidersOut>("/admin/providers", {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

export function probeProviders(
  names?: string[],
): Promise<ProvidersProbeOut> {
  return apiFetch<ProvidersProbeOut>("/admin/providers/probe", {
    method: "POST",
    ...(names ? { body: JSON.stringify({ names }) } : {}),
  });
}

export function getProviderStats(): Promise<ProviderStatsOut> {
  return apiFetch<ProviderStatsOut>("/admin/providers/stats");
}

// ——— Admin: 代理池（独立路由，CRUD 仍走 /admin/providers PUT） ———

export function listAdminProxies(): Promise<import("./types").ProxyListOut> {
  return apiFetch<import("./types").ProxyListOut>("/admin/proxies");
}

export function updateAdminProxies(
  items: ProviderProxyIn[],
): Promise<import("./types").ProxyListOut> {
  return apiFetch<import("./types").ProxyListOut>("/admin/proxies", {
    method: "PUT",
    body: JSON.stringify({ items }),
  });
}

export function restartTelegramBot(): Promise<{ ok: boolean; receivers: number }> {
  return apiFetch<{ ok: boolean; receivers: number }>(
    "/admin/telegram/restart",
    { method: "POST" },
  );
}

export function testAdminProxy(
  name: string,
  target?: string,
): Promise<import("./types").ProxyTestOut> {
  return apiFetch<import("./types").ProxyTestOut>(
    `/admin/proxies/test/${encodeURIComponent(name)}`,
    {
      method: "POST",
      body: JSON.stringify(target ? { target } : {}),
    },
  );
}

export function testAllAdminProxies(
  target?: string,
): Promise<import("./types").ProxyTestOut[]> {
  return apiFetch<import("./types").ProxyTestOut[]>("/admin/proxies/test-all", {
    method: "POST",
    body: JSON.stringify(target ? { target } : {}),
  });
}

// ——— Me: sessions ———

export function listMySessions(): Promise<{ items: SessionOut[] }> {
  return apiFetch<{ items: SessionOut[] }>("/me/sessions");
}

export function revokeMySession(id: string): Promise<NoContent> {
  return apiFetchNoContent(`/me/sessions/${id}`, { method: "DELETE" });
}

// ——— Me: account / data ———

export function deleteMyAccount(): Promise<NoContent> {
  return apiFetchNoContent("/me", { method: "DELETE" });
}

// /me/export 返回 zip 流，apiFetch 默认按 JSON 解析无法处理，所以自己写。
export async function exportMyData(): Promise<Blob> {
  const url = `${API_BASE.replace(/\/$/, "")}/me/export`;
  const headers = new Headers();
  const csrf = readCookie("csrf");
  if (csrf) headers.set("x-csrf-token", csrf);

  let res: Response;
  try {
    res = await fetch(url, {
      method: "POST",
      headers,
      credentials: "include",
    });
  } catch (err) {
    throw new ApiError({
      code: "network_error",
      message: err instanceof Error ? err.message : "network error",
      status: 0,
    });
  }

  if (res.status === 401) {
    handle401();
    throw new ApiError({
      code: "unauthorized",
      message: "未登录或会话已失效",
      status: 401,
    });
  }

  if (!res.ok) {
    let code = "http_error";
    let message = `HTTP ${res.status}`;
    const ct = res.headers.get("content-type") ?? "";
    if (ct.includes("application/json")) {
      const data = (await res.json().catch(() => null)) as unknown;
      if (
        data &&
        typeof data === "object" &&
        data !== null &&
        "error" in data &&
        typeof (data as { error: unknown }).error === "object"
      ) {
        const e = (data as { error: { code?: string; message?: string } })
          .error;
        if (e.code) code = e.code;
        if (e.message) message = e.message;
      }
      throw new ApiError({ code, message, status: res.status, payload: data });
    }
    throw new ApiError({ code, message, status: res.status });
  }

  return res.blob();
}
