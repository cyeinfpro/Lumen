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
    | "added"
    | "updated"
    | "merged"
    | "superseded"
    | "staged"
    | "rejected_pii";
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
  account_mode: "wallet" | "byok";
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
  queue_lane?: string | null;
  workflow_type?: string | null;
  workflow_step_key?: string | null;
  pixel_count?: number | null;
  size_bucket?: string | null;
  cost_class?: string | null;
  queue_wait_ms?: number | null;
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
  | "image.generation_concurrency"
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
  | "byok.mode_enabled"
  | "auth.byok_signup_enabled"
  | "auth.byok_signup_bypasses_allowlist"
  | "byok.fallback_to_admin_provider"
  | "byok.validation_model"
  | "byok.validation_timeout_ms"
  | "byok.pending_token_ttl_seconds"
  | "billing.enabled"
  | "billing.usd_to_rmb_rate"
  | "billing.allow_negative_balance"
  | "billing.image_size_thresholds"
  | "billing.redemption_code_secret"
  | "billing.low_balance_warn_micro"
  | "billing.bootstrap_completed"
  | "billing.show_estimate_in_composer"
  | "providers"
  | "video.enabled"
  | "video.providers"
  | "video.token_hold_estimates";

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
export type ImageEditInputTransport = "url" | "file";
export type ProviderPurpose = "chat" | "image" | "embedding";

export interface ProviderItemOut {
  name: string;
  base_url: string;
  api_key_hint: string;
  priority: number;
  weight: number;
  enabled: boolean;
  purposes: ProviderPurpose[];
  proxy: string | null;
  image_jobs_enabled: boolean;
  image_jobs_endpoint: ImageJobsEndpoint;
  image_jobs_endpoint_lock: boolean;
  image_jobs_base_url: string;
  image_edit_input_transport: ImageEditInputTransport;
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
  source: "db" | "env" | "none" | "desktop";
}

export interface ProviderItemIn {
  name: string;
  base_url: string;
  api_key?: string;
  priority: number;
  weight: number;
  enabled: boolean;
  purposes: ProviderPurpose[];
  proxy?: string | null;
  image_jobs_enabled?: boolean;
  image_jobs_endpoint?: ImageJobsEndpoint;
  image_jobs_endpoint_lock?: boolean;
  image_jobs_base_url?: string;
  image_edit_input_transport?: ImageEditInputTransport;
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

export type VideoProviderKind =
  | "volcano"
  | "volcano_third_party"
  | "dashscope"
  | "veo"
  | "omni_flash"
  | "fake";

export interface VideoProviderItemOut {
  name: string;
  kind: VideoProviderKind;
  base_url: string;
  api_key_hint: string;
  enabled: boolean;
  priority: number;
  weight: number;
  concurrency: number;
  proxy: string | null;
  models: Record<string, string>;
}

export interface VideoProvidersOut {
  enabled: boolean;
  items: VideoProviderItemOut[];
  proxies: ProviderProxyOut[];
  source: "db" | "env" | "none" | "desktop";
}

export interface VideoProviderItemIn {
  name: string;
  kind: VideoProviderKind;
  base_url: string;
  api_key?: string;
  enabled: boolean;
  priority: number;
  weight: number;
  concurrency: number;
  proxy?: string | null;
  models: Record<string, string>;
}

export interface VideoProvidersUpdateIn {
  enabled: boolean;
  items: VideoProviderItemIn[];
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

// ---------- BYOK ----------

export type ByokPurpose = "chat" | "image" | "embedding";

export interface ByokSettingsOut {
  mode_enabled: boolean;
  byok_signup_enabled: boolean;
  byok_signup_bypasses_allowlist: boolean;
  fallback_to_admin_provider: boolean;
  validation_model: string;
  validation_timeout_ms: number;
  pending_token_ttl_seconds: number;
}

export interface ByokSettingsPatchIn {
  mode_enabled?: boolean;
  byok_signup_enabled?: boolean;
  byok_signup_bypasses_allowlist?: boolean;
  fallback_to_admin_provider?: boolean;
  validation_model?: string;
  validation_timeout_ms?: number;
  pending_token_ttl_seconds?: number;
}

export interface ApiSupplierTemplateOut {
  id: string;
  name: string;
  slug: string;
  base_url: string;
  enabled: boolean;
  public_signup_enabled: boolean;
  user_bind_enabled: boolean;
  purposes: ByokPurpose[];
  validation_model: string;
  default_chat_model: string;
  fast_chat_model: string | null;
  validation_timeout_ms: number;
  proxy_name: string | null;
  text_concurrency_per_key: number;
  image_concurrency_per_key: number;
  capabilities_jsonb: Record<string, unknown>;
  active_credentials: number;
  recent_success_rate: number | null;
  recent_error_counts: Record<string, number>;
  created_at: string;
  updated_at: string;
}

export interface ApiSupplierTemplatePublicOut {
  id: string;
  name: string;
  purposes: ByokPurpose[];
  validation_model: string;
}

export interface ApiSupplierTemplateListOut {
  items: ApiSupplierTemplateOut[];
}

export interface ApiSupplierTemplatePublicListOut {
  items: ApiSupplierTemplatePublicOut[];
}

export interface ApiSupplierTemplateIn {
  name: string;
  slug?: string | null;
  base_url: string;
  enabled: boolean;
  public_signup_enabled: boolean;
  user_bind_enabled: boolean;
  purposes: ByokPurpose[];
  validation_model: string;
  default_chat_model: string;
  fast_chat_model?: string | null;
  validation_timeout_ms: number;
  proxy_name?: string | null;
  text_concurrency_per_key: number;
  image_concurrency_per_key: number;
  capabilities_jsonb?: Record<string, unknown>;
}

export interface ApiKeyVerifyOut {
  ok: boolean;
  verification_token: string;
  supplier_id: string;
  key_hint: string;
  verified_at: string;
}

// 与 packages/core/lumen_core/schemas.py ApiSupplierProbeOut 对齐。
// 管理员探活 /admin/api-suppliers/{id}/probe 的返回。
export interface ApiSupplierProbeOut {
  ok: boolean;
  error_code: string | null;
  http_status: number | null;
  latency_ms: number;
  key_hint: string | null;
}

export interface UserApiCredentialOut {
  id: string;
  supplier_id: string;
  supplier_name: string;
  key_hint: string;
  status: string;
  last_verified_at: string | null;
  last_failed_at: string | null;
  last_error_code: string | null;
  rate_limited_until: string | null;
  created_at: string;
  updated_at: string;
}

export interface UserApiCredentialListOut {
  items: UserApiCredentialOut[];
}

export interface TelegramLinkCodeOut {
  code: string;
  expires_in: number;
  deep_link: string | null;
}

export interface SessionOut {
  id: string;
  ua: string | null;
  ip: string | null;
  created_at: string;
  expires_at: string;
  is_current: boolean;
}

// ---------- Billing / Wallet ----------

export interface MoneyOut {
  micro: number;
  rmb: string;
}

export interface WalletOut {
  mode: "wallet" | "byok";
  balance: MoneyOut | null;
  hold: MoneyOut | null;
  low_balance_threshold?: MoneyOut | null;
  frozen: boolean;
}

export interface WalletTransactionOut {
  id: string;
  kind: string;
  amount: MoneyOut;
  balance_after: MoneyOut;
  hold_after: MoneyOut;
  ref_type: string | null;
  ref_id: string | null;
  meta: Record<string, unknown>;
  created_at: string;
  created_by_admin: string | null;
}

export interface WalletTransactionListOut {
  items: WalletTransactionOut[];
  next_cursor?: string | null;
}

export interface BillingWindowOut {
  used_micro: number;
  limit_micro: number;
  resets_at: string | null;
}

export interface BillingUsageByKindOut {
  input: number;
  output: number;
  cache_read: number;
  cache_creation: number;
  image: number;
  reasoning: number;
}

export interface BillingSnapshotOut {
  balance_micro: number;
  billing_rate_multiplier: string;
  windows: Record<string, BillingWindowOut>;
  by_kind_30d: BillingUsageByKindOut;
}

export interface AdminBillingUsageOut {
  user_id: string;
  balance_micro: number;
  billing_rate_multiplier: string;
  range_start: string;
  range_end: string;
  windows: Record<string, BillingWindowOut>;
  by_kind_30d: BillingUsageByKindOut;
  total_micro: number;
  transaction_count: number;
}

export interface PricingRuleOut {
  id: string;
  scope: "image_size" | "chat_model" | "video";
  key: string;
  variant: string;
  unit:
    | "per_image"
    | "per_1k_tokens_in"
    | "per_1k_tokens_out"
    | "per_1k_tokens_cache_read"
    | "per_1k_tokens_cache_creation"
    | "per_1k_tokens_cache_creation_5m"
    | "per_1k_tokens_cache_creation_1h"
    | "per_1k_tokens_image_output"
    | "per_1k_tokens_reasoning"
    | "per_1k_tokens_input_priority"
    | "per_1k_tokens_output_priority"
    | "per_1k_tokens_cache_read_priority"
    | "long_context_threshold"
    | "long_context_input_multiplier"
    | "long_context_output_multiplier"
    | "per_mtoken";
  price: MoneyOut;
  enabled: boolean;
  note: string | null;
  created_at: string;
  updated_at: string;
}

export interface PricingRulesOut {
  items: PricingRuleOut[];
  image_size_thresholds?: Record<string, number> | null;
  billing_enabled?: boolean | null;
  show_estimate_in_composer?: boolean | null;
}

export type VideoAction = "t2v" | "i2v" | "reference";
export type VideoStatus =
  | "queued"
  | "submitting"
  | "submitted"
  | "running"
  | "succeeded"
  | "failed"
  | "canceled"
  | "expired";
export type VideoStage =
  | "queued"
  | "submitting"
  | "rendering"
  | "fetching"
  | "storing"
  | "billing"
  | "finished";

export interface VideoOut {
  id: string;
  url: string;
  poster_url?: string | null;
  width: number;
  height: number;
  duration_ms: number;
  fps?: number | null;
  has_audio: boolean;
  mime: string;
  size_bytes?: number | null;
  faststart?: boolean | null;
  created_at?: string | null;
}

export interface VideoReferenceMediaIn {
  kind: "image" | "video";
  image_id?: string | null;
  video_id?: string | null;
  url?: string | null;
  label?: string | null;
}

export interface VideoReferenceMediaOut {
  kind: "image" | "video";
  image_id?: string | null;
  video_id?: string | null;
  url?: string | null;
  label?: string | null;
  mime?: string | null;
}

export interface VideoCreateIn {
  action: VideoAction;
  model: string;
  prompt: string;
  input_image_id?: string | null;
  reference_media?: VideoReferenceMediaIn[];
  duration_s: number;
  resolution: "480p" | "720p" | "1080p" | "4k";
  aspect_ratio: string;
  generate_audio?: boolean;
  seed?: number | null;
  watermark?: boolean;
  idempotency_key: string;
}

export interface VideoPromptEnhanceIn {
  text?: string;
  action?: VideoAction;
  model?: string;
  duration_s?: number | null;
  resolution?: string | null;
  aspect_ratio?: string | null;
  generate_audio?: boolean | null;
  input_image_id?: string | null;
  reference_media?: VideoReferenceMediaIn[];
  variant_count?: number;
}

export interface VideoPriceOptionOut {
  model: string;
  action: VideoAction | "reference_image" | "reference_video";
  resolution?: string | null;
  variant?: string | null;
  unit: "per_mtoken";
  price: MoneyOut;
  enabled: boolean;
  note?: string | null;
}

export interface VideoModelOptionOut {
  model: string;
  billing_model?: string | null;
  billing_models?: Partial<Record<VideoAction, string>>;
  actions: VideoAction[];
  durations_s?: number[];
  resolutions?: Array<VideoCreateIn["resolution"]>;
}

export interface VideoOptionsOut {
  enabled: boolean;
  models: VideoModelOptionOut[];
  durations_s: number[];
  resolutions: string[];
  aspect_ratios: string[];
  generate_audio: boolean;
  pricing: VideoPriceOptionOut[];
  hold_estimates: Record<string, unknown>;
  unavailable_reason?: string | null;
}

export interface VideoGenerationOut {
  id: string;
  action: VideoAction;
  model: string;
  prompt: string;
  input_image_id?: string | null;
  reference_media: VideoReferenceMediaOut[];
  duration_s: number;
  resolution: string;
  aspect_ratio: string;
  fps?: number | null;
  generate_audio: boolean;
  seed?: number | null;
  status: VideoStatus;
  progress_stage: VideoStage;
  progress_pct: number;
  provider_name?: string | null;
  provider_kind?: string | null;
  est_token_upper: number;
  est_cost: MoneyOut;
  billed_tokens?: number | null;
  billed_cost?: MoneyOut | null;
  video?: VideoOut | null;
  error_code?: string | null;
  error_message?: string | null;
  diagnostics?: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  started_at?: string | null;
  submitted_at?: string | null;
  finished_at?: string | null;
}

export interface VideoGenerationsOut {
  items: VideoGenerationOut[];
  next_cursor?: string | null;
}

export interface PricingRuleUpsertIn {
  scope: "image_size" | "chat_model" | "video";
  key: string;
  variant?: string;
  unit:
    | "per_image"
    | "per_1k_tokens_in"
    | "per_1k_tokens_out"
    | "per_1k_tokens_cache_read"
    | "per_1k_tokens_cache_creation"
    | "per_1k_tokens_cache_creation_5m"
    | "per_1k_tokens_cache_creation_1h"
    | "per_1k_tokens_image_output"
    | "per_1k_tokens_reasoning"
    | "per_1k_tokens_input_priority"
    | "per_1k_tokens_output_priority"
    | "per_1k_tokens_cache_read_priority"
    | "long_context_threshold"
    | "long_context_input_multiplier"
    | "long_context_output_multiplier"
    | "per_mtoken";
  price_rmb: string;
  enabled?: boolean;
  note?: string | null;
}

export interface AdminPricingBulkRatesIn {
  input?: string | number | null;
  output?: string | number | null;
  cache_read?: string | number | null;
  cache_creation?: string | number | null;
  cache_creation_5m?: string | number | null;
  cache_creation_1h?: string | number | null;
  image_output?: string | number | null;
  reasoning?: string | number | null;
  input_priority?: string | number | null;
  output_priority?: string | number | null;
  cache_read_priority?: string | number | null;
  long_context_threshold?: number | null;
  long_context_input_multiplier?: number | null;
  long_context_output_multiplier?: number | null;
}

export interface AdminPricingBulkIn {
  model: string;
  channel?: string | null;
  rates: AdminPricingBulkRatesIn;
  enabled?: boolean;
  note?: string | null;
}

export interface RedemptionOut {
  amount: MoneyOut;
  balance: MoneyOut;
}

export interface RedemptionUsageOut {
  id: string;
  code_id: string;
  amount: MoneyOut;
  redeemed_at: string;
}

export interface RedemptionUsageListOut {
  items: RedemptionUsageOut[];
  next_cursor?: string | null;
}

export interface AdminRedemptionCodeOut {
  id: string;
  code_prefix: string;
  amount: MoneyOut;
  max_redemptions: number;
  redeemed_count: number;
  usable_count: number;
  status: "active" | "revoked" | "expired" | "exhausted";
  batch_id: string | null;
  note: string | null;
  expires_at: string | null;
  revoked_at: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface AdminRedemptionCodeListOut {
  items: AdminRedemptionCodeOut[];
  next_cursor?: string | null;
}

export interface AdminRedemptionUsageOut {
  id: string;
  code_id: string;
  user_id: string;
  user_email: string | null;
  amount: MoneyOut;
  wallet_tx_id: string;
  redeemed_at: string;
  ip_hash: string | null;
}

export interface AdminRedemptionUsageListOut {
  items: AdminRedemptionUsageOut[];
  next_cursor?: string | null;
}

export interface AdminRedemptionCodeCreateOut {
  batch_id: string;
  count: number;
  amount: MoneyOut;
  download_token: string;
  plaintext_codes: string[];
  expires_at: string | null;
}

export interface AdminWalletOut {
  user_id: string;
  email: string;
  account_mode: "wallet" | "byok";
  wallet: WalletOut;
  last_topup_at?: string | null;
  last_charge_at?: string | null;
}

export interface AdminWalletListOut {
  items: AdminWalletOut[];
  next_cursor?: string | null;
}

export interface AdminWalletDetailOut extends AdminWalletOut {
  last_redemption_at?: string | null;
  transactions: WalletTransactionOut[];
  redemptions: AdminRedemptionUsageOut[];
}

export interface AdminBillingAuditEventOut {
  id: string;
  event_type: string;
  user_id: string | null;
  target_user_id: string | null;
  details: Record<string, unknown>;
  created_at: string;
}

export interface AdminBillingOverviewOut {
  billing_enabled: boolean;
  redemption_secret_configured: boolean;
  bootstrap_completed: boolean;
  wallet_total_balance: MoneyOut;
  active_holds_count: number;
  active_holds: MoneyOut;
  codes_active: number;
  codes_redeemed_24h: number;
  codes_redeemed_24h_amount: MoneyOut;
  charges_24h: MoneyOut;
  thresholds_pricing_aligned: boolean;
  thresholds_missing_prices: string[];
  recent_audit_events: AdminBillingAuditEventOut[];
}

export interface AdminWalletAuditOut {
  ok: boolean;
  transactions: number;
  users: number;
  mismatch_count: number;
  mismatches: string[];
}

export interface AdminOrphanHoldOut {
  tx: WalletTransactionOut;
  user_id: string;
  age_seconds: number;
}

export interface AdminBillingBootstrapIn {
  redemption_code_secret?: string | null;
  enabled?: boolean;
  usd_to_rmb_rate?: number;
  low_balance_warn_rmb?: string;
  image_size_thresholds?: Record<string, number>;
  image_prices_rmb?: Record<string, string>;
}

export interface AdminRedemptionBatchRedownloadOut {
  batch_id: string;
  count: number;
  download_token: string;
  plaintext_codes: string[];
  expires_in_seconds: number;
}
