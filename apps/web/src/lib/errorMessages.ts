// V1.0 新增：上游错误码 → 中文文案的映射层。
//
// 背景：上游错误体格式固定 `{"error":{"message":"...","type":"...","code":"..."}}`；
// worker 端通过 `lumen_core.constants.classify_upstream_error` 把它收敛到 `UPSTREAM_*`
// 系列内部码。前端 toast / 红条 / ErrorBoundary 等用这里的 `getErrorMessage` 拿一句话
// 中文文案，避免直接展示英文 stack（例如线上曾出现 `Instructions are required`）。
//
// 设计：
// - 仅维护"码 → 文案"映射，不依赖 React，server / client 双侧可用
// - 兼容三种 key：枚举名（`UPSTREAM_INVALID_REQUEST`）、`.value`（`upstream_invalid_request`）、
//   以及历史 enum value（`invalid_request_error` / `rate_limit_error` / `server_error` /
//   `authentication_error` / `upstream_timeout` / `cancelled`），便于前端不需要关心后端
//   到底用哪种命名
// - 与现有 `errors.ts` 的 `errorCodeToMessage` 共存：未命中时调用方可继续走老路径
// - 不动 ErrorToast / ErrorBanner 等组件——保持文件边界
//
// 使用：
//   import { getErrorMessage } from "@/lib/errorMessages";
//   const text = getErrorMessage(failure.error_code);

/** 上游分类码（与 packages/core/lumen_core/constants.py 中 UPSTREAM_* 对齐）。 */
export const UpstreamErrorCode = {
  UPSTREAM_INVALID_REQUEST: "upstream_invalid_request",
  UPSTREAM_RATE_LIMITED: "upstream_rate_limited",
  UPSTREAM_SERVER_ERROR: "upstream_server_error",
  UPSTREAM_AUTH_ERROR: "upstream_auth_error",
  UPSTREAM_TIMEOUT: "upstream_timeout",
  UPSTREAM_CANCELLED: "upstream_cancelled",
  UPSTREAM_NETWORK_ERROR: "upstream_network_error",
  UPSTREAM_PAYLOAD_TOO_LARGE: "upstream_payload_too_large",
  UPSTREAM_CONTEXT_TOO_LONG: "upstream_context_too_long",
  UPSTREAM_UNKNOWN: "upstream_unknown",
  PROMPT_TOO_LONG: "prompt_too_long",
} as const;

export type UpstreamErrorCodeKey = keyof typeof UpstreamErrorCode;
export type UpstreamErrorCodeValue =
  (typeof UpstreamErrorCode)[UpstreamErrorCodeKey];

// 主映射：以 enum 值（lower snake）为键。
// 文案要求：一句话可操作，避免英文 stack；与 DESIGN 保持一致的口吻。
const UPSTREAM_MESSAGES: Record<UpstreamErrorCodeValue, string> = {
  [UpstreamErrorCode.UPSTREAM_INVALID_REQUEST]:
    "请求格式有误，请刷新后重试，如反复出现请反馈。",
  [UpstreamErrorCode.UPSTREAM_RATE_LIMITED]: "请求过于频繁，请稍候片刻再试。",
  [UpstreamErrorCode.UPSTREAM_SERVER_ERROR]:
    "服务暂时不可用，已自动重试，请稍后再试。",
  [UpstreamErrorCode.UPSTREAM_AUTH_ERROR]: "认证失败，请联系管理员。",
  [UpstreamErrorCode.UPSTREAM_TIMEOUT]: "上游响应超时，请重试或简化请求内容。",
  [UpstreamErrorCode.UPSTREAM_CANCELLED]: "已取消。",
  [UpstreamErrorCode.UPSTREAM_NETWORK_ERROR]: "网络异常，请检查连接后重试。",
  [UpstreamErrorCode.UPSTREAM_PAYLOAD_TOO_LARGE]:
    "上传内容过大，请压缩后重试。",
  [UpstreamErrorCode.UPSTREAM_CONTEXT_TOO_LONG]:
    "对话内容过长，请新建对话或压缩历史。",
  [UpstreamErrorCode.UPSTREAM_UNKNOWN]: "出现未知错误，请稍后重试。",
  [UpstreamErrorCode.PROMPT_TOO_LONG]:
    "提示词过长，请精简后再生成。",
};

// 兜底兼容：上游/worker 在迁移前可能直接落库为这些"老" enum value，
// 把它们也指到同一条文案，避免出现"码已知但文案空"。
const LEGACY_ALIAS_TO_VALUE: Record<string, UpstreamErrorCodeValue> = {
  // 老 enum value（lumen_core.constants.GenerationErrorCode 已存在的）
  invalid_request_error: UpstreamErrorCode.UPSTREAM_INVALID_REQUEST,
  rate_limit_error: UpstreamErrorCode.UPSTREAM_RATE_LIMITED,
  rate_limit_exceeded: UpstreamErrorCode.UPSTREAM_RATE_LIMITED,
  rate_limited: UpstreamErrorCode.UPSTREAM_RATE_LIMITED,
  server_error: UpstreamErrorCode.UPSTREAM_SERVER_ERROR,
  internal_error: UpstreamErrorCode.UPSTREAM_SERVER_ERROR,
  bad_gateway: UpstreamErrorCode.UPSTREAM_SERVER_ERROR,
  service_unavailable: UpstreamErrorCode.UPSTREAM_SERVER_ERROR,
  upstream_error: UpstreamErrorCode.UPSTREAM_SERVER_ERROR,
  authentication_error: UpstreamErrorCode.UPSTREAM_AUTH_ERROR,
  permission_error: UpstreamErrorCode.UPSTREAM_AUTH_ERROR,
  unauthorized: UpstreamErrorCode.UPSTREAM_AUTH_ERROR,
  timeout: UpstreamErrorCode.UPSTREAM_TIMEOUT,
  cancelled: UpstreamErrorCode.UPSTREAM_CANCELLED,
  context_length_exceeded: UpstreamErrorCode.UPSTREAM_CONTEXT_TOO_LONG,
  network_error: UpstreamErrorCode.UPSTREAM_NETWORK_ERROR,
  network_transient: UpstreamErrorCode.UPSTREAM_NETWORK_ERROR,
};

/** 内部归一：把外部 code 字符串归到 lower snake 的 enum value。 */
function normalize(code: string): string {
  return code.trim().toLowerCase();
}

/**
 * 单点查询：错误码 → 用户可见中文文案。
 *
 * 兼容多种 key 形态：
 * - `UpstreamErrorCode` 枚举值（`upstream_invalid_request`）
 * - 枚举名（`UPSTREAM_INVALID_REQUEST`，会被 lower 化后命中）
 * - 历史 enum value（`invalid_request_error` 等，走 LEGACY_ALIAS_TO_VALUE）
 *
 * 未命中时返回 `UPSTREAM_UNKNOWN` 对应的兜底文案，确保调用方拿到的一定是"可读中文"。
 */
export function getErrorMessage(code: string | null | undefined): string {
  if (!code) {
    return UPSTREAM_MESSAGES[UpstreamErrorCode.UPSTREAM_UNKNOWN];
  }
  const key = normalize(code);
  // 1. 直接命中 UPSTREAM_* enum value
  if (key in UPSTREAM_MESSAGES) {
    return UPSTREAM_MESSAGES[key as UpstreamErrorCodeValue];
  }
  // 2. legacy enum value alias
  const aliased = LEGACY_ALIAS_TO_VALUE[key];
  if (aliased) {
    return UPSTREAM_MESSAGES[aliased];
  }
  // 3. 兜底
  return UPSTREAM_MESSAGES[UpstreamErrorCode.UPSTREAM_UNKNOWN];
}

/** 调用方需要"码已知 / 未知"的判定时使用，未知时不要硬塞兜底文案。 */
export function hasErrorMessage(code: string | null | undefined): boolean {
  if (!code) return false;
  const key = normalize(code);
  return key in UPSTREAM_MESSAGES || key in LEGACY_ALIAS_TO_VALUE;
}
