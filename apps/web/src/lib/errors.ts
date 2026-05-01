// Lumen 前端统一错误码 → 用户友好文案 + 操作建议的映射层。
// 调用方：toast / inline alert / ErrorBoundary 等都应过这一层，避免直接渲染原始 error.message
// 或后端 code 给最终用户。
//
// 设计：
// - mapError 对不同输入归一化成 NormalizedError：含 title（标题）、description（说明）、
//   actionLabel（CTA 文案，如有），以及原始 code/status 留作打点
// - errorCodeToMessage / errorCodeToAction 仍保留单点查询便利，已被 useChatStore 早期使用
// - 不依赖 React，server / client 双侧可用

import { ApiError } from "./api/http";

export type ErrorAction = "retry" | "login" | "back" | "refresh" | "wait";

export interface NormalizedError {
  /** 一句话主信息，可放 toast / 红条 */
  title: string;
  /** 选填：补充说明（一行内可读完） */
  description?: string;
  /** 推荐操作类型 */
  action?: ErrorAction;
  /** 推荐操作 CTA 文案 */
  actionLabel?: string;
  /** 原始 code（无法识别时为 "unknown"） */
  code: string;
  /** HTTP status，0 表示请求未抵达 */
  status: number;
}

// 错误码 → 主标题。命中即可定型；未命中走 status 兜底。
const CODE_TITLE: Record<string, string> = {
  network_error: "网络异常",
  upstream_timeout: "服务繁忙",
  rate_limited: "操作过于频繁",
  unauthorized: "登录已过期",
  forbidden: "没有访问权限",
  csrf_failed: "请求校验失败",
  quota_exceeded: "上游服务暂时拥挤",
  upstream_error: "上游服务异常",
  prompt_too_long: "提示词过长",
  invalid_request: "请求内容不合法",
  validation_error: "输入内容不合法",
  not_found: "请求的资源不存在",
  client_exception: "客户端异常",
  // 业务子集
  no_conversation: "当前没有活动会话",
  message_not_found: "找不到对应的消息",
  missing_parent: "消息缺少父级关联",
  email_taken: "该邮箱已被注册",
  invite_invalid: "邀请链接无效或已过期",
  conversation_not_found: "会话不存在或已删除",
};

const CODE_DESC: Record<string, string> = {
  network_error: "网络断开或请求超时，请稍后重试",
  upstream_timeout: "上游响应超时，请稍后再试",
  rate_limited: "请求过于频繁，稍后重试",
  unauthorized: "请重新登录后继续操作",
  forbidden: "你没有权限访问该资源",
  csrf_failed: "请刷新页面后再试",
  quota_exceeded: "服务器繁忙，请稍后重试",
  upstream_error: "服务暂时不可用，请稍后再试",
  prompt_too_long: "请精简提示词后再发送",
  invalid_request: "请检查输入内容后重试",
  validation_error: "请检查输入内容是否合规",
  not_found: "目标可能已被删除或不存在",
  client_exception: "客户端发生异常，刷新后再试",
};

const CODE_ACTION: Record<string, ErrorAction> = {
  network_error: "retry",
  upstream_timeout: "retry",
  rate_limited: "wait",
  unauthorized: "login",
  forbidden: "back",
  csrf_failed: "refresh",
  quota_exceeded: "wait",
  upstream_error: "retry",
  prompt_too_long: "back",
  invalid_request: "back",
  validation_error: "back",
  not_found: "back",
  client_exception: "refresh",
};

const ACTION_LABEL: Record<ErrorAction, string> = {
  retry: "重试",
  login: "去登录",
  back: "返回",
  refresh: "刷新页面",
  wait: "稍后再试",
};

// 把 status 兜底成最贴近的 code，配合 mapError 使用。
function statusToCode(status: number): string | null {
  if (status === 0) return "network_error";
  if (status === 401) return "unauthorized";
  if (status === 403) return "forbidden";
  if (status === 404) return "not_found";
  if (status === 408) return "upstream_timeout";
  if (status === 422) return "validation_error";
  if (status === 429) return "rate_limited";
  if (status === 503) return "upstream_error";
  if (status >= 500 && status < 600) return "upstream_error";
  return null;
}

/** 单点查询：错误码 → 用户友好文案。映射缺失时返回 null。 */
export function errorCodeToMessage(code: string): string | null {
  return CODE_TITLE[code] ?? null;
}

/** 单点查询：错误码 → 推荐 action 标签。 */
export function errorCodeToAction(code: string): string | null {
  const action = CODE_ACTION[code];
  return action ? ACTION_LABEL[action] : null;
}

/** 把任意 error / code / 字符串归一成 NormalizedError，可直接渲染 toast/红条/对话框。 */
export function mapError(input: unknown): NormalizedError {
  // ApiError：优先用 code，未命中再 fallback 到 status
  if (input instanceof ApiError) {
    const fallbackCode = statusToCode(input.status) ?? "client_exception";
    const code = CODE_TITLE[input.code] ? input.code : fallbackCode;
    const action = CODE_ACTION[code] ?? "retry";
    return {
      title: CODE_TITLE[code] ?? input.message ?? "请求失败",
      description: CODE_DESC[code] ?? input.message,
      action,
      actionLabel: ACTION_LABEL[action],
      code,
      status: input.status,
    };
  }
  // 普通 Error
  if (input instanceof Error) {
    return {
      title: "发生异常",
      description: input.message || "未知错误",
      action: "retry",
      actionLabel: ACTION_LABEL.retry,
      code: "client_exception",
      status: 0,
    };
  }
  // 直接传字符串
  if (typeof input === "string" && input.trim().length > 0) {
    return {
      title: input.trim(),
      action: "retry",
      actionLabel: ACTION_LABEL.retry,
      code: "unknown",
      status: 0,
    };
  }
  return {
    title: "未知错误",
    action: "retry",
    actionLabel: ACTION_LABEL.retry,
    code: "unknown",
    status: 0,
  };
}

/** 仅取展示用文本（toast 单行场景）。 */
export function errorToText(input: unknown): string {
  const n = mapError(input);
  return n.description ? `${n.title}：${n.description}` : n.title;
}
