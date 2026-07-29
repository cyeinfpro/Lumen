import type { VideoProviderKind } from "./videoProviderTypes";
import type { AdminUserOut } from "./adminTypes";

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
  source: "db" | "env" | "none";
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

export interface VideoProviderItemOut {
  name: string;
  kind: VideoProviderKind;
  base_url: string;
  api_key_hint: string;
  access_key_id_hint?: string;
  secret_access_key_hint?: string;
  project_name?: string | null;
  region?: string | null;
  asset_management_ready?: boolean;
  enabled: boolean;
  priority: number;
  weight: number;
  concurrency: number;
  supports_idempotency: boolean;
  proxy: string | null;
  models: Record<string, string>;
}

export interface VideoProvidersOut {
  enabled: boolean;
  items: VideoProviderItemOut[];
  proxies: ProviderProxyOut[];
  source: "db" | "env" | "none";
}

export interface VideoProviderItemIn {
  name: string;
  kind: VideoProviderKind;
  base_url: string;
  api_key?: string;
  access_key_id?: string;
  secret_access_key?: string;
  project_name?: string;
  region?: string;
  enabled: boolean;
  priority: number;
  weight: number;
  concurrency: number;
  supports_idempotency: boolean;
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
  retention_hide_enabled: boolean;
  retention_delete_enabled: boolean;
  retention_hide_days: number;
  retention_delete_days: number;
}

export interface ByokSettingsPatchIn {
  mode_enabled?: boolean;
  byok_signup_enabled?: boolean;
  byok_signup_bypasses_allowlist?: boolean;
  fallback_to_admin_provider?: boolean;
  validation_model?: string;
  validation_timeout_ms?: number;
  pending_token_ttl_seconds?: number;
  retention_hide_enabled?: boolean;
  retention_delete_enabled?: boolean;
  retention_hide_days?: number;
  retention_delete_days?: number;
}

export interface AdminUserHistoryImageOut {
  id: string;
  url: string;
  display_url: string;
  preview_url: string | null;
  thumb_url: string | null;
  width: number;
  height: number;
  mime: string;
}

export interface AdminUserHistoryItemOut {
  id: string;
  kind: "generation";
  created_at: string;
  status: string;
  prompt: string | null;
  conversation_id: string | null;
  conversation_title: string | null;
  message_id: string | null;
  retention_state: "active" | "hidden" | "deleted";
  images: AdminUserHistoryImageOut[];
}

export interface AdminUserHistoryOut {
  user: AdminUserOut;
  items: AdminUserHistoryItemOut[];
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
