// Lumen 前端统一 API 客户端（DESIGN §3.1 / §5.x）
//
// API_BASE 解析（优先级从高到低）：
//   1. 显式 NEXT_PUBLIC_API_BASE（编译期注入，跨域 / 子域部署时用）
//   2. 浏览器运行时 → "/api"（同源相对路径；由 next.config rewrites / 外层反代转发到后端）
//   3. SSR / build fallback → http://127.0.0.1:8000（生产 SSR 极少用到 API）
//
// 这样生产部署只需要外层 nginx 把 https://domain/* 一律透传到 web:3000；
// 不用再在反代层配 /api 路由 —— next.js rewrites 会把 /api/* 和 /events 内部
// 转发到 LUMEN_BACKEND_URL（默认 http://127.0.0.1:8000）。
//
// 所有请求带 credentials:"include" 以携带会话 cookie；写操作附 X-CSRF-Token。
// 401 一次性触发重定向到 /login（仅客户端环境）。非 2xx 抛 ApiError。
//
// 注意：本文件只做"薄封装"；不做请求缓存 / 重试 / 乐观更新（那些由 store 层处理）。

function computeApiBase(): string {
  const explicit = process.env.NEXT_PUBLIC_API_BASE?.trim();
  if (explicit) return explicit.replace(/\/$/, "");
  // 默认永远是同源 "/api" —— 无论浏览器还是 SSR 渲染。
  // 关键：imageBinaryUrl / eventUrl / shareImageUrl 等会被 render 成 <img src>、
  // 传到客户端 HTML。如果 SSR 期用绝对地址（localhost:8000），会被 baked 进 HTML
  // 导致浏览器跑 HTTP 被 mixed-content 拦。
  // 相对 "/api/..." 浏览器解析时自动拼成 https://<host>/api/...（同源）。
  //
  // 副作用：如果真有 server component 需要 fetch API，它得拿 LUMEN_BACKEND_URL
  // 自己拼绝对地址（目前没有这种调用；所有 apiFetch 只在 "use client" 组件中跑）。
  return "/api";
}

export const API_BASE = computeApiBase();

export type NoContent = undefined;

export type ApiFetchInit = RequestInit & {
  /**
   * Mark endpoints that intentionally return HTTP 204. This keeps call sites from
   * claiming a JSON shape for an empty response.
   */
  expectNoContent?: boolean;
};

// 只在客户端、且一个 tick 内只做一次跳转，防止多并发 401 风暴。
let _redirecting = false;
export function handle401() {
  if (typeof window === "undefined") return;
  if (_redirecting) return;
  _redirecting = true;
  // 使用 location.assign 而非 replace，保留 back 返回能力
  try {
    window.location.assign("/login");
  } catch {
    /* swallow */
  }
}

export function readCookie(name: string): string | null {
  if (typeof document === "undefined") return null;
  const cookies = document.cookie ? document.cookie.split("; ") : [];
  for (const raw of cookies) {
    const eq = raw.indexOf("=");
    if (eq < 0) continue;
    const k = raw.slice(0, eq);
    if (k === name) {
      const value = raw.slice(eq + 1);
      try {
        return decodeURIComponent(value);
      } catch (e) {
        // Cookie 值含损坏的 URI 编码（例如不完整的 %XX）会让 decodeURIComponent 抛 URIError，
        // 不能让该异常中止整个请求流程（CSRF 读取失败会引发所有写操作失败）。
        try {
          console.warn("[readCookie] failed to decode", name, e);
        } catch {
          /* console 不可用时忽略 */
        }
        return value;
      }
    }
  }
  return null;
}

export class ApiError extends Error {
  code: string;
  status: number;
  payload?: unknown;
  constructor(opts: {
    code: string;
    message: string;
    status: number;
    payload?: unknown;
  }) {
    super(opts.message);
    this.name = "ApiError";
    this.code = opts.code;
    this.status = opts.status;
    this.payload = opts.payload;
  }
}

// 写操作（POST/PUT/PATCH/DELETE）需要 X-CSRF-Token。GET/HEAD 不附。
const WRITE_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);
const CSRF_FAILED_CODE = "csrf_failed";

// API 重启 / 临时网络抖动时，fetch 会直接 throw（请求从未抵达服务器），这种情况重试是
// 安全的——服务端不会看到任何重复请求。HTTP 状态码错误（即使 5xx）不在这里重试，
// 因为无法判断服务端是否已部分处理。
const NETWORK_RETRY_MAX = 2;
const NETWORK_RETRY_DELAYS_MS = [400, 1200];

async function fetchWithNetworkRetry(
  url: string,
  init: RequestInit,
): Promise<Response> {
  let lastErr: unknown;
  for (let attempt = 0; attempt <= NETWORK_RETRY_MAX; attempt++) {
    try {
      return await fetch(url, init);
    } catch (err) {
      // 用户主动 abort 直接抛，不重试
      if (err instanceof DOMException && err.name === "AbortError") throw err;
      // 传入的 signal 已 aborted 也不重试
      const sig = (init as { signal?: AbortSignal }).signal;
      if (sig && sig.aborted) throw err;
      lastErr = err;
      if (attempt < NETWORK_RETRY_MAX) {
        await new Promise((r) =>
          setTimeout(r, NETWORK_RETRY_DELAYS_MS[attempt] ?? 1200),
        );
      }
    }
  }
  throw lastErr;
}

async function refreshCsrfToken(): Promise<string | null> {
  if (typeof document === "undefined") return null;
  const url = `${API_BASE.replace(/\/$/, "")}/auth/csrf`;
  const res = await fetchWithNetworkRetry(url, {
    method: "GET",
    credentials: "include",
    cache: "no-store",
  });
  if (!res.ok) return null;
  const data = (await res.json().catch(() => null)) as
    | { csrf_token?: unknown }
    | null;
  return typeof data?.csrf_token === "string"
    ? data.csrf_token
    : readCookie("csrf");
}

export async function apiFetch(
  path: string,
  init: ApiFetchInit & { expectNoContent: true },
): Promise<NoContent>;
export async function apiFetch<T = unknown>(
  path: string,
  init?: ApiFetchInit,
): Promise<T>;
export async function apiFetch<T = unknown>(
  path: string,
  init?: ApiFetchInit,
): Promise<T | NoContent> {
  const { expectNoContent = false, ...fetchInit } = init ?? {};
  const method = (fetchInit.method ?? "GET").toUpperCase();
  const url = path.startsWith("http")
    ? path
    : `${API_BASE.replace(/\/$/, "")}${path.startsWith("/") ? path : `/${path}`}`;

  const headers = new Headers(fetchInit.headers ?? {});
  // 当 body 是字符串时默认 JSON；FormData / Blob / ArrayBuffer / typed array 不设置
  // （让浏览器自带正确的 content-type，FormData 还会带 boundary）。
  const body = fetchInit.body;
  const isBinary =
    (typeof FormData !== "undefined" && body instanceof FormData) ||
    (typeof Blob !== "undefined" && body instanceof Blob) ||
    (typeof ArrayBuffer !== "undefined" &&
      (body instanceof ArrayBuffer || ArrayBuffer.isView(body as ArrayBufferView)));
  if (!isBinary && body && !headers.has("content-type")) {
    headers.set("content-type", "application/json");
  }
  if (WRITE_METHODS.has(method)) {
    let csrf = readCookie("csrf");
    if (!csrf) csrf = await refreshCsrfToken().catch(() => null);
    if (csrf && !headers.has("x-csrf-token")) {
      headers.set("x-csrf-token", csrf);
    } else if (!csrf) {
      // CSRF 仍拿不到：服务器一般会返回 403 csrf_failed；此处提前告警便于排查
      // dev 环境用 console.warn 以避免在 404/未登录场景反复打 Sentry
      if (process.env.NODE_ENV !== "production") {
        console.warn(
          "[apiFetch] missing CSRF token for write request",
          { method, path },
        );
      }
    }
  }

  let res: Response;
  const requestInit = {
    ...fetchInit,
    method,
    headers,
    credentials: "include" as RequestCredentials,
  };
  try {
    res = await fetchWithNetworkRetry(url, requestInit);
  } catch (err) {
    throw new ApiError({
      code: "network_error",
      message: err instanceof Error ? err.message : "network error",
      status: 0,
    });
  }

  if (res.status === 401) {
    handle401();
    throw new ApiError({
      code: "unauthorized",
      message: "未登录或会话已失效",
      status: 401,
    });
  }

  if (res.status === 204) {
    return undefined;
  }

  let ct = res.headers.get("content-type") ?? "";
  const isJson = ct.includes("application/json");
  let data: unknown = isJson
    ? await res.json().catch(() => null)
    : await res.text().catch(() => null);

  const codeFromPayload = (payload: unknown): string | undefined => {
    if (
      payload &&
      typeof payload === "object" &&
      "error" in payload &&
      typeof (payload as { error: unknown }).error === "object" &&
      (payload as { error: unknown }).error !== null
    ) {
      const e = (payload as { error: { code?: unknown } }).error;
      return typeof e.code === "string" ? e.code : undefined;
    }
    return undefined;
  };

  if (
    res.status === 403 &&
    WRITE_METHODS.has(method) &&
    codeFromPayload(data) === CSRF_FAILED_CODE
  ) {
    const fresh = await refreshCsrfToken().catch(() => null);
    if (fresh) {
      const retryHeaders = new Headers(headers);
      retryHeaders.set("x-csrf-token", fresh);
      const retryInit = {
        ...requestInit,
        headers: retryHeaders,
      };
      try {
        res = await fetchWithNetworkRetry(url, retryInit);
      } catch (err) {
        throw new ApiError({
          code: "network_error",
          message: err instanceof Error ? err.message : "network error",
          status: 0,
        });
      }
      if (res.status === 401) {
        handle401();
        throw new ApiError({
          code: "unauthorized",
          message: "未登录或会话已失效",
          status: 401,
        });
      }
      if (res.status === 204) {
        return undefined;
      }
      ct = res.headers.get("content-type") ?? "";
      const retryIsJson = ct.includes("application/json");
      data = retryIsJson
        ? await res.json().catch(() => null)
        : await res.text().catch(() => null);
    }
  }

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
    if (res.status === 413 && message === `HTTP ${res.status}`) {
      code = "request_too_large";
      message = "上传文件过大，请压缩后重试";
    }
    throw new ApiError({ code, message, status: res.status, payload: data });
  }

  if (expectNoContent) {
    return undefined;
  }

  return data as T;
}

export function apiFetchNoContent(
  path: string,
  init: ApiFetchInit = {},
): Promise<NoContent> {
  return apiFetch(path, { ...init, expectNoContent: true });
}
