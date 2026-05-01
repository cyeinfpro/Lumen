// 轻量日志层。生产环境通过 Sentry 上报；dev 同时保留 console 便于调试。
// 为什么不直接到处 import @sentry/nextjs：
// 1. instrumentation 未启用 DSN 时，captureException 是 no-op，但仍会触发 SDK lazy-init
// 2. 生产 console.warn 会被浏览器扩展、隐私模式、CI 不一致地处理；统一走这一层
// 3. 也方便后续接 PII 脱敏 / 降采样

import * as Sentry from "@sentry/nextjs";

const isDev = process.env.NODE_ENV !== "production";

interface LogContext {
  /** 错误码（来自 ApiError 等） */
  code?: string;
  /** 上下文标签：用于 Sentry tag/breadcrumb */
  scope?: string;
  /** 原始 payload，可用于诊断；不应包含 PII */
  extra?: Record<string, unknown>;
}

function toError(input: unknown): Error {
  if (input instanceof Error) return input;
  if (typeof input === "string") return new Error(input);
  try {
    return new Error(JSON.stringify(input));
  } catch {
    return new Error("unknown error");
  }
}

/** 警告级日志：dev 输出 console.warn，生产作为 captureMessage(level=warning) 上报 */
export function logWarn(message: string, ctx?: LogContext): void {
  if (isDev) {
    const tag = ctx?.scope ?? "app";
    if (ctx?.extra) console.warn(`[${tag}] ${message}`, ctx.extra);
    else console.warn(`[${tag}] ${message}`);
  }
  try {
    Sentry.captureMessage(message, {
      level: "warning",
      tags: {
        scope: ctx?.scope,
        code: ctx?.code,
      },
      extra: ctx?.extra,
    });
  } catch {
    /* swallow */
  }
}

/** 错误级日志：dev 输出 console.error，生产 captureException 上报 */
export function logError(error: unknown, ctx?: LogContext): void {
  const err = toError(error);
  if (isDev) {
    if (ctx?.extra) console.error(`[${ctx?.scope ?? "app"}] ${err.message}`, err, ctx.extra);
    else console.error(`[${ctx?.scope ?? "app"}] ${err.message}`, err);
  }
  try {
    Sentry.captureException(err, {
      tags: {
        scope: ctx?.scope,
        code: ctx?.code,
      },
      extra: ctx?.extra,
    });
  } catch {
    /* swallow */
  }
}
