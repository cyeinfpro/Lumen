import { Type } from "typebox";
import { defineTool, type ToolDefinition } from "@earendil-works/pi-coding-agent";

import {
  AGENT_TOOL_LIST_FILES,
  AGENT_TOOL_READ_FILE,
  AGENT_TOOL_SEARCH_FILES,
  type RuntimeRequest,
} from "../contracts.js";
import type { ToolRuntimeState } from "./create-image.js";
import {
  beginLocalTool,
  completeLocalTool,
  failLocalTool,
} from "./local-tool-state.js";

function filesFor(request: RuntimeRequest) {
  return request.version === 5 ? request.workspace_files : [];
}

function findFile(request: RuntimeRequest, name: string) {
  const normalized = name.trim().toLocaleLowerCase();
  return filesFor(request).find(
    (file) => file.name.toLocaleLowerCase() === normalized,
  );
}

function complete(
  state: ToolRuntimeState,
  ordinal: number,
  mode: "file_list" | "file_read" | "file_search",
  value: unknown,
) {
  const resultText = JSON.stringify(value).slice(0, 20_000);
  completeLocalTool(state);
  return {
    content: [{ type: "text" as const, text: resultText }],
    details: { ordinal, mode, result_text: resultText },
  };
}

function listFilesTool(
  request: RuntimeRequest,
  state: ToolRuntimeState,
): ToolDefinition {
  return defineTool({
    name: AGENT_TOOL_LIST_FILES,
    label: "List files",
    description:
      "List the bounded virtual text files supplied by the user for this turn. This tool cannot access host or container paths.",
    executionMode: "sequential",
    parameters: Type.Object({}, { additionalProperties: false }),
    async execute(toolCallId) {
      const ordinal = beginLocalTool(
        request,
        state,
        toolCallId,
        "file_list",
        "file",
      );
      return complete(
        state,
        ordinal,
        "file_list",
        filesFor(request).map((file) => ({
          name: file.name,
          mime_type: file.mime_type,
          size: file.size,
          lines: file.content.split(/\r?\n/u).length,
        })),
      );
    },
  });
}

function readFileTool(
  request: RuntimeRequest,
  state: ToolRuntimeState,
): ToolDefinition {
  return defineTool({
    name: AGENT_TOOL_READ_FILE,
    label: "Read file",
    description:
      "Read a bounded line range from one user-supplied virtual text file by exact name. This tool cannot access host or container paths.",
    executionMode: "sequential",
    parameters: Type.Object(
      {
        name: Type.String({ minLength: 1, maxLength: 128 }),
        line_start: Type.Optional(Type.Integer({ minimum: 1, maximum: 1_000_000 })),
        line_count: Type.Optional(Type.Integer({ minimum: 1, maximum: 400 })),
      },
      { additionalProperties: false },
    ),
    async execute(toolCallId, params) {
      const ordinal = beginLocalTool(
        request,
        state,
        toolCallId,
        "file_read",
        "file",
      );
      const file = findFile(request, params.name);
      if (!file) failLocalTool(state, toolCallId, "agent_file_not_found");
      const lines = file.content.split(/\r?\n/u);
      const lineStart = params.line_start ?? 1;
      const lineCount = params.line_count ?? 200;
      const selected = lines.slice(lineStart - 1, lineStart - 1 + lineCount);
      const content = selected.join("\n").slice(0, 18_000);
      return complete(state, ordinal, "file_read", {
        name: file.name,
        line_start: lineStart,
        line_end: lineStart + Math.max(0, selected.length - 1),
        total_lines: lines.length,
        truncated: selected.join("\n").length > content.length,
        content,
      });
    },
  });
}

function searchFilesTool(
  request: RuntimeRequest,
  state: ToolRuntimeState,
): ToolDefinition {
  return defineTool({
    name: AGENT_TOOL_SEARCH_FILES,
    label: "Search files",
    description:
      "Search literal text across user-supplied virtual files. Returns bounded matching lines and cannot access host or container paths.",
    executionMode: "sequential",
    parameters: Type.Object(
      {
        query: Type.String({ minLength: 1, maxLength: 256 }),
        name: Type.Optional(Type.String({ minLength: 1, maxLength: 128 })),
        max_matches: Type.Optional(Type.Integer({ minimum: 1, maximum: 50 })),
      },
      { additionalProperties: false },
    ),
    async execute(toolCallId, params) {
      const ordinal = beginLocalTool(
        request,
        state,
        toolCallId,
        "file_search",
        "file",
      );
      const query = params.query.trim();
      if (!query) failLocalTool(state, toolCallId, "agent_tool_preflight_failed");
      const selectedFile = params.name ? findFile(request, params.name) : undefined;
      if (params.name && !selectedFile) {
        failLocalTool(state, toolCallId, "agent_file_not_found");
      }
      const candidates = selectedFile ? [selectedFile] : filesFor(request);
      const maximum = params.max_matches ?? 20;
      const normalized = query.toLocaleLowerCase();
      const matches: Array<{ name: string; line: number; text: string }> = [];
      for (const file of candidates) {
        for (const [index, line] of file.content.split(/\r?\n/u).entries()) {
          if (!line.toLocaleLowerCase().includes(normalized)) continue;
          matches.push({
            name: file.name,
            line: index + 1,
            text: line.trim().slice(0, 500),
          });
          if (matches.length >= maximum) break;
        }
        if (matches.length >= maximum) break;
      }
      return complete(state, ordinal, "file_search", {
        query,
        searched_files: candidates.map((file) => file.name),
        matches,
        truncated: matches.length >= maximum,
      });
    },
  });
}

export function createVirtualFileTools(
  request: RuntimeRequest,
  state: ToolRuntimeState,
): ToolDefinition[] {
  return [
    listFilesTool(request, state),
    readFileTool(request, state),
    searchFilesTool(request, state),
  ];
}
