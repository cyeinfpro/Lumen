import type {
  VideoGenerationOut,
} from "@/lib/types";
import type {
  VideoHistoryFilter,
} from "./video-task-model";
import type {
  PromptEnhanceCandidate,
} from "./video-workbench-ui";

export function videoEstimateIssue(
  seedIsValid: boolean,
  estimate: { tokens: number; micro: number } | null,
): string | null {
  if (!seedIsValid) return "Seed 需为 -1 到 4294967295 的整数";
  if (estimate === null) return "缺少预扣估算";
  return null;
}

export function filteredVideoHistoryItems(
  historyFilter: VideoHistoryFilter,
  settledItems: VideoGenerationOut[],
  succeededItems: VideoGenerationOut[],
  failedItems: VideoGenerationOut[],
): VideoGenerationOut[] {
  if (historyFilter === "succeeded") return succeededItems;
  if (historyFilter === "failed") return failedItems;
  return settledItems;
}

export function hasPromptEnhancementPanel(
  isEnhancing: boolean,
  preview: string,
  candidates: PromptEnhanceCandidate[],
): boolean {
  return isEnhancing || Boolean(preview.trim()) || candidates.length > 0;
}
