import { runtimeToolPolicy, type RuntimeRequest } from "../contracts.js";
import {
  ordinalFor,
  rejectTool,
  type ToolMode,
  type ToolRuntimeState,
} from "./create-image.js";
import { ToolGatewayError } from "./gateway.js";

export type LocalToolKind = "web" | "file";

export function beginLocalTool(
  request: RuntimeRequest,
  state: ToolRuntimeState,
  toolCallId: string,
  mode: ToolMode,
  kind: LocalToolKind,
): number {
  const policy = runtimeToolPolicy(request);
  const ordinal = ordinalFor(state, toolCallId);
  state.modes.set(toolCallId, mode);
  if (state.calls >= policy.max_tool_calls) {
    state.limitReason = "tool_calls";
    rejectTool(state, toolCallId, "agent_tool_limit_reached");
  }
  if (kind === "web" && state.webSearchCalls >= policy.max_web_search_calls) {
    state.limitReason = "web_search_calls";
    rejectTool(state, toolCallId, "agent_web_search_limit_reached");
  }
  if (kind === "file" && state.fileCalls >= policy.max_file_tool_calls) {
    state.limitReason = "file_tool_calls";
    rejectTool(state, toolCallId, "agent_file_tool_limit_reached");
  }
  state.calls += 1;
  if (kind === "web") state.webSearchCalls += 1;
  else state.fileCalls += 1;
  return ordinal;
}

export function completeLocalTool(state: ToolRuntimeState): void {
  state.successfulCalls += 1;
}

export function failLocalTool(
  state: ToolRuntimeState,
  toolCallId: string,
  code: string,
): never {
  if (!state.errors.has(toolCallId)) {
    state.errors.set(toolCallId, { code, resultUnknown: false });
    state.failedCalls += 1;
    state.lastErrorCode = code;
  }
  throw new ToolGatewayError(code, false);
}
