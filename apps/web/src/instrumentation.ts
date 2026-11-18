// Next.js 16 服务端 instrumentation 入口（docs: 01-app/02-guides/instrumentation.md）。
// register() 在新的 Next 服务实例启动时被调用一次。
// 仅在 NEXT_PUBLIC_SENTRY_DSN（或 SENTRY_DSN）存在时启用；否则静默。
// 按 NEXT_RUNTIME 区分 node / edge，分别加载独立运行时 SDK，不引入 Next 构建插件。

import type { Instrumentation } from "next";

async function loadSentry() {
  return process.env.NEXT_RUNTIME === "edge"
    ? import("@sentry/vercel-edge")
    : import("@sentry/node");
}

const EMAIL_RE = /[\w.+-]+@[\w-]+\.[\w.-]+/g;

function redactString(input: string): string {
  if (!input) return input;
  let out = input.replace(EMAIL_RE, "<email>");
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
      if (
        /^(?:cookie|set-cookie|authorization|x-csrf-token|password|token|prompt|email)$/i.test(
          k,
        )
      ) {
        out[k] = "<redacted>";
        continue;
      }
      out[k] = redactObject(v, depth + 1);
    }
    return out as unknown as T;
  }
  return obj;
}

export async function register(): Promise<void> {
  const dsn = process.env.NEXT_PUBLIC_SENTRY_DSN ?? process.env.SENTRY_DSN;
  if (!dsn) return;

  try {
    const Sentry = await loadSentry();
    Sentry.init({
      dsn,
      tracesSampleRate: 0.1,
      environment: process.env.SENTRY_ENV ?? process.env.NODE_ENV,
      beforeBreadcrumb(breadcrumb) {
        try {
          const next = { ...breadcrumb };
          if (next.message) next.message = redactString(next.message);
          if (next.data) next.data = redactObject(next.data);
          return next;
        } catch {
          return breadcrumb;
        }
      },
      beforeSend(event) {
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
    // 服务端 Sentry 初始化失败不影响业务
  }
}

// 使用 Next 的 onRequestError hook 把未捕获错误送到 Sentry（DSN 未配置则 no-op）
export const onRequestError: Instrumentation.onRequestError = async (
  error, request, context,
): Promise<void> => {
  const dsn = process.env.NEXT_PUBLIC_SENTRY_DSN ?? process.env.SENTRY_DSN;
  if (!dsn) return;
  try {
    const Sentry = await loadSentry();
    Sentry.withScope((scope) => {
      // Do not send request query strings or headers (may contain credentials).
      scope.setContext("nextjs", {
        router_kind: context.routerKind,
        router_path: context.routePath,
        route_type: context.routeType,
        request_method: request.method,
      });
      scope.setTransactionName(`${request.method} ${context.routePath}`);
      Sentry.captureException(error, {
        mechanism: { handled: false, type: "auto.function.nextjs.on_request_error" },
      });
    });
    await Sentry.flush(2000);
  } catch {
    // Monitoring must never interrupt the response.
  }
};
