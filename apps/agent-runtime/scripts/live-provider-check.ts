import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";

import { parseRuntimeRequest, type RuntimeRequest } from "../src/contracts.js";
import { CollectingEventWriter } from "../src/ndjson.js";
import {
  executeAgentRun,
  RuntimeExecutionError,
  type ExecuteResult,
} from "../src/runtime.js";

if (process.env.AGENT_LIVE_CHECK !== "1") {
  console.error("Refusing a billable provider call: set AGENT_LIVE_CHECK=1 explicitly.");
  process.exitCode = 2;
} else {
  const apiKey = process.env.AGENT_LIVE_API_KEY?.trim();
  const baseUrl = process.env.AGENT_LIVE_BASE_URL?.trim();
  const model = process.env.AGENT_LIVE_MODEL?.trim();
  const api = process.env.AGENT_LIVE_API?.trim() ?? "openai-responses";
  const scenario = process.env.AGENT_LIVE_SCENARIO?.trim() || "text";
  if (!apiKey || !baseUrl || !model) {
    throw new Error("AGENT_LIVE_API_KEY, AGENT_LIVE_BASE_URL, and AGENT_LIVE_MODEL are required");
  }
  if (!new Set(["openai-responses", "openai-completions", "anthropic-messages"]).has(api)) {
    throw new Error("AGENT_LIVE_API is unsupported");
  }
  const runId = randomUUID();
  const vision = scenario === "vision";
  const tool = scenario === "tool";
  const referencePath = process.env.AGENT_LIVE_REFERENCE_IMAGE?.trim();
  const references = vision
    ? [
        {
          reference_label: "ref_1" as const,
          role: "reference",
          display_label: "live compatibility reference",
          mime_type: "image/png" as const,
          data_base64: referencePath
            ? readFileSync(referencePath).toString("base64")
            : "",
        },
      ]
    : [];
  if (vision && !referencePath) {
    throw new Error("AGENT_LIVE_REFERENCE_IMAGE is required for the vision scenario");
  }
  const toolGatewayUrl = process.env.AGENT_LIVE_TOOL_GATEWAY_URL?.trim() || null;
  const toolCapability = process.env.AGENT_LIVE_TOOL_CAPABILITY?.trim() || null;
  if (tool && (!toolGatewayUrl || !toolCapability)) {
    throw new Error(
      "AGENT_LIVE_TOOL_GATEWAY_URL and AGENT_LIVE_TOOL_CAPABILITY are required for the tool scenario",
    );
  }
  const systemPrompt = tool
    ? "You are a Lumen Agent Runtime compatibility probe. Follow the exact user request and call only explicitly registered tools."
    : "You are a Lumen Agent Runtime compatibility probe. Follow the exact user request and do not call tools.";
  const request: RuntimeRequest = {
    version: 1,
    run_id: runId,
    agent_session_id: randomUUID(),
    user_id: randomUUID(),
    execution_epoch: 1,
    user_message_id: randomUUID(),
    assistant_message_id: randomUUID(),
    trace_id: randomUUID().replaceAll("-", ""),
    provider: {
      provider_id: "lumen-live-check",
      api: api as "openai-responses" | "openai-completions" | "anthropic-messages",
      api_key: apiKey,
      base_url: baseUrl,
      headers: {},
      model,
      proxy_url: process.env.AGENT_LIVE_PROXY_URL?.trim() || null,
      resolved_ips: [],
      context_window: Number(process.env.AGENT_LIVE_CONTEXT_WINDOW ?? 272_000),
      max_output_tokens: Number(process.env.AGENT_LIVE_MAX_OUTPUT_TOKENS ?? 4096),
      reasoning_supported: true,
      vision_supported: vision,
    },
    system_prompt: systemPrompt,
    history: [],
    compaction: null,
    current_prompt: process.env.AGENT_LIVE_PROMPT?.trim() || (
      tool
        ? "Call lumen_create_image exactly once with prompt 'Lumen live tool check', then confirm."
        : vision
          ? "Inspect the attached image and reply with exactly: LUMEN_AGENT_VISION_OK"
          : "Reply with exactly: LUMEN_AGENT_RUNTIME_LIVE_OK"
    ),
    allowed_tools: tool ? ["lumen_create_image"] : [],
    tool_gateway_url: toolGatewayUrl,
    tool_capability: toolCapability,
    references,
    image_defaults: {
      count: 1,
      aspect_ratio: "1:1",
      quality: "1k",
      render_quality: "low",
      background: "auto",
      output_format: "webp",
    },
    reasoning_effort: "low",
    limits: {
      max_turns: 2,
      max_tool_calls: tool ? 1 : 0,
      max_image_tool_calls: tool ? 1 : 0,
      max_images_per_run: 1,
      max_output_tokens: Number(process.env.AGENT_LIVE_MAX_OUTPUT_TOKENS ?? 4096),
      run_timeout_seconds: Number(process.env.AGENT_LIVE_TIMEOUT_SECONDS ?? 120),
      tool_timeout_seconds: 30,
      max_output_chars: 64_000,
    },
  };
  const validatedRequest = parseRuntimeRequest(request);
  const writer = new CollectingEventWriter(runId, validatedRequest.execution_epoch);
  const timeout = AbortSignal.timeout(validatedRequest.limits.run_timeout_seconds * 1000);
  const abortController = new AbortController();
  const abortDelay = scenario === "abort"
    ? Number(process.env.AGENT_LIVE_ABORT_AFTER_MS ?? 250)
    : 0;
  const abortTimer = abortDelay > 0
    ? setTimeout(() => abortController.abort(), abortDelay)
    : null;
  let result: ExecuteResult;
  try {
    result = await executeAgentRun(
      validatedRequest,
      writer,
      AbortSignal.any([timeout, abortController.signal]),
    );
  } catch (error) {
    if (!(error instanceof RuntimeExecutionError)) throw error;
    result = error.result;
  } finally {
    if (abortTimer) clearTimeout(abortTimer);
  }
  const textEvents = writer.events.filter((event) => event.type === "text.delta");
  console.log(
    JSON.stringify({
      outcome: result.outcome,
      scenario,
      error_code: result.errorCode,
      turn_count: result.turnCount,
      tool_call_count: result.toolCallCount,
      text_event_count: textEvents.length,
      usage: result.usage,
      provider_statuses: writer.events
        .filter((event) => event.type === "provider.response")
        .map((event) => event.status),
    }),
  );
  const statuses = writer.events
    .filter((event) => event.type === "provider.response")
    .map((event) => event.status);
  const passed =
    scenario === "abort"
      ? result.outcome === "cancelled" || result.errorCode === "agent_cancelled"
      : scenario === "error-429"
        ? statuses.includes(429) && result.outcome !== "succeeded"
        : scenario === "error-5xx"
          ? statuses.some((status) => typeof status === "number" && status >= 500) &&
            result.outcome !== "succeeded"
          : scenario === "truncated"
            ? result.outcome !== "succeeded"
            : scenario === "reasoning"
              ? result.outcome === "succeeded" && result.usage.reasoning_tokens > 0
              : scenario === "tool"
                ? result.outcome === "succeeded" && result.toolCallCount === 1
                : result.outcome === "succeeded" && textEvents.length > 0;
  if (!passed) process.exitCode = 1;
}
