import { describe, expect, it } from "vitest";

import { AGENT_TOOL_SEARCH_FILES, parseRuntimeRequest } from "../src/contracts.js";
import type { ToolRuntimeState } from "../src/tools/create-image.js";
import { createVirtualFileTools } from "../src/tools/virtual-files.js";
import { runtimeRequestV5 } from "./fixtures.js";

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
        matches: [{ name: "sample.txt", line: 1, text: prefixLength === 498 ? content : prefix }],
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
