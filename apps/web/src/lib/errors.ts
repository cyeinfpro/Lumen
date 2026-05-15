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
  INSUFFICIENT_BALANCE: "余额不足",
  WALLET_FROZEN: "钱包已冻结",
  WALLET_HAS_ACTIVE_HOLDS: "钱包有待结算任务",
  NO_ACTIVE_API_KEY: "需要重新绑定 API Key",
  ACCOUNT_MODE_FORBIDDEN: "当前账号模式不可用",
  ACCOUNT_NOT_WALLET: "目标不是钱包账号",
  CODE_NOT_FOUND: "兑换码不存在",
  CODE_REVOKED: "兑换码已撤销",
  CODE_EXPIRED: "兑换码已过期",
  CODE_EXHAUSTED: "兑换码已兑完",
  CODE_ALREADY_USED: "兑换码已使用",
  PRICING_NOT_CONFIGURED: "价格未配置",
  REDEMPTION_SECRET_NOT_CONFIGURED: "兑换码功能未配置",
  BOOTSTRAP_INCOMPLETE: "计费功能未初始化",
  BILLING_DISABLED: "计费功能已关闭",
  ALREADY_REVOKED: "兑换码已撤销",
  THRESHOLDS_PRICING_MISMATCH: "尺寸档位和价格不一致",
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
  INSUFFICIENT_BALANCE: "请先进入钱包兑换或联系管理员充值",
  WALLET_FROZEN: "请充值或联系管理员处理后再继续使用",
  WALLET_HAS_ACTIVE_HOLDS: "请先取消或等待正在进行的任务结束，再切换账号模式",
  NO_ACTIVE_API_KEY: "请到设置里的 API Keys 重新绑定一张可用的密钥",
  ACCOUNT_MODE_FORBIDDEN: "钱包账号和 BYOK 账号入口互斥，请切换到对应入口",
  ACCOUNT_NOT_WALLET: "BYOK 账号不能做钱包调账",
  CODE_NOT_FOUND: "请检查兑换码是否输入完整",
  CODE_REVOKED: "这张兑换码已被管理员撤销",
  CODE_EXPIRED: "这张兑换码已超过有效期",
  CODE_EXHAUSTED: "这张兑换码的可用次数已经用完",
  CODE_ALREADY_USED: "该账号已经兑换过这张码",
  PRICING_NOT_CONFIGURED: "管理员需要先补齐对应模型或尺寸档位的价格",
  REDEMPTION_SECRET_NOT_CONFIGURED: "请先在管理后台配置兑换码 secret",
  BOOTSTRAP_INCOMPLETE: "管理员需要先完成计费初始化",
  BILLING_DISABLED: "请联系管理员开启计费功能后再兑换",
  ALREADY_REVOKED: "这张兑换码此前已经被撤销",
  THRESHOLDS_PRICING_MISMATCH: "请确保每个尺寸档位都有启用的价格规则",
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
  INSUFFICIENT_BALANCE: "back",
  WALLET_FROZEN: "back",
  WALLET_HAS_ACTIVE_HOLDS: "back",
  NO_ACTIVE_API_KEY: "back",
  ACCOUNT_MODE_FORBIDDEN: "back",
  ACCOUNT_NOT_WALLET: "back",
  CODE_NOT_FOUND: "back",
  CODE_REVOKED: "back",
  CODE_EXPIRED: "back",
  CODE_EXHAUSTED: "back",
  CODE_ALREADY_USED: "back",
  PRICING_NOT_CONFIGURED: "back",
  REDEMPTION_SECRET_NOT_CONFIGURED: "back",
  BOOTSTRAP_INCOMPLETE: "back",
  BILLING_DISABLED: "back",
  ALREADY_REVOKED: "back",
  THRESHOLDS_PRICING_MISMATCH: "back",
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

/**
 * 错误码 → 单行可直接展示的完整文案（title + description）。映射缺失时返回 null，
 * 让调用方回退到原始 message。useChatStore.composerError 之类的单行展示场景用它。
 */
export function errorCodeToFullText(code: string): string | null {
  const title = CODE_TITLE[code];
  if (!title) return null;
  const desc = CODE_DESC[code];
  return desc ? `${title}：${desc}` : title;
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
