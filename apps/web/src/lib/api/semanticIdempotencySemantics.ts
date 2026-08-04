const DEFINITIVE_FAILURE_DISPOSITION = "definitive-terminal-failure";
const REPLAY_UNAVAILABLE_CODE = "idempotency_replay_unavailable";
const TERMINAL_PERSIST_UNKNOWN_CODE = "idempotency_terminal_persist_unknown";

type DefinitiveFailure = {
  semanticIdempotencyDisposition?: unknown;
};

function sortJsonValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortJsonValue);
  if (value === null || typeof value !== "object") return value;
  const record = value as Record<string, unknown>;
  const sorted: Record<string, unknown> = {};
  for (const key of Object.keys(record).sort()) {
    sorted[key] = sortJsonValue(record[key]);
  }
  return sorted;
}

export function semanticRequestFingerprint(
  scope: unknown,
  payload: unknown,
): string {
  return JSON.stringify(sortJsonValue({ payload, scope }));
}

export function markDefinitiveRequestFailure<T extends object>(error: T): T {
  Object.defineProperty(error, "semanticIdempotencyDisposition", {
    configurable: true,
    value: DEFINITIVE_FAILURE_DISPOSITION,
  });
  return error;
}

export function isDefinitiveRequestFailure(error: unknown): boolean {
  return (
    error !== null &&
    typeof error === "object" &&
    (error as DefinitiveFailure).semanticIdempotencyDisposition ===
      DEFINITIVE_FAILURE_DISPOSITION
  );
}

function hasFailureCode(error: unknown, expected: string): boolean {
  if (error === null || typeof error !== "object") return false;
  const direct = (error as { code?: unknown }).code;
  if (typeof direct === "string" && direct.trim() === expected) return true;
  const payload = (error as { payload?: unknown }).payload;
  if (payload === null || typeof payload !== "object") return false;
  const payloadCode = (payload as { code?: unknown }).code;
  if (typeof payloadCode === "string" && payloadCode.trim() === expected) {
    return true;
  }
  for (const field of ["error", "detail"] as const) {
    const nested = (payload as Record<string, unknown>)[field];
    if (nested && typeof nested === "object") {
      const nestedCode = (nested as { code?: unknown }).code;
      if (typeof nestedCode === "string" && nestedCode.trim() === expected) {
        return true;
      }
    }
  }
  return false;
}

export function isAmbiguousRequestFailure(error: unknown): boolean {
  if (
    hasFailureCode(error, REPLAY_UNAVAILABLE_CODE) ||
    hasFailureCode(error, TERMINAL_PERSIST_UNKNOWN_CODE)
  ) {
    return true;
  }
  if (isDefinitiveRequestFailure(error)) return false;
  if (error === null || error === undefined) return true;
  if (typeof error !== "object" || !("status" in error)) return true;
  const status = (error as { status?: unknown }).status;
  if (typeof status !== "number" || status <= 0) return true;
  return status === 408 || status === 425 || status === 429 || status >= 500;
}
