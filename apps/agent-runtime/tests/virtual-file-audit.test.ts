import { describe, expect, it } from "vitest";

import {
  encodeBoundedToolResult,
  encodedBytes,
  MAX_TOOL_RESULT_BYTES,
} from "../src/tools/bounded-results.js";
import { readVirtualFilePage } from "../src/tools/virtual-file-page.js";

function readAll(content: string): string {
  const file = { name: "notes.txt", content };
  const pieces: string[] = [];
  let cursor: string | undefined;
  const seen = new Set<string>();
  for (let index = 0; index < 100; index += 1) {
    const page = readVirtualFilePage(file, { line_count: 2, ...(cursor ? { cursor } : {}) });
    expect(encodedBytes(page)).toBeLessThanOrEqual(MAX_TOOL_RESULT_BYTES);
    expect(JSON.parse(encodeBoundedToolResult(page))).toEqual(page);
    pieces.push(page.content);
    if (page.next_cursor === null) return pieces.join("");
    expect(seen.has(page.next_cursor)).toBe(false);
    expect(page.content.length).toBeGreaterThan(0);
    seen.add(page.next_cursor);
    cursor = page.next_cursor;
  }
  throw new Error("File pagination failed to terminate");
}

describe("B15/B16 native virtual file regressions", () => {
  it.each([
    ["single long line", "x".repeat(40_000) + "TAIL"],
    ["escaped JSON text", '"\\'.repeat(18_000) + "TAIL"],
    ["Unicode", "图片🙂".repeat(8_000) + "尾部"],
    ["line boundaries", "alpha\r\nbeta\n" + "z".repeat(25_000) + "\r\nTAIL\n"],
    ["empty file", ""],
  ])("reconstructs %s without dropping or duplicating text", (_name, content) => {
    expect(readAll(content)).toBe(content);
  });

  it("rejects a cursor after the file changes", () => {
    const file = { name: "notes.txt", content: "a".repeat(40_000) };
    const cursor = readVirtualFilePage(file, {}).next_cursor;
    if (cursor === null) throw new Error("Long-file fixture must produce a continuation cursor");
    expect(cursor).not.toBeNull();
    expect(() => readVirtualFilePage({ ...file, content: file.content + "b" }, {
      cursor,
    })).toThrow(/stale/u);
  });

  it("binds continuation to the file identity", () => {
    const file = { name: "notes.txt", content: "a".repeat(40_000) };
    const cursor = readVirtualFilePage(file, {}).next_cursor;
    if (cursor === null) throw new Error("Long-file fixture must produce a continuation cursor");
    expect(() => readVirtualFilePage({ ...file, name: "other.txt" }, {
      cursor,
    })).toThrow(/stale/u);
  });

  it("reduces search rows without cutting JSON or rewriting retained URLs", () => {
    const original = {
      query: "example",
      sources: Array.from({ length: 50 }, (_, index) => ({
        title: `Source ${String(index)}`,
        url: `https://example.test/source/${String(index)}?name=a%20b`,
        snippet: '"\\'.repeat(400),
      })),
    };
    const encoded = encodeBoundedToolResult(original);
    expect(Buffer.byteLength(encoded, "utf8")).toBeLessThanOrEqual(MAX_TOOL_RESULT_BYTES);
    const output = JSON.parse(encoded) as typeof original & { truncated: boolean };
    expect(output.truncated).toBe(true);
    expect(output.sources.length).toBeGreaterThan(0);
    expect(output.sources).toEqual(original.sources.slice(0, output.sources.length));
    expect(original.sources).toHaveLength(50);
  });
});
