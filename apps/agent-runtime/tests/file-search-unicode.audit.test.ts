import { describe, expect, it } from "vitest";

import { AGENT_TOOL_SEARCH_FILES, parseRuntimeRequest } from "../src/contracts.js";
import type { ToolRuntimeState } from "../src/tools/create-image.js";
import { createVirtualFileTools } from "../src/tools/virtual-files.js";
import { runtimeRequestV5 } from "./fixtures.js";

type SearchData = {
  truncated: boolean;
  matches: Array<{
    text: string; line_truncated: boolean; match_column: number;
    char_start: number; char_end: number;
  }>;
};

function parseSearchResult(result: unknown): SearchData {
  const content = (result as { content: Array<{ text: string }> }).content;
  const first = content[0];
  if (!first) throw new Error("missing search result");
  return JSON.parse(first.text) as SearchData;
}

function state(): ToolRuntimeState {
  return {
    ordinals: new Map(), errors: new Map(), modes: new Map(),
    nextOrdinal: 1, calls: 0, imageCalls: 0, webSearchCalls: 0, fileCalls: 0,
    acceptedImages: 0, successfulCalls: 0, failedCalls: 0, unknownResults: 0,
    lastErrorCode: null, limitReason: null,
  };
}

describe("virtual file search Unicode boundary audit", () => {
  for (const prefixLength of [498, 499]) {
    it(`keeps only complete code points after ${String(prefixLength)} ASCII characters`, async () => {
      const prefix = "a".repeat(prefixLength);
      const content = prefix + "🙂";
      const request = parseRuntimeRequest(runtimeRequestV5({
        allowed_tools: [AGENT_TOOL_SEARCH_FILES],
        workspace_files: [{
          name: "sample.txt", mime_type: "text/plain",
          size: Buffer.byteLength(content, "utf8"), content,
        }],
        tool_policy: {
          max_image_tool_calls: 0, max_images_per_run: 4,
          max_web_search_calls: 0, max_file_tool_calls: 1, max_tool_calls: 1,
        },
      }));
      const runtimeState = state();
      const tool = createVirtualFileTools(request, runtimeState).find(
        (candidate) => candidate.name === AGENT_TOOL_SEARCH_FILES,
      );
      if (!tool) throw new Error("File search tool is missing");
      // Exercise the registered local tool itself. This implementation uses
      // only call ID and parameters, not a provider/session context.
      const result: unknown = await Reflect.apply(tool.execute.bind(tool), tool, [
        "unicode-search-1", { query: "a" }, undefined,
      ]);
      const expectedText = JSON.stringify({
        query: "a", searched_files: ["sample.txt"],
        matches: [{ name: "sample.txt", line: 1, text: prefixLength === 498 ? content : prefix,
          char_start: 0, char_end: prefixLength === 498 ? 500 : 499,
          match_column: 0, line_truncated: prefixLength === 499 }],
        truncated: false,
      });
      expect(result).toEqual({
        content: [{ type: "text", text: expectedText }],
        details: { ordinal: 1, mode: "file_search", result_text: expectedText },
      });
      expect(runtimeState.successfulCalls).toBe(1);
      expect(runtimeState.fileCalls).toBe(1);
    });
  }
});

it("returns the late hit with Unicode-safe context and independent truncation flags", async () => {
  const content = "İ".repeat(350) + "🙂".repeat(180) + "needle" + "z".repeat(600);
  const request = parseRuntimeRequest(runtimeRequestV5({
    allowed_tools: [AGENT_TOOL_SEARCH_FILES],
    workspace_files: [{ name: "late.txt", mime_type: "text/plain", size: Buffer.byteLength(content), content }],
    tool_policy: { max_image_tool_calls: 0, max_images_per_run: 4,
      max_web_search_calls: 0, max_file_tool_calls: 1, max_tool_calls: 1 },
  }));
  const tool = createVirtualFileTools(request, state()).find(t => t.name === AGENT_TOOL_SEARCH_FILES);
  if (!tool) throw new Error("missing tool");
  const result: unknown = await Reflect.apply(tool.execute.bind(tool), tool, ["late", { query: "needle", max_matches: 1 }, undefined]);
  const data = parseSearchResult(result);
  const match = data.matches[0];
  if (!match) throw new Error("missing search match");
  expect(data.truncated).toBe(false);
  expect(match.text).toContain("needle");
  expect(match.line_truncated).toBe(true);
  expect(match.match_column).toBe(content.indexOf("needle"));
  expect(match.text).toBe(content.slice(match.char_start, match.char_end));
  expect(Buffer.from(match.text, "utf8").toString("utf8")).toBe(match.text);
});

it("keeps contextual Greek lowercasing and original columns after expanding folds", async () => {
  const content = "İ".repeat(350) + " ΟΣ";
  const request = parseRuntimeRequest(runtimeRequestV5({
    allowed_tools: [AGENT_TOOL_SEARCH_FILES],
    workspace_files: [{ name: "greek.txt", mime_type: "text/plain", size: Buffer.byteLength(content), content }],
    tool_policy: { max_image_tool_calls: 0, max_images_per_run: 4,
      max_web_search_calls: 0, max_file_tool_calls: 1, max_tool_calls: 1 },
  }));
  const tool = createVirtualFileTools(request, state()).find(t => t.name === AGENT_TOOL_SEARCH_FILES);
  if (!tool) throw new Error("missing tool");
  const result: unknown = await Reflect.apply(tool.execute.bind(tool), tool, ["greek", { query: "ος" }, undefined]);
  const data = parseSearchResult(result);
  const match = data.matches[0];
  if (!match) throw new Error("missing search match");
  expect(data.matches).toHaveLength(1);
  expect(match.text).toContain("ΟΣ");
  expect(match.match_column).toBe(content.indexOf("ΟΣ"));
});
