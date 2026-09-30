export type PromptEnhancementModel = "gpt-6-astra" | "gpt-6-sol" | "gpt-6-luna";

export type TextReasoningEffort = "auto" | "none" | "minimal" | "low" | "medium" | "high" | "xhigh" | "max";

export function gpt6ModelFamily(modelId: string | null | undefined): "astra" | "sol" | "luna" | null {
  const canonical = (modelId ?? "").trim().toLowerCase().split(/[/:]/u).at(-1) ?? "";
  const match = /^gpt-6-(astra|sol|luna)(?:-\d{4}-\d{2}-\d{2})?$/u.exec(canonical);
  return (match?.[1] as "astra" | "sol" | "luna" | undefined) ?? null;
}

export function textModelLabel(modelId: string | null | undefined): string {
  const family = gpt6ModelFamily(modelId);
  if (!family) return modelId || "默认模型";
  const name = `GPT-6 ${family[0].toUpperCase()}${family.slice(1)}`;
  const snapshot = /-\d{4}-\d{2}-\d{2}$/u.exec(modelId ?? "")?.[0];
  return snapshot ? `${name} · ${snapshot.slice(1)}` : name;
}

export const TEXT_REASONING_OPTIONS: ReadonlyArray<{ value: TextReasoningEffort; label: string }> = [
  { value: "auto", label: "自动" }, { value: "none", label: "关闭" },
  { value: "minimal", label: "极低" }, { value: "low", label: "低" },
  { value: "medium", label: "中" }, { value: "high", label: "高" },
  { value: "xhigh", label: "超高" }, { value: "max", label: "最大" },
];

export function normalizeTextReasoning(modelId: string | null | undefined, effort: TextReasoningEffort): TextReasoningEffort {
  const family = gpt6ModelFamily(modelId);
  if (family && (effort === "minimal" || (family === "astra" && effort === "none"))) return "low";
  return effort;
}

export function textReasoningOptions(modelId: string | null | undefined) {
  const family = gpt6ModelFamily(modelId);
  return TEXT_REASONING_OPTIONS.filter((option) => !family || (
    option.value !== "minimal" && (family !== "astra" || option.value !== "none")
  ));
}

export function textReasoningLabel(modelId: string | null | undefined, effort: TextReasoningEffort): string {
  const effective = normalizeTextReasoning(modelId, effort);
  return TEXT_REASONING_OPTIONS.find((option) => option.value === effective)?.label ?? "自动";
}
