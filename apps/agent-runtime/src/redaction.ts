const SENSITIVE_KEY = /(api[-_]?key|authorization|cookie|secret|token|capability|password|prompt|content|text|base64|data)/iu;

export function safeErrorCode(value: unknown): string {
  if (typeof value !== "string") return "agent_runtime_error";
  const normalized = value.trim().toLowerCase();
  return /^[a-z][a-z0-9_]{0,63}$/u.test(normalized)
    ? normalized
    : "agent_runtime_error";
}

export function safeErrorMessage(value: unknown): string {
  void value;
  return "Agent Runtime request failed";
}

export function redact(value: unknown, key = ""): unknown {
  if (SENSITIVE_KEY.test(key)) return "[redacted]";
  if (Array.isArray(value)) return value.map((item) => redact(item, key));
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([name, item]) => [name, redact(item, name)]),
    );
  }
  if (typeof value === "string") {
    return value.replace(/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/gu, "[email]");
  }
  return value;
}

export function logRuntime(
  level: "info" | "warn" | "error",
  event: string,
  fields: Record<string, unknown> = {},
): void {
  const cleaned = redact(fields);
  const payload = JSON.stringify({
    level,
    event,
    ...(cleaned !== null && typeof cleaned === "object" && !Array.isArray(cleaned)
      ? cleaned
      : {}),
  });
  if (level === "error") console.error(payload);
  else if (level === "warn") console.warn(payload);
  else console.info(payload);
}
