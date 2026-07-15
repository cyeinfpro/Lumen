import {
  API_BASE,
  ApiError,
  apiFetch,
  apiFetchNoContent,
  ensureCsrfToken,
  handle401,
  refreshCsrfToken,
} from "./api/http";
import type { NoContent } from "./api/http";
import type {
  BackendCompletion,
  BackendGeneration,
  BackendImageMeta,
  TaskStatus,
} from "./api/tasks";
import type { WorkflowRun } from "./api/workflows";
import type {
  Intent,
  ImageParams,
  StructuredAttachment,
  AllowedEmailOut,
  AdminRequestEventsOut,
  AdminUserHistoryOut,
  AdminContextHealthOut,
  AdminModelsOut,
  AdminUserOut,
  ShareOut,
  InviteLinkOut,
  InviteLinkPublicOut,
  SystemSettingsOut,
  ProviderItemIn,
  ProviderItemOut,
  ProviderProxyIn,
  ProvidersOut,
  ProvidersProbeOut,
  ProviderStatsOut,
  SessionOut,
  ApiSupplierProbeOut,
  ApiSupplierTemplateIn,
  ApiSupplierTemplateListOut,
  ApiSupplierTemplateOut,
  ApiSupplierTemplatePublicListOut,
  ApiKeyVerifyOut,
  ByokSettingsOut,
  ByokSettingsPatchIn,
  TelegramLinkCodeOut,
  UserApiCredentialListOut,
  UserApiCredentialOut,
  AdminBillingBootstrapIn,
  AdminBillingOverviewOut,
  AdminOrphanHoldOut,
  AdminPricingBulkIn,
  AdminWalletAuditOut,
  AdminRedemptionBatchRedownloadOut,
  AdminRedemptionCodeCreateOut,
  AdminRedemptionCodeListOut,
  AdminRedemptionUsageListOut,
  AdminWalletDetailOut,
  AdminWalletListOut,
  BillingSnapshotOut,
  PricingRuleUpsertIn,
  PricingRulesOut,
  RedemptionOut,
  RedemptionUsageListOut,
  WalletOut,
  WalletTransactionListOut,
  WalletTransactionOut,
  VideoCreateIn,
  VideoGenerationOut,
  VideoPromptEnhanceIn,
  VideoProvidersOut,
  VideoProvidersUpdateIn,
  RecommendedErrorAction,
} from "./types";
import { uuid } from "./utils";
export { API_BASE, ApiError, apiFetch, apiFetchNoContent } from "./api/http";
export type { NoContent } from "./api/http";
export * from "./api/tasks";
export * from "./api/storyboards";
export * from "./api/workflows";

// —————————————————— 领域接口 ——————————————————

function createIdempotencyKey(): string {
  if (
    typeof crypto !== "undefined" &&
    typeof crypto.randomUUID === "function"
  ) {
    try {
      return crypto.randomUUID();
    } catch {
      /* fall back to RFC 4122 v4 helper */
    }
  }
  return uuid();
}

export interface AuthUser {
  id: string;
  email?: string;
  name?: string;
  account_mode?: "wallet" | "byok";
  role?: "admin" | "member";
  default_system_prompt_id?: string | null;
  runtime_defaults?: {
    fast?: boolean;
    upload_max_source_bytes?: number;
    canvas_enabled?: boolean;
    nav_visibility?: {
      studio?: boolean;
      video?: boolean;
      projects?: boolean;
      assets?: boolean;
    };
  };
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

export function listPublicApiSuppliers(): Promise<ApiSupplierTemplatePublicListOut> {
  return apiFetch<ApiSupplierTemplatePublicListOut>("/auth/api-suppliers");
}

export function verifyApiKey(
  supplier_id: string,
  api_key: string,
): Promise<ApiKeyVerifyOut> {
  return apiFetch<ApiKeyVerifyOut>("/auth/api-key/verify", {
    method: "POST",
    body: JSON.stringify({ supplier_id, api_key }),
  });
}

export function signupByok(
  email: string,
  password: string,
  verification_token: string,
  display_name = "",
): Promise<AuthUser> {
  return apiFetch<AuthUser>("/auth/signup/byok", {
    method: "POST",
    body: JSON.stringify({ email, password, display_name, verification_token }),
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
  memory_disabled?: boolean;
  active_scope_id?: string | null;
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
  attachments?: StructuredAttachment[];
  input_images?: string[];
  source?: string;
  action_source?: string;
  trace_id?: string;
  // 局部修改 (inpaint) mask 的 image_id（已通过 /images/upload 上传，
  // RGBA PNG，alpha=0 处为要重画区域）。仅 image_to_image 时有意义。
  mask_image_id?: string;
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
  display_url?: string | null;
  preview_url?: string | null;
  thumb_url?: string | null;
  mime?: string;
  metadata_jsonb?: Record<string, unknown> | null;
}

export interface UploadImageOptions {
  signal?: AbortSignal;
  purpose?: "inpaint_mask";
}

export function uploadImage(
  file: File,
  opts: UploadImageOptions = {},
): Promise<UploadedImage> {
  const fd = new FormData();
  fd.append("file", file);
  if (opts.purpose) fd.append("purpose", opts.purpose);
  return apiFetch<UploadedImage>("/images/upload", {
    method: "POST",
    signal: opts.signal,
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

// —— 视频生成 ——

export function createVideoGeneration(
  body: Omit<VideoCreateIn, "idempotency_key"> & { idempotency_key?: string },
): Promise<VideoGenerationOut> {
  return apiFetch<VideoGenerationOut>("/videos/generations", {
    method: "POST",
    body: JSON.stringify({
      ...body,
      idempotency_key: body.idempotency_key ?? createIdempotencyKey(),
    }),
  });
}

export function cancelVideoGeneration(id: string): Promise<VideoGenerationOut> {
  return apiFetch<VideoGenerationOut>(
    `/videos/generations/${encodeURIComponent(id)}/cancel`,
    { method: "POST" },
  );
}

export function retryVideoGeneration(id: string): Promise<VideoGenerationOut> {
  return apiFetch<VideoGenerationOut>(
    `/videos/generations/${encodeURIComponent(id)}/retry`,
    { method: "POST" },
  );
}

export function deleteVideo(id: string): Promise<NoContent> {
  return apiFetchNoContent(`/videos/${encodeURIComponent(id)}`, { method: "DELETE" });
}

export function videoBinaryUrl(videoId: string): string {
  return `${API_BASE.replace(/\/$/, "")}/videos/${encodeURIComponent(videoId)}/binary`;
}

export function videoDownloadUrl(videoId: string): string { return `${videoBinaryUrl(videoId)}?download=1`; }

export function videoPosterUrl(videoId: string): string {
  return `${API_BASE.replace(/\/$/, "")}/videos/${encodeURIComponent(videoId)}/poster`;
}

// —— 任务 ——

export type TaskKind = "generations" | "completions";
export type TaskResponse<K extends TaskKind = TaskKind> =
  K extends "generations" ? BackendGeneration : BackendCompletion;

export interface TaskActionResponse {
  status: TaskStatus | "canceling";
  cancel_requested?: boolean;
}

export interface TaskItemResponse {
  kind: "generation" | "completion";
  id: string;
  message_id: string;
  status: TaskStatus;
  progress_stage: string;
  stage?: string | null;
  started_at: string | null;
  date?: string | null;
  cursor?: string | null;
  created_at?: string | null;
  finished_at?: string | null;
  source?: string | null;
  action_source?: string | null;
  trace_id?: string | null;
  conversation_id?: string | null;
  project_id?: string | null;
  workflow_type?: string | null;
  workflow_step_key?: string | null;
  queue_lane?: string | null;
  pixel_count?: number | null;
  size_bucket?: string | null;
  cost_class?: string | null;
  queue_wait_ms?: number | null;
  queue_position?: number | null;
  substage?: string | null;
  retrying?: boolean;
  waiting_provider?: boolean;
  cancelled?: boolean;
  title?: string | null;
  prompt?: string | null;
  source_image_id?: string | null;
  error_code?: string | null;
  error_message?: string | null;
  retryable?: boolean;
  recommended_actions?: RecommendedErrorAction[];
  thumb_url?: string | null;
}

export interface TaskListResponse {
  items: TaskItemResponse[];
  next_cursor?: string | null;
}

export function getTask(
  kind: "generations",
  id: string,
  opts?: { signal?: AbortSignal },
): Promise<BackendGeneration>;
export function getTask(
  kind: "completions",
  id: string,
  opts?: { signal?: AbortSignal },
): Promise<BackendCompletion>;
export function getTask(
  kind: TaskKind,
  id: string,
  opts?: { signal?: AbortSignal },
): Promise<TaskResponse>;
export function getTask(
  kind: TaskKind,
  id: string,
  opts: { signal?: AbortSignal } = {},
): Promise<TaskResponse> {
  const seg = kind === "generations" ? "generations" : "completions";
  return apiFetch<TaskResponse>(`/${seg}/${id}`, {
    signal: opts.signal,
  });
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
  kind?: "all" | "generation" | "completion";
  source?: string;
  conversation_id?: string;
  project_id?: string;
  date?: string;
  cursor?: string;
  error_code?: string;
  retryable?: boolean;
  limit?: number;
}

export function listTasks(
  opts: TaskListOpts = {},
  requestOpts: { signal?: AbortSignal } = {},
): Promise<TaskListResponse> {
  const q = new URLSearchParams();
  if (opts.status) q.set("status", opts.status);
  if (opts.mine) q.set("mine", "1");
  if (opts.kind && opts.kind !== "all") q.set("kind", opts.kind);
  if (opts.source) q.set("source", opts.source);
  if (opts.conversation_id) q.set("conversation_id", opts.conversation_id);
  if (opts.project_id) q.set("project_id", opts.project_id);
  if (opts.date) q.set("date", opts.date);
  if (opts.cursor) q.set("cursor", opts.cursor);
  if (opts.error_code) q.set("error_code", opts.error_code);
  if (opts.retryable != null) q.set("retryable", opts.retryable ? "1" : "0");
  if (opts.limit != null) q.set("limit", String(opts.limit));
  const suffix = q.toString() ? `?${q.toString()}` : "";
  return apiFetch<TaskListResponse>(`/tasks${suffix}`, {
    signal: requestOpts.signal,
  });
}

// 用户级中心任务列表：返回当前登录用户**所有**会话的进行中任务完整字段，
// 用于前端启动 / SSE 重连后一次性 hydrate，避免 GlobalTaskTray 按会话碎片化。
export interface ActiveTasksResponse {
  generations: BackendGeneration[];
  completions: BackendCompletion[];
}

export function listMyActiveTasks(
  opts: { signal?: AbortSignal } = {},
): Promise<ActiveTasksResponse> {
  return apiFetch<ActiveTasksResponse>(`/tasks/mine/active`, {
    signal: opts.signal,
  });
}

// —— SSE URL 构造（供 useSSE 使用） ——

export function sseUrl(channels: string[], lastEventId?: string | null): string {
  const q = new URLSearchParams({ channels: [...channels].sort().join(",") });
  if (lastEventId) q.set("last_event_id", lastEventId);
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

async function streamApiErrorFromResponse(
  res: Response,
  fallbackCode: string,
): Promise<ApiError> {
  const contentType = res.headers.get("content-type") ?? "";
  const payload = contentType.includes("application/json")
    ? await res.json().catch(() => null)
    : await res.text().catch(() => null);
  let code = fallbackCode;
  let message = `HTTP ${res.status}`;

  if (
    payload &&
    typeof payload === "object" &&
    "error" in payload &&
    typeof (payload as { error: unknown }).error === "object" &&
    (payload as { error: unknown }).error !== null
  ) {
    const err = (payload as { error: { code?: unknown; message?: unknown } }).error;
    if (typeof err.code === "string" && err.code.trim()) code = err.code;
    if (typeof err.message === "string" && err.message.trim()) {
      message = err.message;
    }
  } else if (
    payload &&
    typeof payload === "object" &&
    "detail" in payload
  ) {
    const detail = (payload as { detail?: unknown }).detail;
    if (typeof detail === "string" && detail.trim()) {
      message = detail;
    } else if (
      detail &&
      typeof detail === "object" &&
      "error" in detail &&
      typeof (detail as { error?: unknown }).error === "object" &&
      (detail as { error?: unknown }).error !== null
    ) {
      const err = (detail as { error: { code?: unknown; message?: unknown } }).error;
      if (typeof err.code === "string" && err.code.trim()) code = err.code;
      if (typeof err.message === "string" && err.message.trim()) {
        message = err.message;
      }
    } else if (Array.isArray(detail) && detail.length > 0) {
      const first = detail[0];
      if (first && typeof first === "object" && "msg" in first) {
        const msg = (first as { msg?: unknown }).msg;
        if (typeof msg === "string" && msg.trim()) message = msg;
      }
    }
  } else if (typeof payload === "string" && payload.trim()) {
    message = payload.trim();
  }
  if (res.status === 413 && message === `HTTP ${res.status}`) {
    code = "request_too_large";
    message = "参考素材过大，请减少素材后重试";
  }

  return new ApiError({ code, message, status: res.status, payload });
}

function promptEnhanceStreamErrorMessage(code: string): string {
  switch (code) {
    case "timeout":
      return "上游长时间没有返回内容，已自动停止。请稍后重试或减少参考素材。";
    case "upstream_error":
      return "上游暂时不可用，请稍后重试。";
    case "billing_failed":
      return "扣费结算失败，已停止本次优化。";
    case "internal":
      return "服务内部错误，请稍后重试。";
    default:
      return code;
  }
}

async function streamPromptEnhancement(
  path: string,
  body: unknown,
  onDelta: (text: string) => void,
  signal?: AbortSignal,
): Promise<void> {
  const url = `${API_BASE.replace(/\/$/, "")}${path}`;
  const doFetch = async (csrf: string | null): Promise<Response> =>
    fetch(url, {
      method: "POST",
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        ...(csrf ? { "X-CSRF-Token": csrf } : {}),
      },
      body: JSON.stringify(body),
      signal,
    });
  let res: Response;
  try {
    res = await doFetch(await ensureCsrfToken());
  } catch (err) {
    if (signal?.aborted) throw err;
    throw new ApiError({
      code: "network_error",
      message: err instanceof Error ? err.message : "network error",
      status: 0,
    });
  }
  if (res.status === 401) {
    handle401();
    throw new ApiError({ code: "unauthorized", message: "未登录", status: 401 });
  }
  if (res.status === 403) {
    const err = await streamApiErrorFromResponse(res, "enhance_failed");
    if (err.code !== "csrf_failed") throw err;
    const fresh = await refreshCsrfToken().catch(() => null);
    if (!fresh) throw err;
    try {
      res = await doFetch(fresh);
    } catch (retryErr) {
      if (signal?.aborted) throw retryErr;
      throw new ApiError({
        code: "network_error",
        message: retryErr instanceof Error ? retryErr.message : "network error",
        status: 0,
      });
    }
    if (res.status === 401) {
      handle401();
      throw new ApiError({ code: "unauthorized", message: "未登录", status: 401 });
    }
  }
  if (!res.ok) {
    throw await streamApiErrorFromResponse(res, "enhance_failed");
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
      if (evt.error) {
        throw new ApiError({
          code: evt.error,
          message: promptEnhanceStreamErrorMessage(evt.error),
          status: 502,
        });
      }
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
  try {
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
  } catch (err) {
    try {
      await reader.cancel();
    } catch {
      // ignore
    }
    throw err;
  }
  if (streamDone && hasText) return;
  if (!hasText) {
    throw new ApiError({ code: "enhance_empty_response", message: "empty response", status: 502 });
  }
}

export async function enhancePrompt(
  text: string,
  onDelta: (text: string) => void,
  signal?: AbortSignal,
): Promise<void> {
  return streamPromptEnhancement("/prompts/enhance", { text }, onDelta, signal);
}

export async function enhanceVideoPrompt(
  body: VideoPromptEnhanceIn,
  onDelta: (text: string) => void,
  signal?: AbortSignal,
): Promise<void> {
  return streamPromptEnhancement("/prompts/video/enhance", body, onDelta, signal);
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

export function getAdminUserHistory(
  userId: string,
  params: { limit?: number } = {},
): Promise<AdminUserHistoryOut> {
  const q = new URLSearchParams();
  if (params.limit != null) q.set("limit", String(params.limit));
  const suffix = q.toString() ? `?${q.toString()}` : "";
  return apiFetch<AdminUserHistoryOut>(
    `/admin/users/${encodeURIComponent(userId)}/history${suffix}`,
  );
}

export function setAdminUserPassword(
  userId: string,
  password: string,
): Promise<{ ok: boolean }> {
  return apiFetch<{ ok: boolean }>(
    `/admin/users/${encodeURIComponent(userId)}/password`,
    {
      method: "PATCH",
      body: JSON.stringify({ password }),
    },
  );
}

export function deleteAdminUser(userId: string): Promise<{ ok: boolean }> {
  return apiFetch<{ ok: boolean }>(
    `/admin/users/${encodeURIComponent(userId)}`,
    { method: "DELETE" },
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

export interface UpdateReleaseOut {
  tag: string;
  name?: string | null;
  body_md: string;
  body_html: string;
  html_url?: string | null;
  published_at?: string | null;
  is_prerelease: boolean;
  assets: Record<string, unknown>[];
}

export interface UpdateCacheOut {
  cached: boolean;
  fetched_at?: string | null;
  stale: boolean;
  ttl_remaining_sec: number;
}

export interface AdminUpdateCheckOut {
  current_version: string;
  latest_version: string;
  has_update: boolean | null;
  release?: UpdateReleaseOut | null;
  cache: UpdateCacheOut;
  channel: string;
  resolved_image_tag: string;
  build_type: string;
  warning?: string | null;
  warm_pull?: {
    state?: string;
    tag?: string;
  };
}

export interface AdminUpdateVersionOut {
  version: string;
  image_tag: string;
  release_id?: string | null;
  sha?: string | null;
  channel: string;
  build_type: string;
  degraded: string[];
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

export interface SystemMaintenanceOut {
  running: boolean;
  phase?: string | null;
  started_at?: string | null;
  target_tag?: string | null;
  estimated_remaining_min: number;
}

export interface AdminUpdateTriggerOut {
  accepted: boolean;
  pid?: number | null;
  unit?: string | null;
  started_at: string;
  proxy_name?: string | null;
  log_path: string;
  note: string;
  target_tag?: string | null;
  idempotency_key?: string | null;
  replayed?: boolean;
}

export interface AdminUpdateTriggerIn {
  target_tag?: string | null;
  force_redeploy?: boolean;
  channel?: string | null;
  confirm_update?: boolean;
  confirmed_target_tag?: string | null;
}

export interface AdminRollbackOut {
  accepted: boolean;
  target: ReleaseInfo;
  started_at: string;
}

export function getAdminUpdateStatus(): Promise<AdminUpdateStatusOut> {
  return apiFetch<AdminUpdateStatusOut>("/admin/update/status");
}

export function getSystemMaintenance(): Promise<SystemMaintenanceOut> {
  return apiFetch<SystemMaintenanceOut>("/system/maintenance");
}

export function getAdminUpdateVersion(): Promise<AdminUpdateVersionOut> {
  return apiFetch<AdminUpdateVersionOut>("/admin/update/version");
}

export function checkAdminUpdate(force = false): Promise<AdminUpdateCheckOut> {
  const suffix = force ? "?force=true" : "";
  return apiFetch<AdminUpdateCheckOut>(`/admin/update/check${suffix}`);
}

export function triggerAdminUpdate(
  body: AdminUpdateTriggerIn = {},
): Promise<AdminUpdateTriggerOut> {
  return apiFetch<AdminUpdateTriggerOut>("/admin/update", {
    method: "POST",
    body: JSON.stringify(body),
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

export function rollbackPreviousAdminRelease(): Promise<AdminRollbackOut> {
  return apiFetch<AdminRollbackOut>("/admin/update/rollback-previous", {
    method: "POST",
    body: JSON.stringify({}),
  });
}

// SSE 端点。EventSource 不允许自定义 header，但 cookie 由 withCredentials 自动带；
// 后端用 cookie 鉴权 + CSRF 不适用于 GET。
export function adminUpdateStreamUrl(): string {
  return `${API_BASE.replace(/\/$/, "")}/admin/update/stream`;
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

const SYSTEM_SETTINGS_BASE = "/admin/settings";

export function getSystemSettings(): Promise<SystemSettingsOut> {
  return apiFetch<SystemSettingsOut>(SYSTEM_SETTINGS_BASE);
}

export function updateSystemSettings(
  items: { key: string; value: string }[],
): Promise<SystemSettingsOut> {
  return apiFetch<SystemSettingsOut>(SYSTEM_SETTINGS_BASE, {
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

const PROVIDERS_BASE = "/admin/providers";

export function getProviders(): Promise<ProvidersOut> {
  return apiFetch<ProvidersOut>(PROVIDERS_BASE);
}

export async function updateProviders(
  payload: ProviderItemIn[] | { items: ProviderItemIn[]; proxies?: ProviderProxyIn[] },
): Promise<ProvidersOut> {
  const body = Array.isArray(payload)
    ? { items: payload, proxies: [] }
    : { items: payload.items, proxies: payload.proxies ?? [] };
  return apiFetch<ProvidersOut>(PROVIDERS_BASE, {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

export function patchProviderEnabled(
  name: string,
  enabled: boolean,
): Promise<ProviderItemOut> {
  return apiFetch<ProviderItemOut>(
    `${PROVIDERS_BASE}/${encodeURIComponent(name)}/enabled`,
    {
      method: "PATCH",
      body: JSON.stringify({ enabled }),
    },
  );
}

export function probeProviders(
  names?: string[],
): Promise<ProvidersProbeOut> {
  return apiFetch<ProvidersProbeOut>(`${PROVIDERS_BASE}/probe`, {
    method: "POST",
    ...(names ? { body: JSON.stringify({ names }) } : {}),
  });
}

export function getProviderStats(): Promise<ProviderStatsOut> {
  return apiFetch<ProviderStatsOut>(`${PROVIDERS_BASE}/stats`);
}

export function getVideoProviders(): Promise<VideoProvidersOut> {
  return apiFetch<VideoProvidersOut>(`${PROVIDERS_BASE}/video`);
}

export function updateVideoProviders(
  body: VideoProvidersUpdateIn,
): Promise<VideoProvidersOut> {
  return apiFetch<VideoProvidersOut>(`${PROVIDERS_BASE}/video`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

// ——— BYOK ———

export function getByokSettings(): Promise<ByokSettingsOut> {
  return apiFetch<ByokSettingsOut>("/admin/byok-settings");
}

export function patchByokSettings(
  body: ByokSettingsPatchIn,
): Promise<ByokSettingsOut> {
  return apiFetch<ByokSettingsOut>("/admin/byok-settings", {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export function listApiSuppliers(): Promise<ApiSupplierTemplateListOut> {
  return apiFetch<ApiSupplierTemplateListOut>("/admin/api-suppliers");
}

export function createApiSupplier(
  body: ApiSupplierTemplateIn,
): Promise<ApiSupplierTemplateOut> {
  return apiFetch<ApiSupplierTemplateOut>("/admin/api-suppliers", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function patchApiSupplier(
  id: string,
  body: Partial<ApiSupplierTemplateIn>,
): Promise<ApiSupplierTemplateOut> {
  return apiFetch<ApiSupplierTemplateOut>(`/admin/api-suppliers/${id}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export function probeApiSupplier(
  id: string,
  api_key: string,
): Promise<ApiSupplierProbeOut> {
  return apiFetch<ApiSupplierProbeOut>(`/admin/api-suppliers/${id}/probe`, {
    method: "POST",
    body: JSON.stringify({ api_key }),
  });
}

export function listMyApiCredentials(): Promise<UserApiCredentialListOut> {
  return apiFetch<UserApiCredentialListOut>("/me/api-credentials");
}

export function listBindableApiSuppliers(): Promise<ApiSupplierTemplatePublicListOut> {
  return apiFetch<ApiSupplierTemplatePublicListOut>("/me/api-credentials/suppliers");
}

export function putMyApiCredential(
  supplier_id: string,
  api_key: string,
): Promise<UserApiCredentialOut> {
  return apiFetch<UserApiCredentialOut>(`/me/api-credentials/${supplier_id}`, {
    method: "PUT",
    body: JSON.stringify({ api_key }),
  });
}

export function probeMyApiCredential(credential_id: string): Promise<UserApiCredentialOut> {
  return apiFetch<UserApiCredentialOut>(
    `/me/api-credentials/${credential_id}/probe`,
    { method: "POST" },
  );
}

export function revokeMyApiCredential(credential_id: string): Promise<{ ok: boolean }> {
  return apiFetch<{ ok: boolean }>(`/me/api-credentials/${credential_id}`, {
    method: "DELETE",
  });
}

export function createTelegramLinkCode(): Promise<TelegramLinkCodeOut> {
  return apiFetch<TelegramLinkCodeOut>("/me/telegram/link-code", {
    method: "POST",
  });
}

// ——— Billing / Wallet ———

export function getMyWallet(): Promise<WalletOut> {
  return apiFetch<WalletOut>("/me/wallet");
}

export function getMyBillingSnapshot(): Promise<BillingSnapshotOut> {
  return apiFetch<BillingSnapshotOut>("/me/billing/snapshot");
}

export function listMyWalletTransactions(
  opts: { cursor?: string | null; limit?: number; kind?: string | null } = {},
): Promise<WalletTransactionListOut> {
  const qs = new URLSearchParams();
  if (opts.cursor) qs.set("cursor", opts.cursor);
  if (opts.limit != null) qs.set("limit", String(opts.limit));
  if (opts.kind) qs.set("kind", opts.kind);
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return apiFetch<WalletTransactionListOut>(`/me/wallet/transactions${suffix}`);
}

export function redeemCode(code: string): Promise<RedemptionOut> {
  return apiFetch<RedemptionOut>("/me/redemptions", {
    method: "POST",
    headers: { "Idempotency-Key": createIdempotencyKey() },
    body: JSON.stringify({ code }),
  });
}

export function listMyRedemptions(
  opts: { cursor?: string | null; limit?: number } = {},
): Promise<RedemptionUsageListOut> {
  const qs = new URLSearchParams();
  if (opts.cursor) qs.set("cursor", opts.cursor);
  if (opts.limit != null) qs.set("limit", String(opts.limit));
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return apiFetch<RedemptionUsageListOut>(`/me/redemptions${suffix}`);
}

export function getPricing(): Promise<PricingRulesOut> {
  return apiFetch<PricingRulesOut>("/me/pricing");
}

export function getAdminPricing(): Promise<PricingRulesOut> {
  return apiFetch<PricingRulesOut>("/admin/pricing");
}

export function getAdminBillingOverview(): Promise<AdminBillingOverviewOut> {
  return apiFetch<AdminBillingOverviewOut>("/admin/billing/overview");
}

export function bootstrapAdminBilling(body: AdminBillingBootstrapIn): Promise<AdminBillingOverviewOut> {
  return apiFetch<AdminBillingOverviewOut>("/admin/billing/bootstrap", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function rotateAdminRedemptionSecret(): Promise<AdminBillingOverviewOut> {
  return apiFetch<AdminBillingOverviewOut>("/admin/billing/redemption_secret:rotate", {
    method: "POST",
  });
}

export function runAdminWalletAudit(): Promise<AdminWalletAuditOut> {
  return apiFetch<AdminWalletAuditOut>("/admin/billing/wallet_audit");
}

export function listAdminOrphanHolds(
  opts: { min_age_minutes?: number; limit?: number } = {},
): Promise<AdminOrphanHoldOut[]> {
  const qs = new URLSearchParams();
  if (opts.min_age_minutes != null) qs.set("min_age_minutes", String(opts.min_age_minutes));
  if (opts.limit != null) qs.set("limit", String(opts.limit));
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return apiFetch<AdminOrphanHoldOut[]>(`/admin/billing/orphan_holds${suffix}`);
}

export function releaseAdminOrphanHold(txId: string): Promise<WalletTransactionOut> {
  return apiFetch<WalletTransactionOut>(
    `/admin/billing/holds/${encodeURIComponent(txId)}:release`,
    { method: "POST" },
  );
}

export function updateAdminPricing(
  items: PricingRuleUpsertIn[],
  opts: { image_size_thresholds?: Record<string, number>; force?: boolean } = {},
): Promise<PricingRulesOut> {
  return apiFetch<PricingRulesOut>("/admin/pricing", {
    method: "PUT",
    body: JSON.stringify({ items, ...opts }),
  });
}

export function bulkUpdateAdminPricing(body: AdminPricingBulkIn): Promise<PricingRulesOut> {
  return apiFetch<PricingRulesOut>("/admin/billing/pricing/bulk", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function importOpenAiPricing(content: string, rate = 1): Promise<PricingRulesOut> {
  return apiFetch<PricingRulesOut>("/admin/pricing/import_openai", {
    method: "POST",
    body: JSON.stringify({ content, rate }),
  });
}

export function listAdminRedemptionCodes(opts: {
  status?: "all" | "active" | "revoked" | "expired" | "exhausted";
  batch_id?: string | null;
  q?: string | null;
  cursor?: string | null;
  limit?: number;
} = {}): Promise<AdminRedemptionCodeListOut> {
  const qs = new URLSearchParams();
  if (opts.status) qs.set("status", opts.status);
  if (opts.batch_id) qs.set("batch_id", opts.batch_id);
  if (opts.q) qs.set("q", opts.q);
  if (opts.cursor) qs.set("cursor", opts.cursor);
  if (opts.limit != null) qs.set("limit", String(opts.limit));
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return apiFetch<AdminRedemptionCodeListOut>(`/admin/redemption_codes${suffix}`);
}

export function createAdminRedemptionCodes(body: {
  amount_rmb: string;
  count: number;
  max_redemptions?: number;
  expires_at?: string | null;
  note?: string | null;
}): Promise<AdminRedemptionCodeCreateOut> {
  return apiFetch<AdminRedemptionCodeCreateOut>("/admin/redemption_codes", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function revokeAdminRedemptionCode(
  id: string,
): Promise<import("./types").AdminRedemptionCodeOut> {
  return apiFetch<import("./types").AdminRedemptionCodeOut>(
    `/admin/redemption_codes/${encodeURIComponent(id)}:revoke`,
    { method: "POST" },
  );
}

export function listAdminRedemptionCodeUsage(id: string): Promise<AdminRedemptionUsageListOut> {
  return apiFetch<AdminRedemptionUsageListOut>(
    `/admin/redemption_codes/${encodeURIComponent(id)}/usage`,
  );
}

export function revokeAdminRedemptionBatch(batchId: string): Promise<AdminRedemptionCodeListOut> {
  return apiFetch<AdminRedemptionCodeListOut>(
    `/admin/redemption_codes/batches/${encodeURIComponent(batchId)}:revoke`,
    { method: "POST" },
  );
}

export function adminRedemptionBatchCsvUrl(batchId: string, token: string): string {
  return `${API_BASE}/admin/redemption_codes/batches/${encodeURIComponent(batchId)}.csv?download_token=${encodeURIComponent(token)}`;
}

export function adminRedemptionBatchTxtUrl(batchId: string, token: string): string {
  return `${API_BASE}/admin/redemption_codes/batches/${encodeURIComponent(batchId)}.txt?download_token=${encodeURIComponent(token)}`;
}

export function redownloadAdminRedemptionBatch(batchId: string): Promise<AdminRedemptionBatchRedownloadOut> {
  return apiFetch<AdminRedemptionBatchRedownloadOut>(
    `/admin/redemption_codes/batches/${encodeURIComponent(batchId)}/redownload`,
    { method: "POST" },
  );
}

export function listAdminWallets(
  q?: string,
  mode: "wallet" | "byok" | "all" = "wallet",
  opts: { cursor?: string | null; limit?: number } = {},
): Promise<AdminWalletListOut> {
  const qs = new URLSearchParams();
  if (q) qs.set("q", q);
  qs.set("mode", mode);
  if (opts.cursor) qs.set("cursor", opts.cursor);
  if (opts.limit != null) qs.set("limit", String(opts.limit));
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return apiFetch<AdminWalletListOut>(`/admin/wallets${suffix}`);
}

export function getAdminWalletDetail(userId: string): Promise<AdminWalletDetailOut> {
  return apiFetch<AdminWalletDetailOut>(`/admin/wallets/${encodeURIComponent(userId)}`);
}

export function listAdminWalletTransactions(
  userId: string,
  opts: {
    cursor?: string | null;
    limit?: number;
    kind?: string | null;
    ref_type?: string | null;
    ref_id?: string | null;
  } = {},
): Promise<WalletTransactionListOut> {
  const qs = new URLSearchParams();
  if (opts.cursor) qs.set("cursor", opts.cursor);
  if (opts.limit != null) qs.set("limit", String(opts.limit));
  if (opts.kind) qs.set("kind", opts.kind);
  if (opts.ref_type) qs.set("ref_type", opts.ref_type);
  if (opts.ref_id) qs.set("ref_id", opts.ref_id);
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return apiFetch<WalletTransactionListOut>(
    `/admin/wallets/${encodeURIComponent(userId)}/transactions${suffix}`,
  );
}

export function adjustAdminWallet(
  userId: string,
  amount_rmb_signed: string,
  reason: string,
): Promise<WalletTransactionOut> {
  return apiFetch<WalletTransactionOut>(`/admin/wallets/${userId}:adjust`, {
    method: "POST",
    body: JSON.stringify({ amount_rmb_signed, reason }),
  });
}

export function setAdminAccountMode(
  userId: string,
  mode: "wallet" | "byok",
  on_residual_balance: "freeze" | "zero" = "freeze",
): Promise<import("./types").AdminWalletOut> {
  return apiFetch<import("./types").AdminWalletOut>(
    `/admin/users/${userId}:set_account_mode`,
    {
      method: "POST",
      body: JSON.stringify({ mode, on_residual_balance }),
    },
  );
}

// ——— Account memory ———

export type MemoryType = "profile" | "preference" | "avoid" | "project";

export interface MemoryItemOut {
  id: string;
  type: MemoryType;
  content: string;
  source_message_id?: string | null;
  source_excerpt?: string | null;
  source: "explicit" | "auto" | "manual";
  confidence: number;
  pinned: boolean;
  disabled: boolean;
  positive_signal: number;
  negative_signal: number;
  superseded_by?: string | null;
  last_used_at?: string | null;
  scope_id: string;
  last_confirmed_at?: string | null;
  created_at: string;
  updated_at: string;
}

export interface MemoryStagingOut {
  id: string;
  type: MemoryType;
  content: string;
  source_message_id?: string | null;
  source_excerpt?: string | null;
  confidence: number;
  scope_id: string;
  recommended_scope_id?: string | null;
  decision: "pending" | "accepted" | "rejected";
  expires_at: string;
  created_at: string;
}

export interface MemoryScopeOut {
  id: string;
  name: string;
  emoji?: string | null;
  is_default: boolean;
  count: number;
  created_at: string;
}

export interface MemorySettingsOut {
  paused: boolean;
  disabled: boolean;
  extraction_threshold: number;
  onboarding_seen: number;
  confirmation_enabled: boolean;
  embedding_available: boolean;
}

export interface MemoryAuditOut {
  id: string;
  event_type: string;
  memory_id?: string | null;
  staging_id?: string | null;
  old_content?: string | null;
  new_content?: string | null;
  source_message_id?: string | null;
  details: Record<string, unknown>;
  created_at: string;
}

export interface MemoryListOut {
  items: MemoryItemOut[];
}

export interface MemoryStagingListOut {
  items: MemoryStagingOut[];
}

export interface MemoryTimelineOut {
  items: MemoryAuditOut[];
  next_cursor?: string | null;
}

export interface MemoryPatchIn {
  type?: MemoryType;
  content?: string;
  pinned?: boolean;
  disabled?: boolean;
  scope_id?: string | null;
}

export function getMemorySettings(): Promise<MemorySettingsOut> {
  return apiFetch<MemorySettingsOut>("/me/memory-settings");
}

export function patchMemorySettings(
  body: Partial<Pick<MemorySettingsOut, "paused" | "disabled" | "confirmation_enabled">>,
): Promise<MemorySettingsOut> {
  return apiFetch<MemorySettingsOut>("/me/memory-settings", {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export function markMemoryOnboardingSeen(flag: number): Promise<MemorySettingsOut> {
  return apiFetch<MemorySettingsOut>("/me/onboarding-seen", {
    method: "PATCH",
    body: JSON.stringify({ flag }),
  });
}

export function listMemories(opts: {
  type?: MemoryType;
  pinned?: boolean;
  disabled?: boolean;
  scope_id?: string;
} = {}): Promise<MemoryListOut> {
  const qs = new URLSearchParams();
  if (opts.type) qs.set("type", opts.type);
  if (opts.pinned != null) qs.set("pinned", String(opts.pinned));
  if (opts.disabled != null) qs.set("disabled", String(opts.disabled));
  if (opts.scope_id) qs.set("scope_id", opts.scope_id);
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return apiFetch<MemoryListOut>(`/me/memories${suffix}`);
}

export function createMemory(body: {
  type: MemoryType;
  content: string;
  source_excerpt?: string | null;
  pinned?: boolean;
  scope_id?: string | null;
}): Promise<MemoryItemOut> {
  return apiFetch<MemoryItemOut>("/me/memories", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function patchMemory(
  id: string,
  body: MemoryPatchIn,
): Promise<MemoryItemOut> {
  return apiFetch<MemoryItemOut>(`/me/memories/${id}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export function deleteMemory(id: string): Promise<{ ok: boolean }> {
  return apiFetch<{ ok: boolean }>(`/me/memories/${id}`, {
    method: "DELETE",
  });
}

export function clearMemories(): Promise<{ deleted: number }> {
  // Header 值必须是 ASCII; UI 已在调用前要求用户输入"清空"二字做二次确认,
  // 通过这一步后传 ASCII 哨兵给后端,避开中文 header 在反代/中间件被 strip。
  return apiFetch<{ deleted: number }>("/me/memories", {
    method: "DELETE",
    headers: { "X-Confirm-Clear-Memory": "yes" },
  });
}

export function exportMemories(): Promise<{ items: Array<Pick<MemoryItemOut, "type" | "content" | "source_excerpt" | "created_at">> }> {
  return apiFetch<{ items: Array<Pick<MemoryItemOut, "type" | "content" | "source_excerpt" | "created_at">> }>("/me/memories/export");
}

export function listMemoryStaging(): Promise<MemoryStagingListOut> {
  return apiFetch<MemoryStagingListOut>("/me/memories/staging");
}

export function patchMemoryStaging(
  id: string,
  body: Partial<Pick<MemoryStagingOut, "type" | "content" | "scope_id">>,
): Promise<MemoryStagingOut> {
  return apiFetch<MemoryStagingOut>(`/me/memories/staging/${id}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export function acceptMemoryStaging(id: string): Promise<MemoryItemOut> {
  return apiFetch<MemoryItemOut>(`/me/memories/staging/${id}/accept`, {
    method: "POST",
  });
}

export function rejectMemoryStaging(id: string): Promise<{ ok: boolean }> {
  return apiFetch<{ ok: boolean }>(`/me/memories/staging/${id}/reject`, {
    method: "POST",
  });
}

export function listMemoryTimeline(cursor?: string): Promise<MemoryTimelineOut> {
  const suffix = cursor ? `?cursor=${encodeURIComponent(cursor)}` : "";
  return apiFetch<MemoryTimelineOut>(`/me/memories/timeline${suffix}`);
}

export function listMemoryScopes(): Promise<MemoryScopeOut[]> {
  return apiFetch<MemoryScopeOut[]>("/me/memory-scopes");
}

export function createMemoryScope(body: {
  name: string;
  emoji?: string | null;
}): Promise<MemoryScopeOut> {
  return apiFetch<MemoryScopeOut>("/me/memory-scopes", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function patchMemoryScope(
  id: string,
  body: { name?: string; emoji?: string | null },
): Promise<MemoryScopeOut> {
  return apiFetch<MemoryScopeOut>(`/me/memory-scopes/${id}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export function deleteMemoryScope(id: string): Promise<{ moved: number }> {
  return apiFetch<{ moved: number }>(`/me/memory-scopes/${id}`, {
    method: "DELETE",
  });
}

export function patchConversationMemoryDisabled(
  convId: string,
  disabled: boolean,
): Promise<{ disabled: boolean }> {
  return apiFetch<{ disabled: boolean }>(
    `/conversations/${convId}/memory-disabled`,
    {
      method: "PATCH",
      body: JSON.stringify({ disabled }),
    },
  );
}

export function patchConversationActiveScope(
  convId: string,
  scopeId: string | null,
): Promise<{ scope_id: string | null }> {
  return apiFetch<{ scope_id: string | null }>(
    `/conversations/${convId}/active-scope`,
    {
      method: "PATCH",
      body: JSON.stringify({ scope_id: scopeId }),
    },
  );
}

export function getConversationUsedMemories(
  convId: string,
): Promise<{ used_memory_ids: string[]; used_memory_summary: Array<{ id: string; type: string; content: string }> }> {
  return apiFetch<{ used_memory_ids: string[]; used_memory_summary: Array<{ id: string; type: string; content: string }> }>(
    `/conversations/${convId}/used-memories`,
  );
}

// ——— Admin: 代理池（独立路由） ———

export function listAdminProxies(): Promise<import("./types").ProxyListOut> {
  return apiFetch<import("./types").ProxyListOut>("/admin/proxies");
}

export async function updateAdminProxies(
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

async function exportApiErrorFromResponse(res: Response): Promise<ApiError> {
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
      const e = (data as { error: { code?: string; message?: string } }).error;
      if (e.code) code = e.code;
      if (e.message) message = e.message;
    }
    return new ApiError({ code, message, status: res.status, payload: data });
  }
  return new ApiError({ code, message, status: res.status });
}

// /me/export 返回 zip 流，apiFetch 默认按 JSON 解析无法处理，所以自己写。
export async function exportMyData(): Promise<Blob> {
  const url = `${API_BASE.replace(/\/$/, "")}/me/export`;
  const doFetch = async (csrf: string | null): Promise<Response> => {
    const headers = new Headers();
    if (csrf) headers.set("x-csrf-token", csrf);
    return fetch(url, {
      method: "POST",
      headers,
      credentials: "include",
    });
  };

  let res: Response;
  try {
    res = await doFetch(await ensureCsrfToken());
  } catch (err) {
    throw new ApiError({
      code: "network_error",
      message: err instanceof Error ? err.message : "network error",
      status: 0,
    });
  }

  if (res.status === 403) {
    const err = await exportApiErrorFromResponse(res);
    if (err.code !== "csrf_failed") throw err;
    const fresh = await refreshCsrfToken().catch(() => null);
    if (!fresh) throw err;
    try {
      res = await doFetch(fresh);
    } catch (retryErr) {
      throw new ApiError({
        code: "network_error",
        message: retryErr instanceof Error ? retryErr.message : "network error",
        status: 0,
      });
    }
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
    throw await exportApiErrorFromResponse(res);
  }

  return res.blob();
}

export * from "./api/posterStyles";

// ============================================================================
// Poster Design Workflow（V1.1 海报工作流详情页）
// 后端路由：apps/api/app/routes/workflows.py 中 POSTER_WORKFLOW_TYPE = "poster_design"
// schemas：packages/core/lumen_core/schemas.py 的 PosterDesignWorkflow* / PosterMaster* / PosterRender* 类
// 7 step：copy_input → style_selection → copy_analysis → master_generation
//        → master_approval → multi_size_generation → delivery
// ============================================================================

export type PosterAspectRatio =
  | "1:1"
  | "9:16"
  | "16:9"
  | "3:4"
  | "4:3"
  | "2:3"
  | "3:2"
  | "4:5";
export type PosterRevisionScope = "background" | "inpaint" | "style";

export interface PosterBrandAssetsIn {
  logo_image_id?: string | null;
  product_image_id?: string | null;
  primary_color?: string | null;
  font_family?: string | null;
}

export interface PosterDesignWorkflowCreateIn {
  conversation_id?: string | null;
  copy_text: string;
  style_id: string;
  target_aspects?: PosterAspectRatio[];
  brand_assets?: PosterBrandAssetsIn;
  quality_mode?: "standard" | "premium";
  title?: string | null;
}

export interface PosterDesignWorkflowCreateOut {
  workflow_run_id: string;
  status: string;
  current_step: string;
}

export interface CopyAnalysisCorrections {
  main_title?: string | null;
  subtitle?: string | null;
  selling_points?: string[] | null;
  cta?: string | null;
  price?: string | null;
  tone?: string | null;
  info_density?: "high" | "medium" | "low" | string | null;
  // 兜底：用户额外字段；后端 corrections 是 Dict[str, Any]
  [key: string]: unknown;
}

export interface CopyAnalysisApproveIn {
  corrections: CopyAnalysisCorrections;
}

export interface PosterMastersCreateIn {
  candidate_count?: number;
  size_mode?: "auto" | "fixed";
  size?: string | null;
}

export interface PosterMasterApproveIn {
  adjustments?: string;
}

export interface PosterRendersCreateIn {
  aspects: PosterAspectRatio[];
  use_master_as_reference?: boolean;
  quality_mode?: "standard" | "premium";
}

export interface PosterReviseIn {
  scope: PosterRevisionScope;
  instruction: string;
  mask_image_id?: string | null;
}

export interface PosterInpaintIn {
  instruction: string;
  mask_image_id: string;
}

// 创建海报工作流
export function createPosterDesignWorkflow(
  body: PosterDesignWorkflowCreateIn,
): Promise<PosterDesignWorkflowCreateOut> {
  return apiFetch<PosterDesignWorkflowCreateOut>("/workflows/poster-design", {
    method: "POST",
    body: JSON.stringify({
      target_aspects: ["1:1", "9:16", "16:9", "3:4"],
      quality_mode: "premium",
      ...body,
    }),
  });
}

// 文案分析确认
export function approveCopyAnalysis(
  workflowId: string,
  body: CopyAnalysisApproveIn = { corrections: {} },
): Promise<WorkflowRun> {
  return apiFetch<WorkflowRun>(
    `/workflows/${workflowId}/steps/copy-analysis/approve`,
    {
      method: "POST",
      body: JSON.stringify({ corrections: body.corrections ?? {} }),
    },
  );
}

// 生成母版候选
export function createPosterMasters(
  workflowId: string,
  body: PosterMastersCreateIn = {},
): Promise<WorkflowRun> {
  return apiFetch<WorkflowRun>(`/workflows/${workflowId}/masters`, {
    method: "POST",
    body: JSON.stringify({
      candidate_count: 4,
      size_mode: "fixed",
      ...body,
    }),
  });
}

// 选定母版
export function approvePosterMaster(
  workflowId: string,
  masterId: string,
  body: PosterMasterApproveIn = {},
): Promise<WorkflowRun> {
  return apiFetch<WorkflowRun>(
    `/workflows/${workflowId}/masters/${masterId}/approve`,
    {
      method: "POST",
      body: JSON.stringify({ adjustments: "", ...body }),
    },
  );
}

// 生成多尺寸成品
export function createPosterRenders(
  workflowId: string,
  body: PosterRendersCreateIn,
): Promise<WorkflowRun> {
  return apiFetch<WorkflowRun>(`/workflows/${workflowId}/renders`, {
    method: "POST",
    body: JSON.stringify({
      use_master_as_reference: true,
      quality_mode: "premium",
      ...body,
    }),
  });
}

// 单张返修（背景重生/风格调整/inpaint）
export function revisePosterRender(
  workflowId: string,
  renderId: string,
  body: PosterReviseIn,
): Promise<WorkflowRun> {
  return apiFetch<WorkflowRun>(
    `/workflows/${workflowId}/renders/${renderId}/revise`,
    {
      method: "POST",
      body: JSON.stringify(body),
    },
  );
}

// 局部 inpaint（mask 必填）
export function inpaintPosterRender(
  workflowId: string,
  renderId: string,
  body: PosterInpaintIn,
): Promise<WorkflowRun> {
  return apiFetch<WorkflowRun>(
    `/workflows/${workflowId}/renders/${renderId}/inpaint`,
    {
      method: "POST",
      body: JSON.stringify(body),
    },
  );
}
