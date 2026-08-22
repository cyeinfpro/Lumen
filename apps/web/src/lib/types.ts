// 与 DESIGN.md §4 / §5 对齐的最小 V1 前端类型。
// 前端自用场景：没有持久化服务端时，消息/图像/任务先在内存中建立，后续接后端时仅替换数据源。


export type { VideoProviderKind } from "./videoProviderTypes";

export type Intent =
  "auto" | "chat" | "vision_qa" | "text_to_image" | "image_to_image";

export type AspectRatio =
  | "1:1"
  | "16:9"
  | "9:16"
  | "21:9"
  | "9:21"
  | "10:7"
  | "7:10"
  | "4:5"
  | "3:4"
  | "4:3"
  | "3:2"
  | "2:3";

export type SizeMode = "auto" | "fixed";
export type Quality = "1k" | "2k" | "4k";
export type RenderQuality = "auto" | "low" | "medium" | "high";
export type RenderQualityChoice = Exclude<RenderQuality, "auto">;
export type ImageOutputFormat = "png" | "jpeg" | "webp";

export interface ImageParams {
  aspect_ratio: AspectRatio;
  size_mode: SizeMode;
  fixed_size?: string; // "WxH"
  quality?: Quality;
  count?: number;
  render_quality?: RenderQuality;
  output_format?: ImageOutputFormat;
  output_compression?: number;
  background?: "auto" | "opaque" | "transparent";
  moderation?: "auto" | "low";
}

export interface ResolvedSize {
  // 提交给上游的 size 字段：`"auto"` 或 `"{W}x{H}"`
  size: "auto" | `${number}x${number}`;
  width?: number;
  height?: number;
  // size=auto 时追加到 prompt 末尾的比例强指令
  prompt_suffix: string;
}

export interface AttachmentImage {
  // 前端 uuid（V1 无后端存储时直接用 crypto.randomUUID）
  id: string;
  // 参考图来源：
  // - upload：用户本地上传（data URL 驻留在客户端）
  // - generated：会话中先前生成的图（也存为 data URL 以便再次回传）
  kind: "upload" | "generated";
  // 实际发给上游的图像 data URL：`data:image/png;base64,...`
  data_url: string;
  mime: string;
  width?: number;
  height?: number;
  // 版本树父图：若该 attachment 由 generated 图派生，保留源 image_id
  source_image_id?: string;
  role?: AttachmentRole;
  label?: string;
  weight?: number;
}

export type AttachmentRole =
  | "reference"
  | "subject"
  | "product"
  | "style"
  | "edit_target"
  | "ask_target"
  | "background"
  | "mask"
  | "other";

export interface StructuredAttachment {
  image_id: string;
  role: AttachmentRole;
  label?: string;
  weight?: number;
}

// 局部修改 (inpaint) mask：与第一张参考图绑定。
// - image_id：上传到后端 /images/upload 后拿到的 mask 图 image_id（RGBA PNG，alpha=0 处会被重画）
// - preview_data_url：浏览器内本地预览（红色 overlay 已合成在原图上），仅用于 composer UI 显示"已设置 mask"
// - target_attachment_id：mask 绑定的参考图 attachment.id；附件变化时（删除 / 第二张加入）需要清除 mask
export interface MaskState {
  image_id: string;
  preview_data_url: string;
  target_attachment_id: string;
}

export interface ImageProviderAttempt {
  provider?: string | null;
  route?: string | null;
  endpoint?: string | null;
  proxy?: string | null;
  status?: string | null;
  duration_ms?: number | null;
  error_summary?: string | null;
  byok?: boolean | null;
  reason?: string | null;
}

export interface ImageGenerationDiagnostics {
  revised_prompt?: string | null;
  requested_params?: Record<string, unknown> | null;
  request_params?: Record<string, unknown> | null;
  effective_params?: Record<string, unknown> | null;
  actual_params?: Record<string, unknown> | null;
  provider?: string | null;
  upstream_provider?: string | null;
  actual_provider?: string | null;
  initial_provider?: string | null;
  first_provider?: string | null;
  proxy_name?: string | null;
  proxy_enabled?: boolean | null;
  duration_ms?: number | null;
  upstream_duration_ms?: number | null;
  upstream_duration_seconds?: number | null;
  elapsed_ms?: number | null;
  failover?: boolean | null;
  provider_failover?: boolean | null;
  failover_count?: number | null;
  debug_id?: string | null;
  trace_id?: string | null;
  request_id?: string | null;
  provider_attempts?: ImageProviderAttempt[];
  safe_error_summary?: string | null;
  upstream_error_summary?: string | null;
  error_summary?: string | null;
}

export interface GeneratedImage {
  id: string;
  data_url: string;
  mime?: string;
  display_url?: string;
  preview_url?: string;
  thumb_url?: string;
  width: number;
  height: number;
  // 版本树主父图；`text_to_image` 时为 null
  parent_image_id: string | null;
  from_generation_id: string;
  // 网关请求与实际返回的尺寸字符串，展示在角标
  size_requested: string;
  size_actual: string;
  filename?: string;
  metadata_jsonb?: Record<string, unknown> | null;
  is_dual_race_bonus?: boolean;
  billing_free?: boolean;
  billing_label?: string;
  billing_exempt_reason?: string;
  source_image_id?: string | null;
  diagnostics?: ImageGenerationDiagnostics | null;
  revised_prompt?: string | null;
  requested_params?: Record<string, unknown> | null;
  request_params?: Record<string, unknown> | null;
  effective_params?: Record<string, unknown> | null;
  actual_params?: Record<string, unknown> | null;
  provider_attempts?: ImageProviderAttempt[];
  source?: string | null;
  action_source?: string | null;
  trace_id?: string | null;
  attachment_roles?: StructuredAttachment[];
  queue_lane?: string | null;
  workflow_type?: string | null;
  workflow_step_key?: string | null;
  pixel_count?: number | null;
  size_bucket?: string | null;
  cost_class?: string | null;
  queue_wait_ms?: number | null;
}

export type GenerationStatus =
  "queued" | "running" | "succeeded" | "failed" | "canceled";

export type GenerationStage =
  "queued" | "understanding" | "rendering" | "finalizing";

// SSE 进度事件携带的细颗粒子阶段。粗 stage 用于持久化、断线重连恢复；
// substage 仅在实时 SSE 中出现，不识别时降级到粗 stage 行为。
// 与后端 lumen_core.constants.GenerationStage 的细子值保持一致。
export type GenerationSubstage =
  | "waiting_queue"
  | "waiting_provider"
  | "preparing_refs"
  | "upstream_started"
  | "upstream_retrying"
  | "postprocessing"
  | "display_ready"
  | "retryable"
  | "terminal"
  | "cancelled"
  | "completed"
  | "provider_selected"
  | "stream_started"
  | "partial_received"
  | "final_received"
  | "processing"
  | "storing";

export interface Generation {
  id: string;
  message_id: string;
  parent_generation_id?: string | null;
  action: "generate" | "edit";
  prompt: string;
  size_requested: string;
  aspect_ratio: AspectRatio;
  input_image_ids: string[];
  primary_input_image_id: string | null;
  status: GenerationStatus;
  stage: GenerationStage;
  // SSE 与任务快照均可填入；不识别时降级到粗 stage。
  substage?: GenerationSubstage;
  // P2: worker 内跨 provider failover 时由 generation.progress 携带 provider_failover=true。
  // 前端可据此在 DevelopingCard 上展示"换号重试中…"。一次任务可能多次 failover，
  // 用计数表达；首次为 0 / undefined。
  failover_count?: number;
  queue_position?: number | null;
  retrying?: boolean;
  waiting_provider?: boolean;
  cancelled?: boolean;
  // 成功后填入
  image?: GeneratedImage;
  error_code?: string;
  error_message?: string;
  retryable?: boolean;
  recommended_actions?: RecommendedErrorAction[];
  source?: string | null;
  conversation_id?: string | null;
  agent_session_id?: string | null;
  agent_run_id?: string | null;
  agent_tool_call_id?: string | null;
  project_id?: string | null;
  thumb_url?: string | null;
  diagnostics?: ImageGenerationDiagnostics | null;
  revised_prompt?: string | null;
  requested_params?: Record<string, unknown> | null;
  request_params?: Record<string, unknown> | null;
  effective_params?: Record<string, unknown> | null;
  actual_params?: Record<string, unknown> | null;
  provider_attempts?: ImageProviderAttempt[];
  action_source?: string | null;
  trace_id?: string | null;
  attachment_roles?: StructuredAttachment[];
  queue_lane?: string | null;
  workflow_type?: string | null;
  workflow_step_key?: string | null;
  pixel_count?: number | null;
  size_bucket?: string | null;
  cost_class?: string | null;
  queue_wait_ms?: number | null;
  attempt: number;
  max_attempts?: number;
  retry_eta?: number;
  retry_error?: string;
  elapsed?: number;
  partial_count?: number;
  started_at: number;
  finished_at?: number;
  is_dual_race_bonus?: boolean;
  billing_free?: boolean;
  billing_label?: string;
  billing_exempt_reason?: string;
}

export interface RecommendedErrorAction {
  id: string;
  label: string;
  kind?: "retry" | "link" | "adjust" | "wait" | "details" | string;
  href?: string | null;
}

export interface UserMessage {
  id: string;
  role: "user";
  text: string;
  attachments: AttachmentImage[];
  intent: Intent;
  image_params: ImageParams;
  web_search?: boolean;
  file_search?: boolean;
  code_interpreter?: boolean;
  image_generation?: boolean;
  created_at: number;
}

export interface AssistantMessage {
  id: string;
  role: "assistant";
  parent_user_message_id: string;
  intent_resolved: Exclude<Intent, "auto">;
  status: "pending" | "streaming" | "succeeded" | "failed" | "canceled";
  generation_ids?: string[];
  generation_id?: string;
  completion_id?: string;
  text?: string; // chat / vision_qa
  thinking?: string; // reasoning summary (streamed)
  tool_calls?: CompletionToolCall[];
  memory_writes?: MemoryWrite[];
  used_memory_ids?: string[];
  used_memory_summary?: UsedMemorySummary[];
  confirmation_candidate_id?: string | null;
  stream_started_at?: number;
  last_delta_at?: number;
  created_at: number;
}

export type Message = UserMessage | AssistantMessage;

export type CompletionToolCallStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "timed_out"
  | "unknown";

export interface CompletionToolCall {
  id: string;
  type: string;
  status: CompletionToolCallStatus;
  label: string;
  name?: string;
  title?: string;
  error?: string;
}

export interface MemoryWrite {
  id?: string | null;
  kind:
    "added" | "updated" | "merged" | "superseded" | "staged" | "rejected_pii";
  type?: "profile" | "preference" | "avoid" | "project" | null;
  content: string;
  source_excerpt?: string | null;
  undo_token?: string | null;
  scope_id?: string | null;
  recommended_scope_id?: string | null;
}

export interface UsedMemorySummary {
  id: string;
  type: "profile" | "preference" | "avoid" | "project" | string;
  content: string;
}


export * from "./adminTypes";
export * from "./providerTypes";
export * from "./billingVideoTypes";
