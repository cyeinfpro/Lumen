import {
  InMemoryCredentialStore,
  InMemoryModelsStore,
  createProvider,
  type Model,
  type ProviderStreams,
} from "@earendil-works/pi-ai";
import { anthropicMessagesApi } from "@earendil-works/pi-ai/api/anthropic-messages.lazy";
import { openAICompletionsApi } from "@earendil-works/pi-ai/api/openai-completions.lazy";
import { openAIResponsesApi } from "@earendil-works/pi-ai/api/openai-responses.lazy";
import { ModelRuntime } from "@earendil-works/pi-coding-agent";

import type { RuntimeRequest } from "../contracts.js";
import { createProviderTransport, type ProviderTransport } from "./transport.js";

export interface PreparedProviderRuntime {
  readonly modelRuntime: ModelRuntime;
  readonly model: Model<string>;
  readonly transport: ProviderTransport;
  close(): Promise<void>;
}

function providerApi(api: RuntimeRequest["provider"]["api"]): ProviderStreams {
  switch (api) {
    case "openai-responses":
      return openAIResponsesApi();
    case "openai-completions":
      return openAICompletionsApi();
    case "anthropic-messages":
      return anthropicMessagesApi();
  }
}

export function runtimeModel(request: RuntimeRequest): Model<string> {
  // Keep capability metadata truthful for role and history conversion. Pi must
  // still be able to hold an internal Off state even when the provider's native
  // off mapping is omission/null; payload omission is enforced below.
  const configuredMap = request.provider.reasoning_supported
    ? request.provider.thinking_level_map
    : undefined;
  const thinkingLevelMap = request.provider.reasoning_supported
    ? { ...configuredMap, off: configuredMap?.off ?? "none" }
    : undefined;
  return {
    id: request.provider.model,
    name: request.provider.model,
    api: request.provider.api,
    provider: request.provider.provider_id,
    baseUrl: request.provider.base_url,
    reasoning: request.provider.reasoning_supported,
    ...(thinkingLevelMap ? { thinkingLevelMap } : {}),
    input: request.provider.vision_supported ? ["text", "image"] : ["text"],
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    contextWindow: request.provider.context_window,
    maxTokens: request.provider.max_output_tokens,
  };
}

const AUTO_REASONING_KEYS = new Set([
  "reasoning",
  "reasoning_effort",
  "thinking",
  "enable_thinking",
  "thinking_token_budget",
  "output_config",
]);

export function omitAutomaticReasoningControls(
  request: RuntimeRequest,
  payload: unknown,
): unknown {
  const explicitOffUsesOmission =
    request.reasoning_effort === "off" &&
    request.provider.thinking_level_map?.off === null;
  if (
    request.reasoning_effort !== null &&
    !explicitOffUsesOmission
  ) {
    return payload;
  }
  if (payload === null || typeof payload !== "object" || Array.isArray(payload)) {
    return payload;
  }
  let output = Object.fromEntries(
    Object.entries(payload as Record<string, unknown>).filter(
      ([key]) => !AUTO_REASONING_KEYS.has(key),
    ),
  );
  for (const key of ["chat_template_kwargs", "chat_template_args"] as const) {
    const raw = output[key];
    if (raw === null || typeof raw !== "object" || Array.isArray(raw)) continue;
    const values = Object.fromEntries(
      Object.entries(raw as Record<string, unknown>).filter(
        ([name]) => name !== "enable_thinking",
      ),
    );
    if (Object.keys(values).length > 0) output[key] = values;
    else {
      output = Object.fromEntries(
        Object.entries(output).filter(([name]) => name !== key),
      );
    }
  }
  return output;
}

export async function prepareProviderRuntime(
  request: RuntimeRequest,
  onDispatch: (signal?: AbortSignal) => Promise<void>,
): Promise<PreparedProviderRuntime> {
  const credentials = new InMemoryCredentialStore();
  const modelRuntime = await ModelRuntime.create({
    credentials,
    modelsPath: null,
    modelsStore: new InMemoryModelsStore(),
    refreshOnCreate: false,
    allowModelNetwork: false,
  });
  const model = runtimeModel(request);
  const provider = createProvider({
    id: request.provider.provider_id,
    name: request.provider.provider_id,
    baseUrl: request.provider.base_url,
    headers: request.provider.headers,
    auth: {
      apiKey: {
        name: "Lumen run-scoped provider credential",
        async check() {
          return { type: "api_key", source: "runtime envelope" };
        },
        async resolve() {
          return {
            auth: { apiKey: request.provider.api_key },
            source: "runtime envelope",
          };
        },
      },
    },
    models: [model],
    api: providerApi(request.provider.api),
  });
  modelRuntime.registerNativeProvider(provider);
  const transport = createProviderTransport(
    request.provider.proxy_url,
    request.provider.base_url,
    request.provider.resolved_ips,
    onDispatch,
  );
  return {
    modelRuntime,
    model,
    transport,
    async close(): Promise<void> {
      await transport.close();
    },
  };
}
