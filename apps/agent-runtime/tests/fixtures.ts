import type { RuntimeRequest } from "../src/contracts.js";

type RuntimeRequestV2 = Extract<RuntimeRequest, { version: 2 }>;
type RuntimeRequestV3 = Extract<RuntimeRequest, { version: 3 }>;
type RuntimeRequestV5 = Extract<RuntimeRequest, { version: 5 }>;

export const TEST_SECRET = "runtime-test-secret-0123456789-abcdef";

export function runtimeRequest(
  overrides: Partial<RuntimeRequestV2> = {},
): RuntimeRequestV2 {
  const base: RuntimeRequestV2 = {
    version: 2,
    run_id: "run-1",
    agent_session_id: "session-1",
    user_id: "user-1",
    execution_epoch: 1,
    user_message_id: "user-message-1",
    assistant_message_id: "message-1",
    trace_id: "0123456789abcdef0123456789abcdef",
    event_features: ["heartbeat-v1", "text-reset-v1"],
    provider: {
      provider_id: "lumen-test",
      api: "openai-responses",
      base_url: "https://provider.example/v1",
      api_key: "provider-test-key",
      headers: {},
      proxy_url: null,
      resolved_ips: [],
      model: "test-model",
      context_window: 128_000,
      max_output_tokens: 4096,
      reasoning_supported: true,
      vision_supported: true,
    },
    system_prompt: "You are Lumen Agent. Use only explicitly registered tools.",
    history: [],
    compaction: null,
    current_prompt: "Create one square image and then confirm it.",
    references: [],
    allowed_tools: ["lumen_create_image"],
    image_defaults: {
      count: 1,
      aspect_ratio: "1:1",
      quality: "2k",
      render_quality: "high",
      background: "auto",
      output_format: "webp",
    },
    tool_gateway_url: "http://api:8000/internal/agent/runs/run-1/tools/create-image",
    tool_capability: "capability-test-token-with-more-than-32-characters",
    reasoning_effort: "low",
    tool_policy: {
      max_image_tool_calls: 2,
      max_images_per_run: 4,
    },
  };
  return { ...base, ...overrides };
}

export function runtimeRequestV5(
  overrides: Partial<RuntimeRequestV5> = {},
): RuntimeRequestV5 {
  return {
    ...runtimeRequest(),
    version: 5,
    operation: "prompt",
    allowed_tools: [],
    workspace_files: [],
    tool_gateway_url: null,
    tool_capability: null,
    tool_policy: {
      max_image_tool_calls: 0,
      max_images_per_run: 4,
      max_web_search_calls: 0,
      max_file_tool_calls: 0,
      max_tool_calls: 0,
    },
    ...overrides,
  };
}

export function runtimeRequestV3(
  overrides: Partial<RuntimeRequestV3> = {},
): RuntimeRequestV3 {
  return {
    ...runtimeRequest(),
    version: 3,
    operation: "prompt",
    ...overrides,
  };
}
