import { API_BASE, ApiError, apiFetch, apiFetchNoContent } from "./http";
import type { NoContent } from "./http";
import type {
  AdminRequestEventsOut,
  AdminUserHistoryOut,
  AdminUserOut,
  AllowedEmailOut,
  InviteLinkOut,
  InviteLinkPublicOut,
  ProviderProxyIn,
  ShareOut,
} from "../types";

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
  return apiFetch<{
    ok: boolean;
    timestamp?: string | null;
    stderr_tail?: string | null;
  }>("/admin/backups/now", { method: "POST", body: JSON.stringify({}) });
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

// ——— Admin: 代理池（独立路由） ———

export function listAdminProxies(): Promise<import("../types").ProxyListOut> {
  return apiFetch<import("../types").ProxyListOut>("/admin/proxies");
}

export async function updateAdminProxies(
  items: ProviderProxyIn[],
): Promise<import("../types").ProxyListOut> {
  return apiFetch<import("../types").ProxyListOut>("/admin/proxies", {
    method: "PUT",
    body: JSON.stringify({ items }),
  });
}

export function restartTelegramBot(): Promise<{
  ok: boolean;
  receivers: number;
}> {
  return apiFetch<{ ok: boolean; receivers: number }>(
    "/admin/telegram/restart",
    { method: "POST" },
  );
}

export function testAdminProxy(
  name: string,
  target?: string,
): Promise<import("../types").ProxyTestOut> {
  return apiFetch<import("../types").ProxyTestOut>(
    `/admin/proxies/test/${encodeURIComponent(name)}`,
    {
      method: "POST",
      body: JSON.stringify(target ? { target } : {}),
    },
  );
}

export function testAllAdminProxies(
  target?: string,
): Promise<import("../types").ProxyTestOut[]> {
  return apiFetch<import("../types").ProxyTestOut[]>("/admin/proxies/test-all", {
    method: "POST",
    body: JSON.stringify(target ? { target } : {}),
  });
}
