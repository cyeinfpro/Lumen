// Next.js 16 客户端侧 instrumentation 入口（docs: 01-app/03-api-reference/03-file-conventions/instrumentation-client.md）。
// 在 HTML 加载后、React hydrate 前执行，适合错误监控/性能打点初始化。
// 仅在配置了 NEXT_PUBLIC_SENTRY_DSN 时初始化 Sentry，否则完全静默（CI/本地/预览环境不需要）。
// V1 不启用 Session Replay（replaysSessionSampleRate = 0 / replaysOnErrorSampleRate = 0）。

import * as Sentry from "@sentry/nextjs";
import type { Breadcrumb, ErrorEvent as SentryErrorEvent } from "@sentry/nextjs";

const dsn = process.env.NEXT_PUBLIC_SENTRY_DSN;

// 邮箱 / 长 prompt 等可能进 breadcrumb data。脱敏放在 init 处统一管。
const EMAIL_RE = /[\p{L}\p{N}._%+-]+@[\p{L}\p{N}.-]+\.[\p{L}\p{N}.-]+/giu;
const INVISIBLE_UNICODE_RE = /[\u200B-\u200D\u2060\uFEFF]/g;
const SENSITIVE_KEY_RE =
  /^(?:cookie|set-cookie|authorization|x-csrf-token|password|token|prompt|email)$/i;

function normalizeForRedaction(input: string): string {
  return input.normalize("NFKC").replace(INVISIBLE_UNICODE_RE, "");
}

function redactString(input: string): string {
  if (!input) return input;
  let out = normalizeForRedaction(input).replace(EMAIL_RE, "<email>");
  // 超长字符串截断（疑似 prompt / token）
  if (out.length > 256) out = out.slice(0, 256) + "…<truncated>";
  return out;
}

function redactObject<T>(obj: T, depth = 0): T {
  if (depth > 4 || obj == null) return obj;
  if (typeof obj === "string") return redactString(obj) as unknown as T;
  if (Array.isArray(obj)) {
    return obj.map((v) => redactObject(v, depth + 1)) as unknown as T;
  }
  if (typeof obj === "object") {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(obj as Record<string, unknown>)) {
      // 直接丢弃可能含敏感信息的 key
      if (SENSITIVE_KEY_RE.test(normalizeForRedaction(k))) {
        out[k] = "<redacted>";
        continue;
      }
      out[k] = redactObject(v, depth + 1);
    }
    return out as unknown as T;
  }
  return obj;
}

if (dsn) {
  try {
    Sentry.init({
      dsn,
      tracesSampleRate: 0.1,
      replaysSessionSampleRate: 0,
      replaysOnErrorSampleRate: 0,
      environment: process.env.NEXT_PUBLIC_SENTRY_ENV ?? process.env.NODE_ENV,
      beforeBreadcrumb(breadcrumb: Breadcrumb): Breadcrumb | null {
        try {
          const next = { ...breadcrumb };
          if (next.message) next.message = redactString(next.message);
          if (next.data) next.data = redactObject(next.data);
          return next;
        } catch {
          return breadcrumb;
        }
      },
      beforeSend(event: SentryErrorEvent): SentryErrorEvent | null {
        try {
          if (event.user?.email) event.user.email = "<redacted>";
          if (event.request) event.request = redactObject(event.request);
          if (event.extra) event.extra = redactObject(event.extra);
          if (event.contexts) event.contexts = redactObject(event.contexts);
          return event;
        } catch {
          return event;
        }
      },
    });
  } catch {
    // Sentry 初始化失败绝不连累页面交互
  }
}

// Router 过渡事件转发到 Sentry（无 DSN 时走 no-op）
export function onRouterTransitionStart(
  href: string,
  navigationType: "push" | "replace" | "traverse",
): void {
  if (!dsn) return;
  try {
    Sentry.captureRouterTransitionStart?.(href, navigationType);
  } catch {
    // swallow
  }
}
