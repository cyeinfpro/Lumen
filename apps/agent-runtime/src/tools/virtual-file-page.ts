import { createHash } from "node:crypto";
import { encodedBytes, MAX_TOOL_RESULT_BYTES, safeTextEnd } from "./bounded-results.js";

export interface VirtualTextFile {
  readonly name: string;
  readonly content: string;
}
export interface FilePageOptions {
  readonly line_start?: number;
  readonly line_count?: number;
  readonly cursor?: string;
}
export interface FilePage {
  readonly name: string;
  readonly line_start: number;
  readonly line_end: number;
  readonly total_lines: number;
  readonly truncated: boolean;
  readonly content: string;
  readonly next_cursor: string | null;
}

export function readVirtualFilePage(file: VirtualTextFile, options: FilePageOptions): FilePage {
  const text = file.content;
  const version = createHash("sha256").update(JSON.stringify([file.name, text]), "utf8").digest("hex");
  const starts = [0];
  for (let offset = 0; offset < text.length; offset += 1) {
    if (text[offset] === "\n") starts.push(offset + 1);
  }
  const lineAt = (offset: number): number => {
    let low = 0;
    let high = starts.length;
    while (low + 1 < high) {
      const mid = Math.floor((low + high) / 2);
      const midpoint = starts[mid];
      if (midpoint !== undefined && midpoint <= offset) low = mid;
      else high = mid;
    }
    return low;
  };
  const count = options.line_count ?? 200;
  const requestedLine = options.line_start ?? 1;
  if (!Number.isInteger(count) || count < 1 || count > 400 ||
    !Number.isInteger(requestedLine) || requestedLine < 1 || requestedLine > 1_000_000) {
    throw new Error("invalid file page range");
  }
  let start = starts[requestedLine - 1] ?? text.length;
  if (options.cursor !== undefined) {
    const match = /^v1:([a-f0-9]{64}):(0|[1-9][0-9]*)$/u.exec(options.cursor);
    if (!match || match[1] !== version) throw new Error("invalid or stale file cursor");
    const parsed = Number(match[2]);
    if (!Number.isSafeInteger(parsed) || parsed < 0 || parsed > text.length || safeTextEnd(text, parsed) !== parsed) {
      throw new Error("invalid file cursor offset");
    }
    start = parsed;
  }
  const firstLine = lineAt(start);
  const target = starts[firstLine + count] ?? text.length;
  const page = (end: number): FilePage => ({
    name: file.name,
    line_start: firstLine + 1,
    line_end: lineAt(end > start ? end - 1 : start) + 1,
    total_lines: starts.length,
    truncated: end < text.length,
    content: text.slice(start, end),
    next_cursor: end < text.length ? `v1:${version}:${String(end)}` : null,
  });
  if (encodedBytes(page(target)) <= MAX_TOOL_RESULT_BYTES) return page(target);
  let low = start;
  let high = target;
  let best = start;
  while (low <= high) {
    const mid = Math.floor((low + high) / 2);
    const end = safeTextEnd(text, mid);
    if (encodedBytes(page(end)) <= MAX_TOOL_RESULT_BYTES) {
      best = Math.max(best, end);
      low = mid + 1;
    } else high = mid - 1;
  }
  if (best <= start && start < text.length) throw new Error("file page metadata exceeds the result budget");
  return page(best);
}
