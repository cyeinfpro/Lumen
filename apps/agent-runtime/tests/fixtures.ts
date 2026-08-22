import type { RuntimeRequest } from "../src/contracts.js";

export const TEST_SECRET = "runtime-test-secret-0123456789-abcdef";

export function runtimeRequest(
  overrides: Partial<RuntimeRequest> = {},
): RuntimeRequest {
  const base: RuntimeRequest = {
    version: 1,
    run_id: "run-1",
    agent_session_id: "session-1",
    user_id: "user-1",
    execution_epoch: 1,
    assistant_message_id: "message-1",
    trace_id: "0123456789abcdef0123456789abcdef",
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
    limits: {
      max_turns: 6,
      max_tool_calls: 3,
      max_image_tool_calls: 2,
      max_images_per_run: 4,
      max_output_tokens: 4096,
      run_timeout_seconds: 30,
      tool_timeout_seconds: 10,
      max_output_chars: 64_000,
    },
  };
  return { ...base, ...overrides };
}
