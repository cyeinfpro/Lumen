// 轻量日志层。生产环境通过 Sentry 上报；dev 同时保留 console 便于调试。
// 为什么不直接到处 import @sentry/nextjs：
// 1. instrumentation 未启用 DSN 时，captureException 是 no-op，但仍会触发 SDK lazy-init
// 2. 生产 console.warn 会被浏览器扩展、隐私模式、CI 不一致地处理；统一走这一层
// 3. 也方便后续接 PII 脱敏 / 降采样

const isDev = process.env.NODE_ENV !== "production";
const sentryDsn = process.env.NEXT_PUBLIC_SENTRY_DSN;
type SentryModule = typeof import("@sentry/nextjs");
let sentryPromise: Promise<SentryModule> | null = null;

interface LogContext {
  /** 错误码（来自 ApiError 等） */
  code?: string;
  /** 上下文标签：用于 Sentry tag/breadcrumb */
  scope?: string;
  /** 原始 payload，可用于诊断；不应包含 PII（上报前会统一过 redactExtra 脱敏） */
  extra?: Record<string, unknown>;
}

// ── PII / 凭据脱敏 ──
// extra 由各调用点自由拼装，实践中很容易顺手带进 token、api key、邮箱。
// 一旦进了 Sentry 就等于把凭据写进第三方系统，因此在这一层统一兜底：
// 1) 键名命中敏感词 → 整个值替换为 [redacted]
// 2) 字符串值命中邮箱 / Bearer / 长随机串 → 就地替换命中片段
// 有意做成纯字符串启发式：不引依赖、不阻塞上报路径。
const SENSITIVE_KEY_RE =
  /(token|secret|password|passwd|pwd|api[-_]?key|apikey|authorization|auth|cookie|session|credential|signature|sign|private|email|mail|phone|mobile)/i;
const EMAIL_RE = /[\w.+-]+@[\w-]+(?:\.[\w-]+)+/g;
const BEARER_RE = /\b(bearer|basic)\s+[\w.\-+/=]{8,}/gi;
// JWT / 长随机串（token、hash、密钥常见形态）；短 id（uuid 段、convId）不受影响
const JWT_RE = /\beyJ[\w-]{6,}\.[\w-]{6,}\.[\w-]{6,}\b/g;
const LONG_SECRET_RE = /\b[A-Za-z0-9_-]{40,}\b/g;
const REDACTED = "[redacted]";
// 防御深层嵌套对象导致的递归开销
const MAX_REDACT_DEPTH = 4;

function redactString(value: string): string {
  return value
    .replace(BEARER_RE, (_match, scheme: string) => `${scheme} ${REDACTED}`)
    .replace(JWT_RE, REDACTED)
    .replace(EMAIL_RE, REDACTED)
    .replace(LONG_SECRET_RE, REDACTED);
}

function redactValue(value: unknown, depth: number): unknown {
  if (typeof value === "string") return redactString(value);
  if (value == null || typeof value !== "object") return value;
  if (depth >= MAX_REDACT_DEPTH) return REDACTED;
  if (Array.isArray(value)) {
    return value.map((item) => redactValue(item, depth + 1));
  }
  if (value instanceof Error) return redactString(value.message);
  const out: Record<string, unknown> = {};
  for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
    out[key] = SENSITIVE_KEY_RE.test(key)
      ? REDACTED
      : redactValue(item, depth + 1);
  }
  return out;
}

/** 对 ctx.extra 做键名 + 值形态双重脱敏；返回新对象，不改调用方入参 */
export function redactExtra(
  extra: Record<string, unknown> | undefined,
): Record<string, unknown> | undefined {
  if (!extra) return undefined;
  try {
    return redactValue(extra, 0) as Record<string, unknown>;
  } catch {
    // 脱敏失败宁可丢诊断信息，也不能把原始值透出去
    return { redaction_failed: true };
  }
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

function loadSentry(): Promise<SentryModule> | null {
  if (!sentryDsn) return null;
  sentryPromise ??= import("@sentry/nextjs");
  return sentryPromise;
}

/** 警告级日志：dev 输出 console.warn，生产作为 captureMessage(level=warning) 上报 */
export function logWarn(message: string, ctx?: LogContext): void {
  // dev 也走脱敏：本地 console 会被扩展/录屏采集，且能让开发者及早发现误传 PII
  const extra = redactExtra(ctx?.extra);
  if (isDev) {
    const tag = ctx?.scope ?? "app";
    if (extra) console.warn(`[${tag}] ${message}`, extra);
    else console.warn(`[${tag}] ${message}`);
  }
  void loadSentry()?.then((Sentry) => {
    Sentry.captureMessage(message, {
      level: "warning",
      tags: {
        scope: ctx?.scope,
        code: ctx?.code,
      },
      extra,
    });
  }).catch(() => {
    /* swallow */
  });
}

/** 错误级日志：dev 输出 console.error，生产 captureException 上报 */
export function logError(error: unknown, ctx?: LogContext): void {
  const err = toError(error);
  const extra = redactExtra(ctx?.extra);
  if (isDev) {
    if (extra) console.error(`[${ctx?.scope ?? "app"}] ${err.message}`, err, extra);
    else console.error(`[${ctx?.scope ?? "app"}] ${err.message}`, err);
  }
  void loadSentry()?.then((Sentry) => {
    Sentry.captureException(err, {
      tags: {
        scope: ctx?.scope,
        code: ctx?.code,
      },
      extra,
    });
  }).catch(() => {
    /* swallow */
  });
}
