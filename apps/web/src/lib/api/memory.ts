import { apiFetch } from "./http";

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
  body: Partial<
    Pick<MemorySettingsOut, "paused" | "disabled" | "confirmation_enabled">
  >,
): Promise<MemorySettingsOut> {
  return apiFetch<MemorySettingsOut>("/me/memory-settings", {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export function markMemoryOnboardingSeen(
  flag: number,
): Promise<MemorySettingsOut> {
  return apiFetch<MemorySettingsOut>("/me/onboarding-seen", {
    method: "PATCH",
    body: JSON.stringify({ flag }),
  });
}

export function listMemories(
  opts: {
    type?: MemoryType;
    pinned?: boolean;
    disabled?: boolean;
    scope_id?: string;
  } = {},
): Promise<MemoryListOut> {
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

export function exportMemories(): Promise<{
  items: Array<
    Pick<MemoryItemOut, "type" | "content" | "source_excerpt" | "created_at">
  >;
}> {
  return apiFetch<{
    items: Array<
      Pick<MemoryItemOut, "type" | "content" | "source_excerpt" | "created_at">
    >;
  }>("/me/memories/export");
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

export function listMemoryTimeline(
  cursor?: string,
): Promise<MemoryTimelineOut> {
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

export function getConversationUsedMemories(convId: string): Promise<{
  used_memory_ids: string[];
  used_memory_summary: Array<{ id: string; type: string; content: string }>;
}> {
  return apiFetch<{
    used_memory_ids: string[];
    used_memory_summary: Array<{ id: string; type: string; content: string }>;
  }>(`/conversations/${convId}/used-memories`);
}
