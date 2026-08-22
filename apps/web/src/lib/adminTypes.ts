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
  | "ui.nav.studio_visible"
  | "ui.nav.agent_visible"
  | "ui.nav.video_visible"
  | "ui.nav.projects_visible"
  | "ui.nav.assets_visible"
  | "canvas.enabled"
  | "agent.enabled"
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
  // DEPRECATED：旧键，worker fallback 仍兼容；新代码用 image.channel + image.engine。
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
  | "byok.retention_hide_enabled"
  | "byok.retention_delete_enabled"
  | "byok.retention_hide_days"
  | "byok.retention_delete_days"
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
