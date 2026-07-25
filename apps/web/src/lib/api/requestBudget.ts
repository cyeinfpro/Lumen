export type RequestClass =
  | "query"
  | "command"
  | "idempotent-command"
  | "upload"
  | "long-operation";

export type RequestBudget =
  | { kind: "deadline"; totalMs: number }
  | { kind: "none" };

export const DEFAULT_API_TIMEOUT_MS = 30_000;

const REQUEST_BUDGET_MS: Record<RequestClass, number | null> = {
  query: DEFAULT_API_TIMEOUT_MS,
  command: DEFAULT_API_TIMEOUT_MS,
  "idempotent-command": 60_000,
  upload: 5 * 60_000,
  "long-operation": null,
};

export const NO_DEADLINE: RequestBudget = { kind: "none" };

export function deadline(totalMs: number): RequestBudget {
  if (!Number.isFinite(totalMs) || totalMs <= 0) {
    throw new TypeError("request deadline must be a positive finite number");
  }
  return { kind: "deadline", totalMs };
}

export function budgetFor(
  requestClass: RequestClass,
  explicit?: RequestBudget,
): RequestBudget {
  if (explicit) return explicit;
  const totalMs = REQUEST_BUDGET_MS[requestClass];
  if (totalMs === null) {
    throw new TypeError("long operations require an explicit request budget");
  }
  return deadline(totalMs);
}
