export const MAX_TOOL_RESULT_BYTES = 20_000;

export function encodedBytes(value: unknown): number {
  return Buffer.byteLength(JSON.stringify(value), "utf8");
}

export function safeTextEnd(text: string, end: number): number {
  if (end > 0 && end < text.length) {
    const previous = text.charCodeAt(end - 1);
    const next = text.charCodeAt(end);
    if (previous >= 0xd800 && previous <= 0xdbff && next >= 0xdc00 && next <= 0xdfff) {
      return end - 1;
    }
  }
  return end;
}

export function encodeBoundedToolResult(value: unknown): string {
  // Clone the JSON value; never mutate retained receipts or cut serialized JSON.
  const result: unknown = JSON.parse(JSON.stringify(value));
  while (encodedBytes(result) > MAX_TOOL_RESULT_BYTES) {
    if (Array.isArray(result)) {
      // File listings are bounded to eight names by the request schema.
      throw new Error("file listing exceeds the result contract");
    }
    if (result === null || typeof result !== "object") {
      throw new Error("tool result exceeds the result contract");
    }
    const record = result as Record<string, unknown>;
    record.truncated = true;
    const rows = [record.matches, record.sources].find(
      (items) => Array.isArray(items) && items.length > 0,
    );
    if (Array.isArray(rows)) {
      rows.pop();
      continue;
    }
    // Never shorten a file page here: its cursor must describe its exact bytes.
    // Public search answers may be shortened without changing source URLs.
    if (typeof record.answer === "string" && record.answer.length > 0) {
      const end = safeTextEnd(record.answer, Math.floor(record.answer.length / 2));
      record.answer = record.answer.slice(0, end) || null;
      continue;
    }
    throw new Error("tool result metadata exceeds the result contract");
  }
  return JSON.stringify(result);
}
