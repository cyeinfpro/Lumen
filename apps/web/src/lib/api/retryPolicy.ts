import { abortReason } from "./requestSignal";

export type RetryMode = "query" | "none" | "idempotent";

const RETRYABLE_STATUS = new Set([502, 503, 504]);
const DEFAULT_DELAYS_MS = [400, 1200];

export function isReplayableBody(
  body: BodyInit | null | undefined,
): boolean {
  return !(
    typeof ReadableStream !== "undefined" &&
    body instanceof ReadableStream
  );
}

export function retryModeFor(
  method: string,
  headers: Headers,
  body?: BodyInit | null,
): RetryMode {
  if (method === "GET" || method === "HEAD") return "query";
  if (!isReplayableBody(body)) return "none";
  return headers.has("Idempotency-Key") ||
    headers.has("idempotency-key")
    ? "idempotent"
    : "none";
}

export function shouldRetryStatus(status: number): boolean {
  return RETRYABLE_STATUS.has(status);
}

function retryAfterMs(response: Response): number | null {
  const raw = response.headers.get("retry-after")?.trim();
  if (!raw) return null;
  const seconds = Number(raw);
  if (Number.isFinite(seconds) && seconds >= 0) {
    return Math.min(5_000, Math.round(seconds * 1000));
  }
  const date = Date.parse(raw);
  return Number.isFinite(date)
    ? Math.min(5_000, Math.max(0, date - Date.now()))
    : null;
}

export function retryDelayMs(attempt: number, response?: Response): number {
  return (
    (response ? retryAfterMs(response) : null) ??
    DEFAULT_DELAYS_MS[attempt] ??
    DEFAULT_DELAYS_MS.at(-1) ??
    1200
  );
}

export async function waitForRetry(
  delayMs: number,
  signal?: AbortSignal | null,
): Promise<void> {
  if (signal?.aborted) throw abortReason(signal);
  await new Promise<void>((resolve, reject) => {
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, delayMs);
    const onAbort = () => {
      clearTimeout(timer);
      reject(abortReason(signal));
    };
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}
