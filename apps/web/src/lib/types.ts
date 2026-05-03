// 与 DESIGN.md §4 / §5 对齐的最小 V1 前端类型。
// 前端自用场景：没有持久化服务端时，消息/图像/任务先在内存中建立，后续接后端时仅替换数据源。

export type Intent =
  | "auto"
  | "chat"
  | "vision_qa"
  | "text_to_image"
  | "image_to_image";

export type AspectRatio =
  | "1:1"
  | "16:9"
  | "9:16"
  | "21:9"
  | "9:21"
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
  fast?: boolean;
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
}

export type GenerationStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "canceled";

export type GenerationStage =
  | "queued"
  | "understanding"
  | "rendering"
  | "finalizing";

// SSE 进度事件携带的细颗粒子阶段。粗 stage 用于持久化、断线重连恢复；
// substage 仅在实时 SSE 中出现，不识别时降级到粗 stage 行为。
// 与后端 lumen_core.constants.GenerationStage 的细子值保持一致。
export type GenerationSubstage =
  | "provider_selected"
  | "stream_started"
  | "partial_received"
  | "final_received"
  | "processing"
  | "storing";

export interface Generation {
  id: string;
  message_id: string;
  action: "generate" | "edit";
  prompt: string;
  size_requested: string;
  aspect_ratio: AspectRatio;
  input_image_ids: string[];
  primary_input_image_id: string | null;
  status: GenerationStatus;
  stage: GenerationStage;
  // 仅 SSE 实时事件填入；用户断线重连时拉历史只读 stage，substage 留空。
  substage?: GenerationSubstage;
  // P2: worker 内跨 provider failover 时由 generation.progress 携带 provider_failover=true。
  // 前端可据此在 DevelopingCard 上展示"换号重试中…"。一次任务可能多次 failover，
  // 用计数表达；首次为 0 / undefined。
  failover_count?: number;
  // 成功后填入
  image?: GeneratedImage;
  error_code?: string;
  error_message?: string;
  attempt: number;
  max_attempts?: number;
  retry_eta?: number;
  retry_error?: string;
  elapsed?: number;
  partial_count?: number;
  started_at: number;
  finished_at?: number;
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
  stream_started_at?: number;
  last_delta_at?: number;
  created_at: number;
}

export type Message = UserMessage | AssistantMessage;

export type CompletionToolCallStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "failed";

export interface CompletionToolCall {
  id: string;
  type: string;
  status: CompletionToolCallStatus;
  label: string;
  name?: string;
  title?: string;
  error?: string;
}

// ——————————————————————————————————————————————————————————————
// V1 收尾：Admin / Usage / Shares（对应后端 Agent B 的契约）
// ——————————————————————————————————————————————————————————————

export interface AllowedEmailOut {
  id: string;
  email: string;
  invited_by_email: string | null;
  created_at: string;
}

export interface AdminUserOut {
  id: string;
  email: string;
  role: "admin" | "member";
  display_name: string | null;
  created_at: string;
  generations_count: number;
  completions_count: number;
  messages_count: number;
}

export interface AdminRequestEventImageOut {
  id: string;
  roles: Array<"input" | "output">;
  source: string;
  url: string;
  display_url: string;
  preview_url: string | null;
  thumb_url: string | null;
  width: number;
  height: number;
  mime: string;
  parent_image_id: string | null;
  owner_generation_id: string | null;
}

export interface AdminRequestEventLiveLane {
  label: string;
  provider: string | null;
  route: string | null;
  endpoint: string | null;
  status: string | null;
  last_failed: string | null;
}

export interface AdminRequestEventOut {
  id: string;
  kind: "generation" | "completion";
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  status: string;
  progress_stage: string;
  attempt: number;
  model: string;
  user_id: string;
  user_email: string;
  conversation_id: string | null;
  conversation_title: string | null;
  message_id: string;
  prompt: string | null;
  action: string | null;
  intent: string | null;
  upstream_provider: string | null;
  upstream_route: string | null;
  upstream_endpoint: string | null;
  tokens_in: number | null;
  tokens_out: number | null;
  error_code: string | null;
  error_message: string | null;
  images: AdminRequestEventImageOut[];
  upstream: Record<string, unknown>;
  live_provider?: string | null;
  live_lanes?: AdminRequestEventLiveLane[];
}

export interface AdminRequestEventModelStatOut {
  model: string;
  count: number;
  share: number;
}

export interface AdminRequestEventsOut {
  items: AdminRequestEventOut[];
  total: number;
  model_stats?: AdminRequestEventModelStatOut[];
}

export interface UsageOut {
  range_start: string;
  range_end: string;
  messages_count: number;
  generations_count: number;
  generations_succeeded: number;
  generations_failed: number;
  completions_count: number;
  completions_succeeded: number;
  completions_failed: number;
  total_pixels_generated: number;
  total_tokens_in: number;
  total_tokens_out: number;
  storage_bytes: number;
}

export interface ShareOut {
  id: string;
  image_id: string;
  image_ids: string[];
  token: string;
  url: string; // 前端可直接打开的 /share/{token} 页面 URL
  image_url: string; // 公开图片二进制 URL
  show_prompt: boolean;
  expires_at: string | null;
  revoked_at: string | null;
  created_at: string;
}

export interface PublicShareImageOut {
  id: string;
  image_url: string;
  display_url?: string | null;
  preview_url?: string | null;
  thumb_url?: string | null;
  width: number;
  height: number;
  mime: string;
  prompt: string | null;
}

export interface PublicShareOut {
  token: string;
  image_url: string;
  images: PublicShareImageOut[];
  width: number;
  height: number;
  mime: string;
  show_prompt: boolean;
  prompt: string | null; // 仅 show_prompt=true 时后端会给，否则 null
  created_at: string;
  expires_at: string | null;
}

// ——————————————————————————————————————————————————————————————
// 邀请链接 / 系统设置 / 会话管理
// ——————————————————————————————————————————————————————————————

export interface InviteLinkOut {
  id: string;
  token: string;
  url: string;
  email: string | null;
  role: "admin" | "member";
  expires_at: string | null;
  used_at: string | null;
  used_by_email: string | null;
  revoked_at: string | null;
  created_at: string;
}

export interface InviteLinkPublicOut {
  token: string;
  email: string | null;
  role: "admin" | "member";
  expires_at: string | null;
  used: boolean;
  valid: boolean;
  invalid_reason: string | null;
}

export type SystemSettingKey =
  | "site.public_base_url"
  | "site.share_expiration_days"
  | "upstream.pixel_budget"
  | "upstream.global_concurrency"
  | "upstream.default_model"
  | "upstream.connect_timeout_s"
  | "upstream.read_timeout_s"
  | "upstream.write_timeout_s"
  | "image.channel"
  | "image.engine"
  | "image.output_format"
  | "image.job_base_url"
  | "image.primary_route"
  // DEPRECATED：旧键，worker fallback 仍兼容；新代码请用 image.channel + image.engine。
  | "image.text_to_image_primary_route"
  | "context.compression_enabled"
  | "context.compression_trigger_percent"
  | "context.summary_target_tokens"
  | "context.summary_model"
  | "context.summary_min_recent_messages"
  | "context.summary_min_interval_seconds"
  | "context.summary_input_budget"
  | "context.image_caption_enabled"
  | "context.image_caption_model"
  | "context.compression_circuit_breaker_threshold"
  | "context.manual_compact_min_input_tokens"
  | "context.manual_compact_cooldown_seconds"
  | "providers";

export interface SystemSettingItem {
  key: SystemSettingKey | string;
  value: string | null;
  has_value: boolean;
  is_sensitive: boolean;
  description: string;
}

export interface SystemSettingsOut {
  items: SystemSettingItem[];
}

export interface AdminModelOut {
  id: string;
  providers: string[];
  object: "model" | string;
}

export interface AdminModelsOut {
  models: AdminModelOut[];
  fetched_at: string;
  errors: { provider: string; message: string }[];
}

export interface AdminContextHealthOut {
  circuit_breaker_state: "closed" | "open" | "half_open" | string;
  circuit_breaker_until: string | null;
  last_24h: {
    summary_attempts: number;
    summary_successes: number;
    summary_failures: number;
    summary_success_rate: number;
    summary_p50_latency_ms: number | null;
    summary_p95_latency_ms: number | null;
    manual_compact_calls: number;
    cold_start_count: number;
    fallback_reasons: Record<string, number>;
  };
}

// ---------- Providers ----------

export type ImageJobsEndpoint = "auto" | "generations" | "responses";

export interface ProviderItemOut {
  name: string;
  base_url: string;
  api_key_hint: string;
  priority: number;
  weight: number;
  enabled: boolean;
  proxy: string | null;
  image_jobs_enabled: boolean;
  image_jobs_endpoint: ImageJobsEndpoint;
  image_jobs_endpoint_lock: boolean;
  image_jobs_base_url: string;
  image_concurrency: number;
}

export type ProviderProxyType = "socks5" | "ssh";

export interface ProviderProxyOut {
  name: string;
  type: ProviderProxyType;
  host: string;
  port: number;
  username: string | null;
  password_hint: string | null;
  private_key_path: string | null;
  enabled: boolean;
}

export interface ProvidersOut {
  items: ProviderItemOut[];
  proxies: ProviderProxyOut[];
  source: "db" | "env" | "none";
}

export interface ProviderItemIn {
  name: string;
  base_url: string;
  api_key?: string;
  priority: number;
  weight: number;
  enabled: boolean;
  proxy?: string | null;
  image_jobs_enabled?: boolean;
  image_jobs_endpoint?: ImageJobsEndpoint;
  image_jobs_endpoint_lock?: boolean;
  image_jobs_base_url?: string;
  image_concurrency?: number;
}

export interface ProviderProxyIn {
  name: string;
  type: ProviderProxyType;
  host: string;
  port: number;
  username?: string | null;
  password?: string;
  private_key_path?: string | null;
  enabled: boolean;
}

// ——— 代理池 ———

export interface ProxyHealthOut {
  name: string;
  type: ProviderProxyType;
  host: string;
  port: number;
  username: string | null;
  private_key_path: string | null;
  has_password: boolean;
  enabled: boolean;
  last_latency_ms: number | null;
  last_tested_at: string | null;
  last_target: string | null;
  in_cooldown: boolean;
}

export interface ProxyListOut {
  items: ProxyHealthOut[];
  test_target: string;
}

export interface ProxyTestOut {
  name: string;
  target: string;
  latency_ms: number;
  ok: boolean;
  error: string | null;
}

export interface ProviderProbeResult {
  name: string;
  ok: boolean;
  latency_ms: number | null;
  error: string | null;
  status: "healthy" | "unhealthy" | "disabled" | "skipped" | "unknown";
}

export interface ProvidersProbeOut {
  items: ProviderProbeResult[];
  probed_at: string | null;
}

export interface ProviderStatsItem {
  name: string;
  total: number;
  success: number;
  fail: number;
  success_rate: number;
  traffic_pct: number;
}

export interface ProviderStatsOut {
  items: ProviderStatsItem[];
  auto_probe_interval: number;
  auto_image_probe_interval: number;
}

export interface SessionOut {
  id: string;
  ua: string | null;
  ip: string | null;
  created_at: string;
  expires_at: string;
  is_current: boolean;
}
