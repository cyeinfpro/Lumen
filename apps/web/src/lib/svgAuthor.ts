import type { SvgAuthorOptions, SvgReasoningEffort } from "./types";

export const SVG_REASONING_EFFORTS: SvgReasoningEffort[] = ["none", "minimal", "low", "medium", "high", "xhigh", "max"];

// A blank author disables model selection: omit the entire svg object.
export function normalizeSvgAuthor(value: unknown): SvgAuthorOptions | undefined {
  if (!value || typeof value !== "object") return undefined;
  const candidate = value as SvgAuthorOptions;
  if (typeof candidate.model !== "string" || !candidate.model.trim()) return undefined;
  const effort = candidate.reasoning?.effort;
  return {
    model: candidate.model.trim(),
    ...(effort && SVG_REASONING_EFFORTS.includes(effort) ? { reasoning: { effort } } : {}),
  };
}
