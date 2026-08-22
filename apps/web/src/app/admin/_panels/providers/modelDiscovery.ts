import type {
  ProviderDiscoveredModel,
  ProviderModelProfileSource,
} from "@/lib/types";
import type { Draft } from "./model";

export interface ProviderModelDiscoveryState {
  status: "idle" | "loading" | "ready" | "error";
  models: ProviderDiscoveredModel[];
  selectedModelId: string | null;
  setAsDefault: boolean;
  error: string | null;
}

export function preferredProviderModel(
  models: ProviderDiscoveredModel[],
  currentDefault: string,
): ProviderDiscoveredModel | null {
  const exact = models.find((model) => model.id === currentDefault);
  if (exact) return exact;
  const ranked = [...models].sort(
    (left, right) =>
      modelRank(right.id) - modelRank(left.id) ||
      left.id.localeCompare(right.id),
  );
  return ranked[0] ?? null;
}

export function modelProfilePatch(
  model: ProviderDiscoveredModel,
  allModels: ProviderDiscoveredModel[],
): Partial<Draft> {
  const modelIds = allModels.slice(0, 128).map((item) => item.id);
  if (!modelIds.includes(model.id)) {
    modelIds[Math.max(0, modelIds.length - 1)] = model.id;
  }
  return {
    agent_models: modelIds,
    agent_api: model.profile.agent_api,
    responses_supported: model.profile.responses_supported,
    vision_supported: model.profile.vision_supported,
    agent_context_window: model.profile.context_window,
    agent_max_output_tokens: model.profile.max_output_tokens,
    agent_reasoning_supported: model.profile.reasoning_supported,
  };
}

export function modelProfileSourceLabel(
  source: ProviderModelProfileSource,
): string {
  if (source === "provider") return "供应商元数据";
  if (source === "known_family") return "已知模型族保守档案";
  return "通用保守档案";
}

function modelRank(modelId: string): number {
  const value = modelId.toLowerCase();
  if (value.includes("gpt-5.6")) return 600;
  if (value.includes("gpt-5.5")) return 550;
  if (value.startsWith("gpt-5")) return 500;
  if (value.startsWith("o4")) return 440;
  if (value.startsWith("o3")) return 430;
  if (value.includes("claude")) return 400;
  if (value.includes("gemini")) return 350;
  if (value.startsWith("gpt-4.1")) return 300;
  if (value.startsWith("gpt-4o")) return 250;
  return 0;
}
