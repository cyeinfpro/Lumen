import type { AgentImageDefaults } from "../model/contracts";

export function mergeAgentImageDefaults(
  current: AgentImageDefaults,
  patch: Partial<AgentImageDefaults>,
): AgentImageDefaults {
  const normalized = { ...patch };
  if (
    patch.background === "transparent" &&
    current.output_format === "jpeg"
  ) {
    normalized.output_format = "png";
  }
  if (
    patch.output_format === "jpeg" &&
    current.background === "transparent"
  ) {
    normalized.output_format = "png";
  }
  return { ...current, ...normalized };
}
