import {
  API_BASE,
  ApiError,
  apiFetch,
  apiFetchNoContent,
  ensureCsrfToken,
  handle401,
  invalidateSessionClientState,
  refreshCsrfToken,
  resumeSessionClientState,
} from "./api/http";
import type { NoContent } from "./api/http";
import type { BackendMessage } from "./api/conversations";
import type {
  BackendCompletion,
  BackendGeneration,
  TaskStatus,
} from "./api/tasks";
import {
  createVideoAsset as createVideoAssetRequest,
  createVideoAssetGroup as createVideoAssetGroupRequest,
  deleteVideoAsset as deleteVideoAssetRequest,
  deleteVideoAssetGroup as deleteVideoAssetGroupRequest,
  patchVideoAsset as patchVideoAssetRequest,
  patchVideoAssetGroup as patchVideoAssetGroupRequest,
} from "./api/videoAssets";
import {
  enhancePrompt as runEnhancePrompt,
  enhanceVideoPrompt as runEnhanceVideoPrompt,
} from "./api/promptEnhancement";
import type {
  ImageParams,
  ApiSupplierTemplatePublicListOut,
  ApiKeyVerifyOut,
  RedemptionOut,
  VideoCreateIn,
  VideoGenerationOut,
  VideoAssetCreateIn,
  VideoAssetGroupCreateIn,
  VideoAssetGroupPatchIn,
  VideoAssetOperationOut,
  VideoAssetPatchIn,
  VideoPromptEnhanceIn,
  RecommendedErrorAction,
} from "./types";
import { uuid } from "./utils";
export {
  API_BASE,
  ApiError,
  apiFetch,
  apiFetchNoContent,
  safeAuthNextPath,
} from "./api/http";
export type { NoContent } from "./api/http";
export * from "./api/tasks";
export * from "./api/storyboards";
export * from "./api/workflows";
export * from "./api/posterWorkflows";
export {
  DEFAULT_VIDEO_ASSET_QUOTAS,
  getVideoAsset,
  getVideoAssetCapabilities,
  getVideoAssetOperation,
  getVideoAssetUsage,
  listVideoAssetGroups,
  listVideoAssets,
  retryVideoAssetOperation,
} from "./api/videoAssets";
export type {
  ListVideoAssetGroupsOptions,
  ListVideoAssetsOptions,
} from "./api/videoAssets";

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

async function acceptAuthenticatedSession(user: AuthUser): Promise<AuthUser> {
  await resumeSessionClientState(user.id);
  return user;
}

function sessionCookieSecureSignal(value: unknown): boolean | null {
  if (!value || typeof value !== "object") return null;
  const record = value as {
    session_cookie_secure?: unknown;
    response?: unknown;
    detail?: unknown;
  };
  if (typeof record.session_cookie_secure === "boolean") {
    return record.session_cookie_secure;
  }
  return (
    sessionCookieSecureSignal(record.response) ??
    sessionCookieSecureSignal(record.detail)
  );
}

function loginSessionError(
  loginResponse: unknown,
  unauthorized: ApiError,
): ApiError {
  const sessionCookieSecure =
    sessionCookieSecureSignal(unauthorized.payload) ??
    sessionCookieSecureSignal(loginResponse);
  const secureCookieBlockedByHttp =
    sessionCookieSecure === true &&
    typeof window !== "undefined" &&
    window.location.protocol === "http:";
  return new ApiError({
    code: secureCookieBlockedByHttp
      ? "secure_cookie_requires_https"
      : "session_unverified",
    message: secureCookieBlockedByHttp
      ? "密码验证成功，但当前使用 HTTP，浏览器无法保存 Secure 会话 Cookie。请改用 HTTPS 地址后重新登录。"
      : "密码验证成功，但登录会话未能确认。请检查 Cookie 或反向代理配置后重试。",
    status: 401,
  });
}

export async function login(
  email: string,
  password: string,
): Promise<AuthUser> {
  const loginResponse = await apiFetch<AuthUser>("/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
  try {
    return await acceptAuthenticatedSession(await getMe());
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) {
      throw loginSessionError(loginResponse, err);
    }
    throw err;
  }
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
  }).then(acceptAuthenticatedSession);
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
  }).then(acceptAuthenticatedSession);
}

export async function logout(): Promise<NoContent> {
  await invalidateSessionClientState();
  try {
    return await apiFetchNoContent("/auth/logout", { method: "POST" });
  } finally {
    await invalidateSessionClientState();
  }
}

export function getMe(): Promise<AuthUser> {
  return apiFetch<AuthUser>("/auth/me");
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
  return apiFetchNoContent(`/videos/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

export function videoBinaryUrl(videoId: string): string {
  return `${API_BASE.replace(/\/$/, "")}/videos/${encodeURIComponent(videoId)}/binary`;
}

export function videoDownloadUrl(videoId: string): string {
  return `${videoBinaryUrl(videoId)}?download=1`;
}

export function videoPosterUrl(videoId: string): string {
  return `${API_BASE.replace(/\/$/, "")}/videos/${encodeURIComponent(videoId)}/poster`;
}

// —— 火山官方 Seedance 私域 AIGC 素材 ——

export function createVideoAssetGroup(
  model: string,
  body: VideoAssetGroupCreateIn,
  opts: { signal?: AbortSignal } = {},
): Promise<VideoAssetOperationOut> {
  return createVideoAssetGroupRequest(model, body, opts);
}

export function patchVideoAssetGroup(
  groupId: string,
  model: string,
  body: VideoAssetGroupPatchIn,
  opts: { signal?: AbortSignal } = {},
): Promise<VideoAssetOperationOut> {
  return patchVideoAssetGroupRequest(groupId, model, body, opts);
}

export function deleteVideoAssetGroup(
  model: string,
  groupId: string,
  opts: { signal?: AbortSignal } = {},
): Promise<VideoAssetOperationOut> {
  return deleteVideoAssetGroupRequest(model, groupId, opts);
}

export function createVideoAsset(
  model: string,
  body: VideoAssetCreateIn,
  opts: { signal?: AbortSignal } = {},
): Promise<VideoAssetOperationOut> {
  return createVideoAssetRequest(model, body, opts);
}

export function patchVideoAsset(
  assetId: string,
  model: string,
  body: VideoAssetPatchIn,
  opts: { signal?: AbortSignal } = {},
): Promise<VideoAssetOperationOut> {
  return patchVideoAssetRequest(assetId, model, body, opts);
}

export function deleteVideoAsset(
  assetId: string,
  model: string,
  opts: { signal?: AbortSignal } = {},
): Promise<VideoAssetOperationOut> {
  return deleteVideoAssetRequest(assetId, model, opts);
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
  return apiFetch<TaskActionResponse>(`/${kind}/${id}/cancel`, {
    method: "POST",
  });
}

export function retryTask(
  kind: TaskKind,
  id: string,
): Promise<TaskActionResponse> {
  // 后端：POST /generations/{id}/retry 或 /completions/{id}/retry（tasks.py:85/169）
  return apiFetch<TaskActionResponse>(`/${kind}/${id}/retry`, {
    method: "POST",
  });
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

function queryValue(value: string | number | boolean | null | undefined): string | undefined {
  if (value === null || value === undefined || value === "") return undefined;
  return String(value);
}

function taskKindValue(kind: TaskListOpts["kind"]): string | undefined {
  return kind === "all" ? undefined : queryValue(kind);
}

function retryableValue(value: boolean | undefined): string | undefined {
  return value === undefined ? undefined : value ? "1" : "0";
}

function taskQuerySuffix(opts: TaskListOpts): string {
  const values: Record<string, string | undefined> = {
    status: queryValue(opts.status),
    mine: opts.mine ? "1" : undefined,
    kind: taskKindValue(opts.kind),
    source: queryValue(opts.source),
    conversation_id: queryValue(opts.conversation_id),
    project_id: queryValue(opts.project_id),
    date: queryValue(opts.date),
    cursor: queryValue(opts.cursor),
    error_code: queryValue(opts.error_code),
    retryable: retryableValue(opts.retryable),
    limit: queryValue(opts.limit),
  };
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value !== undefined) query.set(key, value);
  }
  const encoded = query.toString();
  return encoded ? `?${encoded}` : "";
}

export function listTasks(
  opts: TaskListOpts = {},
  requestOpts: { signal?: AbortSignal } = {},
): Promise<TaskListResponse> {
  const suffix = taskQuerySuffix(opts);
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

export function sseUrl(
  channels: string[],
  lastEventId?: string | null,
): string {
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
  return apiFetch<SilentGenerationOut>(`/conversations/${convId}/generations`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function enhancePrompt(
  text: string,
  onDelta: (text: string) => void,
  signal?: AbortSignal,
): Promise<void> {
  return runEnhancePrompt(text, onDelta, signal);
}

export async function enhanceVideoPrompt(
  body: VideoPromptEnhanceIn,
  onDelta: (text: string) => void,
  signal?: AbortSignal,
): Promise<void> {
  return runEnhanceVideoPrompt(body, onDelta, signal);
}

// ——————————————————————————————————————————————————————————————
// V1 收尾：Admin / Usage / Shares
// 与后端 Agent B 契约对齐；写操作走 apiFetch 自带的 CSRF；
// 公共 share endpoint 不发 credentials，直接用 fetch。
// ——————————————————————————————————————————————————————————————



export function redeemCode(code: string): Promise<RedemptionOut> {
  return apiFetch<RedemptionOut>("/me/redemptions", {
    method: "POST",
    headers: { "Idempotency-Key": createIdempotencyKey() },
    body: JSON.stringify({ code }),
  });
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

export * from "./api/admin";
export * from "./api/system";
export * from "./api/billing";
export * from "./api/memory";
export * from "./api/conversations";
export * from "./api/systemPrompts";
export * from "./api/images";
export * from "./api/account";
export * from "./api/posterStyles";
