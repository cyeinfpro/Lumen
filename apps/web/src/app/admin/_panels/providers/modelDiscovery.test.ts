import { deepStrictEqual, equal } from "node:assert/strict";
import test from "node:test";
import type { ProviderDiscoveredModel } from "@/lib/types";
import {
  modelProfilePatch,
  modelProfileSourceLabel,
  preferredProviderModel,
} from "./modelDiscovery.ts";

const models: ProviderDiscoveredModel[] = [
  {
    id: "gpt-4.1",
    profile: {
      agent_api: "openai-responses",
      responses_supported: true,
      vision_supported: true,
      context_window: 128_000,
      max_output_tokens: 16_384,
      reasoning_supported: false,
      source: "known_family",
    },
  },
  {
    id: "gpt-5.6-sol",
    profile: {
      agent_api: "openai-responses",
      responses_supported: true,
      vision_supported: true,
      context_window: 256_000,
      max_output_tokens: 32_000,
      reasoning_supported: true,
      source: "provider",
    },
  },
];

test("model discovery preserves the current default or prefers GPT-5.6", () => {
  equal(preferredProviderModel(models, "gpt-4.1")?.id, "gpt-4.1");
  equal(preferredProviderModel(models, "missing")?.id, "gpt-5.6-sol");
});

test("selected model profile fills the Agent provider fields and model allowlist", () => {
  deepStrictEqual(modelProfilePatch(models[1], models), {
    agent_models: ["gpt-4.1", "gpt-5.6-sol"],
    agent_api: "openai-responses",
    responses_supported: true,
    vision_supported: true,
    agent_context_window: 256_000,
    agent_max_output_tokens: 32_000,
    agent_reasoning_supported: true,
  });
  equal(modelProfileSourceLabel("provider"), "供应商元数据");
});
