import { apiFetch, apiFetchNoContent } from "./http";
import type { NoContent } from "./http";
import type { Intent, ImageParams, StructuredAttachment } from "../types";
import type {
  BackendCompletion,
  BackendGeneration,
  BackendImageMeta,
} from "./tasks";

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
  CompactConversationCompacted | CompactConversationSkipped;

export type CompactConversationApiResponse =
  | CompactConversationResponse
  | CompactConversationPending
  | CompactConversationFailed;

// 503 时 ApiError.payload 形如 { detail, reason }；这里给消费方一个稳定的常量集合便于分支。
export type CompactUnavailableReason =
  "lock_busy" | "circuit_open" | "upstream_error";

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
  if (opts.include && opts.include.length > 0)
    q.set("include", opts.include.join(","));
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
