import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";

import { parseRuntimeRequest, type RuntimeRequest } from "../src/contracts.js";
import { CollectingEventWriter } from "../src/ndjson.js";
import {
  executeAgentRun,
  RuntimeExecutionError,
  type ExecuteResult,
} from "../src/runtime.js";

const SUPPORTED_APIS = new Set<RuntimeRequest["provider"]["api"]>([
  "openai-responses",
  "openai-completions",
  "anthropic-messages",
]);
const SUPPORTED_SCENARIOS = new Set([
  "text",
  "vision",
  "tool",
  "abort",
  "error-429",
  "error-5xx",
  "truncated",
  "reasoning",
]);
const OUTPUT_TOKEN_HARD_LIMIT = 4_096;
const TIMEOUT_SECONDS_HARD_LIMIT = 300;
const PROMPT_CHARS_HARD_LIMIT = 4_000;
const REFERENCE_BYTES_HARD_LIMIT = 2_000_000;

function positiveIntegerEnv(name: string, fallback: number): number {
  const raw = process.env[name]?.trim();
  if (!raw) return fallback;
  const parsed = Number(raw);
  if (!Number.isSafeInteger(parsed) || parsed <= 0) {
    throw new Error(`${name} must be a positive integer`);
  }
  return parsed;
}

function nonNegativeIntegerEnv(name: string, fallback: number): number {
  const raw = process.env[name]?.trim();
  if (!raw) return fallback;
  const parsed = Number(raw);
  if (!Number.isSafeInteger(parsed) || parsed < 0) {
    throw new Error(`${name} must be a non-negative integer`);
  }
  return parsed;
}

function requireWithinBudget(value: number, budget: number, name: string): number {
  if (value > budget) {
    throw new Error(`${name}=${String(value)} exceeds configured budget ${String(budget)}`);
  }
  return value;
}

function configuredPrompt(defaultPrompt: string): string {
  const prompt = process.env.AGENT_LIVE_PROMPT?.trim() || defaultPrompt;
  const maxPromptChars = requireWithinBudget(
    positiveIntegerEnv("AGENT_LIVE_PROMPT_MAX_CHARS", 1_000),
    PROMPT_CHARS_HARD_LIMIT,
    "AGENT_LIVE_PROMPT_MAX_CHARS",
  );
  if (prompt.length > maxPromptChars) {
    throw new Error("AGENT_LIVE_PROMPT exceeds AGENT_LIVE_PROMPT_MAX_CHARS");
  }
  return prompt;
}

function liveApi(value: string): RuntimeRequest["provider"]["api"] {
  if (!SUPPORTED_APIS.has(value as RuntimeRequest["provider"]["api"])) {
    throw new Error("AGENT_LIVE_API is unsupported");
  }
  return value as RuntimeRequest["provider"]["api"];
}

if (process.env.AGENT_LIVE_CHECK !== "1") {
  console.error("Refusing a billable provider call: set AGENT_LIVE_CHECK=1 explicitly.");
  process.exitCode = 2;
} else {
  const apiKey = process.env.AGENT_LIVE_API_KEY?.trim();
  const baseUrl = process.env.AGENT_LIVE_BASE_URL?.trim();
  const model = process.env.AGENT_LIVE_MODEL?.trim();
  const api = liveApi(process.env.AGENT_LIVE_API?.trim() ?? "openai-responses");
  const scenario = process.env.AGENT_LIVE_SCENARIO?.trim() || "text";
  if (!SUPPORTED_SCENARIOS.has(scenario)) {
    throw new Error("AGENT_LIVE_SCENARIO is unsupported");
  }
  if (!apiKey || !baseUrl || !model) {
    throw new Error("AGENT_LIVE_API_KEY, AGENT_LIVE_BASE_URL, and AGENT_LIVE_MODEL are required");
  }

  const outputTokenBudget = requireWithinBudget(
    positiveIntegerEnv("AGENT_LIVE_OUTPUT_TOKEN_BUDGET", 512),
    OUTPUT_TOKEN_HARD_LIMIT,
    "AGENT_LIVE_OUTPUT_TOKEN_BUDGET",
  );
  const maxOutputTokens = requireWithinBudget(
    positiveIntegerEnv(
      "AGENT_LIVE_MAX_OUTPUT_TOKENS",
      Math.min(512, outputTokenBudget),
    ),
    outputTokenBudget,
    "AGENT_LIVE_MAX_OUTPUT_TOKENS",
  );
  const timeoutBudgetSeconds = requireWithinBudget(
    positiveIntegerEnv("AGENT_LIVE_TIMEOUT_BUDGET_SECONDS", 120),
    TIMEOUT_SECONDS_HARD_LIMIT,
    "AGENT_LIVE_TIMEOUT_BUDGET_SECONDS",
  );
  const timeoutSeconds = requireWithinBudget(
    positiveIntegerEnv(
      "AGENT_LIVE_TIMEOUT_SECONDS",
      Math.min(120, timeoutBudgetSeconds),
    ),
    timeoutBudgetSeconds,
    "AGENT_LIVE_TIMEOUT_SECONDS",
  );
  const maxReferenceBytes = requireWithinBudget(
    positiveIntegerEnv("AGENT_LIVE_REFERENCE_MAX_BYTES", 1_000_000),
    REFERENCE_BYTES_HARD_LIMIT,
    "AGENT_LIVE_REFERENCE_MAX_BYTES",
  );
  const runId = randomUUID();
  const vision = scenario === "vision";
  const tool = scenario === "tool";
  const referencePath = process.env.AGENT_LIVE_REFERENCE_IMAGE?.trim();
  if (vision && !referencePath) {
    throw new Error("AGENT_LIVE_REFERENCE_IMAGE is required for the vision scenario");
  }
  const referenceBytes = vision && referencePath ? readFileSync(referencePath) : null;
  if (referenceBytes !== null && referenceBytes.byteLength > maxReferenceBytes) {
    throw new Error("AGENT_LIVE_REFERENCE_IMAGE exceeds AGENT_LIVE_REFERENCE_MAX_BYTES");
  }
  const references = vision
    ? [
        {
          reference_label: "ref_1" as const,
          role: "reference",
          display_label: "live compatibility reference",
          mime_type: "image/png" as const,
          data_base64: referenceBytes?.toString("base64") ?? "",
        },
      ]
    : [];
  const toolGatewayUrl = process.env.AGENT_LIVE_TOOL_GATEWAY_URL?.trim() || null;
  const toolCapability = process.env.AGENT_LIVE_TOOL_CAPABILITY?.trim() || null;
  const providerDispatchUrl =
    process.env.AGENT_LIVE_PROVIDER_DISPATCH_URL?.trim() || null;
  const providerDispatchCapability =
    process.env.AGENT_LIVE_PROVIDER_DISPATCH_CAPABILITY?.trim() || null;
  if (Boolean(providerDispatchUrl) !== Boolean(providerDispatchCapability)) {
    throw new Error(
      "AGENT_LIVE_PROVIDER_DISPATCH_URL and " +
        "AGENT_LIVE_PROVIDER_DISPATCH_CAPABILITY must be set together",
    );
  }
  const maxToolCalls = tool
    ? requireWithinBudget(
        positiveIntegerEnv("AGENT_LIVE_MAX_TOOL_CALLS", 1),
        1,
        "AGENT_LIVE_MAX_TOOL_CALLS",
      )
    : 0;
  if (tool && (!toolGatewayUrl || !toolCapability)) {
    throw new Error(
      "AGENT_LIVE_TOOL_GATEWAY_URL and AGENT_LIVE_TOOL_CAPABILITY are required for the tool scenario",
    );
  }
  const systemPrompt = tool
    ? "You are a Lumen Agent Runtime compatibility probe. Follow the exact user request and call only explicitly registered tools."
    : "You are a Lumen Agent Runtime compatibility probe. Follow the exact user request and do not call tools.";
  const prompt = configuredPrompt(
    tool
      ? "Call lumen_create_image exactly once with prompt 'Lumen live tool check', then confirm."
      : vision
        ? "Inspect the attached image and reply with exactly: LUMEN_AGENT_VISION_OK"
        : "Reply with exactly: LUMEN_AGENT_RUNTIME_LIVE_OK",
  );
  const request: RuntimeRequest = {
    version: 3,
    run_id: runId,
    agent_session_id: randomUUID(),
    user_id: randomUUID(),
    execution_epoch: 1,
    user_message_id: randomUUID(),
    assistant_message_id: randomUUID(),
    trace_id: randomUUID().replaceAll("-", ""),
    event_features: ["heartbeat-v1", "text-reset-v1"],
    operation: "prompt",
    tool_receipt_version: 2,
    provider: {
      provider_id: "lumen-live-check",
      api,
      api_key: apiKey,
      base_url: baseUrl,
      headers: {},
      model,
      proxy_url: process.env.AGENT_LIVE_PROXY_URL?.trim() || null,
      resolved_ips: [],
      context_window: positiveIntegerEnv("AGENT_LIVE_CONTEXT_WINDOW", 272_000),
      max_output_tokens: maxOutputTokens,
      reasoning_supported: true,
      vision_supported: vision,
    },
    system_prompt: systemPrompt,
    history: [],
    compaction: null,
    current_prompt: prompt,
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
    tool_policy: {
      max_image_tool_calls: maxToolCalls,
      max_images_per_run: 1,
    },
    ...(providerDispatchUrl && providerDispatchCapability
      ? {
          provider_dispatch_url: providerDispatchUrl,
          provider_dispatch_capability: providerDispatchCapability,
          safety_budget: {
            max_provider_dispatches: requireWithinBudget(
              positiveIntegerEnv("AGENT_LIVE_MAX_PROVIDER_DISPATCHES", 8),
              128,
              "AGENT_LIVE_MAX_PROVIDER_DISPATCHES",
            ),
          },
        }
      : {}),
  };
  const validatedRequest = parseRuntimeRequest(request);
  const writer = new CollectingEventWriter(runId, validatedRequest.execution_epoch);
  const timeout = AbortSignal.timeout(timeoutSeconds * 1000);
  const abortController = new AbortController();
  const abortDelay = scenario === "abort"
    ? nonNegativeIntegerEnv("AGENT_LIVE_ABORT_AFTER_MS", 250)
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
      budget: {
        max_output_tokens: maxOutputTokens,
        output_token_budget: outputTokenBudget,
        timeout_seconds: timeoutSeconds,
        timeout_budget_seconds: timeoutBudgetSeconds,
        max_tool_calls: maxToolCalls,
      },
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
            ? result.outcome === "partial" &&
              result.errorCode === "agent_output_truncated" &&
              writer.events.some(
                (event) =>
                  event.type === "turn.completed" && event.stop_reason === "length",
              )
            : scenario === "reasoning"
              ? result.outcome === "succeeded" && result.usage.reasoning_tokens > 0
              : scenario === "tool"
                ? result.outcome === "succeeded" && result.toolCallCount === maxToolCalls
                : result.outcome === "succeeded" && textEvents.length > 0;
  if (!passed) process.exitCode = 1;
}
