import { toast } from "@/components/ui/primitives";
import {
  imageVariantUrl,
  videoPosterUrl,
} from "@/lib/apiClient";
import type {
  VideoAction,
  VideoGenerationOut,
  VideoOptionsOut,
} from "@/lib/types";
import { uuid } from "@/lib/utils";

import {
  anchorPromptEnhanceCandidates,
  displayPromptEnhanceCandidates,
  referenceLabel,
} from "./video-reference-domain";
import type {
  ReferenceKind,
  ReferenceLimits,
} from "./video-reference-domain";
import {
  cleanPromptEnhanceText,
  shouldAutoApplyPromptEnhanceCandidate,
} from "./video-workbench-ui";
import type {
  PromptEnhanceAction,
  PromptEnhanceCandidate,
  ReferenceDraft,
} from "./video-workbench-ui";
import {
  cleanReferencePreviewUrl,
} from "./video-page-utils";
import {
  preferredDuration,
  preferredResolution,
  videoUnavailableReasonMessage,
} from "./video-options-model";
import { videoEstimateIssue } from "./video-page-derived-state";

export const VIDEO_PROMPT_VARIANT_COUNT = 3;
const VIDEO_PROMPT_VARIANT_TITLES = [
  "推荐镜头版",
  "动作节奏版",
  "参考一致版",
];

export const PROMPT_CHIPS = [
  "近景",
  "推镜",
  "跟拍",
  "侧光",
  "转台",
  "干净背景",
  "浅景深",
  "轻微运动模糊",
];

function promptEnhanceAttribute(attrs: string, name: string): string {
  const pattern = new RegExp(`${name}\\s*=\\s*(?:"([^"]*)"|'([^']*)')`, "i");
  const match = pattern.exec(attrs);
  return cleanPromptEnhanceText(match?.[1] ?? match?.[2] ?? "");
}

function normalizePromptEnhanceAction(value: string): PromptEnhanceAction {
  const action = value.trim().toLowerCase().replace(/[-\s]/g, "_");
  if (
    action === "direct_pass" ||
    action === "light_refine" ||
    action === "direct_rewrite" ||
    action === "ask_first" ||
    action === "keep_original" ||
    action === "optional_vc"
  ) {
    return action;
  }
  return "direct_rewrite";
}

function parsePromptEnhanceCandidates(raw: string): PromptEnhanceCandidate[] {
  const normalized = raw.replace(/\r\n/g, "\n").trim();
  if (!normalized) return [];
  const candidates: PromptEnhanceCandidate[] = [];
  const variantPattern = /<variant\b([^>]*)>([\s\S]*?)<\/variant>/gi;
  for (const match of normalized.matchAll(variantPattern)) {
    const attrs = match[1] ?? "";
    const promptText = cleanPromptEnhanceText(match[2] ?? "");
    if (!promptText) continue;
    const title =
      promptEnhanceAttribute(attrs, "title") ||
      VIDEO_PROMPT_VARIANT_TITLES[candidates.length] ||
      `方案 ${candidates.length + 1}`;
    candidates.push({
      id: `variant-${candidates.length + 1}`,
      title,
      prompt: promptText,
      action: normalizePromptEnhanceAction(
        promptEnhanceAttribute(attrs, "action"),
      ),
    });
  }
  if (candidates.length > 0) {
    return candidates.slice(0, VIDEO_PROMPT_VARIANT_COUNT);
  }
  const fallback = cleanPromptEnhanceText(normalized);
  if (!fallback) return [];
  return [
    {
      id: "variant-1",
      title: "优化结果",
      prompt: fallback,
      action: "direct_rewrite",
    },
  ];
}

export function buildPromptEnhanceCandidates(
  raw: string,
  sourceText: string,
  references: ReferenceDraft[],
): PromptEnhanceCandidate[] {
  return displayPromptEnhanceCandidates(
    anchorPromptEnhanceCandidates(
      parsePromptEnhanceCandidates(raw),
      sourceText,
      references,
    ),
    references,
  );
}

export function applyPromptEnhanceCandidateState(
  candidates: PromptEnhanceCandidate[],
  setPrompt: (value: string) => void,
  setCandidates: (value: PromptEnhanceCandidate[]) => void,
  setSelectedId: (value: string) => void,
): { recommended: PromptEnhanceCandidate; autoApply: boolean } | null {
  const recommended = candidates[0];
  if (!recommended) return null;
  const autoApply = shouldAutoApplyPromptEnhanceCandidate(recommended);
  if (autoApply) setPrompt(recommended.prompt);
  setCandidates(candidates);
  setSelectedId(autoApply ? recommended.id : "");
  return { recommended, autoApply };
}

export function notifyCompletedPromptEnhancement(
  recommended: PromptEnhanceCandidate,
  autoApply: boolean,
  candidateCount: number,
): void {
  if (recommended.action === "ask_first") {
    toast.success("需要补充信息", {
      description: "已保留原描述，请根据补问补齐后再优化。",
    });
    return;
  }
  if (recommended.action === "keep_original") {
    toast.success("已判断为原样保留", {
      description: "这个需求更适合保留原工作流，不自动改写。",
    });
    return;
  }
  if (recommended.action === "optional_vc" && !autoApply) {
    toast.success("已生成可选 VC 版", {
      description: "未自动替换原描述，可手动选择使用。",
    });
    return;
  }
  toast.success(
    candidateCount > 1 ? `已生成 ${candidateCount} 个优化方案` : "提示词已优化",
  );
}

export function interruptedPromptEnhanceDescription(
  description?: string,
): string {
  return description
    ? `${description} 已保留已生成内容，可继续编辑或重试。`
    : "已保留已生成内容，可继续编辑或重试。";
}

export function inputImageForVideoAction(
  action: VideoAction,
  inputImageId: string,
): string | null {
  return action === "i2v" ? inputImageId.trim() || null : null;
}

type VideoHistoryReference = VideoGenerationOut["reference_media"][number];

function historyReferenceDisplay(ref: VideoHistoryReference): string {
  if (ref.url) return ref.url.replace(/^asset:\/\//i, "asset://");
  if (ref.kind === "image") return ref.image_id?.slice(0, 8) ?? "图片";
  if (ref.kind === "video") return ref.video_id?.slice(0, 8) ?? "视频";
  return "音频";
}

function historyReferencePreviewUrl(ref: VideoHistoryReference): string | null {
  if (ref.kind === "image") {
    return (
      cleanReferencePreviewUrl(ref.url) ??
      (ref.image_id ? imageVariantUrl(ref.image_id, "display2048") : null)
    );
  }
  if (ref.kind === "video" && ref.video_id) {
    return videoPosterUrl(ref.video_id);
  }
  return null;
}

export function referenceDraftFromHistory(
  ref: VideoHistoryReference,
  index: number,
  references: VideoHistoryReference[],
): ReferenceDraft {
  const kindIndex = references
    .slice(0, index + 1)
    .filter((current) => current.kind === ref.kind).length;
  const fallbackLabel = referenceLabel(ref.kind, kindIndex);
  return {
    _key: uuid(),
    kind: ref.kind,
    image_id: ref.kind === "image" ? (ref.image_id ?? null) : null,
    video_id: ref.kind === "video" ? (ref.video_id ?? null) : null,
    url: ref.url ?? null,
    label: ref.label || fallbackLabel,
    ref_id: ref.ref_id || `ref:${ref.kind}:${kindIndex}`,
    display: historyReferenceDisplay(ref),
    previewUrl: historyReferencePreviewUrl(ref),
  };
}

function videoConfigurationIssue({
  createPending,
  uploadPending,
  optionsLoading,
  options,
  selectedModel,
  availableResolutions,
  resolution,
  availableDurations,
  durationS,
}: {
  createPending: boolean;
  uploadPending: boolean;
  optionsLoading: boolean;
  options: VideoOptionsOut | undefined;
  selectedModel: string;
  availableResolutions: string[];
  resolution: string;
  availableDurations: number[];
  durationS: number;
}): string | null {
  if (createPending) return "正在提交";
  if (uploadPending) return "等待素材上传完成";
  if (optionsLoading) return "正在读取配置";
  if (!options?.enabled) {
    return videoUnavailableReasonMessage(options?.unavailable_reason);
  }
  if (!selectedModel) return "没有可用模型";
  if (!availableResolutions.includes(resolution)) {
    return "当前模型不支持该分辨率";
  }
  if (!availableDurations.includes(durationS)) {
    return "当前模型不支持该时长";
  }
  return null;
}

function videoInputIssue({
  prompt,
  action,
  inputImageId,
  referenceCounts,
  referenceLimitError,
}: {
  prompt: string;
  action: VideoAction;
  inputImageId: string;
  referenceCounts: ReferenceLimits;
  referenceLimitError: string | null;
}): string | null {
  const referenceCount =
    referenceCounts.image + referenceCounts.video + referenceCounts.audio;
  if (!prompt.trim()) return "先填写描述";
  if (action === "i2v" && !inputImageId.trim()) {
    return "需要上传首帧或填写图片 ID";
  }
  if (action === "reference" && referenceCount === 0) {
    return "先添加参考素材";
  }
  if (
    action === "reference" &&
    referenceCounts.image + referenceCounts.video === 0
  ) {
    return "参考生成至少需要一张图片或一个视频，不能仅使用音频";
  }
  if (action === "reference" && referenceLimitError) {
    return referenceLimitError;
  }
  return null;
}

export function videoSubmitDisabledReason({
  createPending,
  uploadPending,
  optionsLoading,
  options,
  selectedModel,
  availableResolutions,
  resolution,
  availableDurations,
  durationS,
  prompt,
  action,
  inputImageId,
  referenceCounts,
  referenceLimitError,
  seedIsValid,
  estimate,
}: {
  createPending: boolean;
  uploadPending: boolean;
  optionsLoading: boolean;
  options: VideoOptionsOut | undefined;
  selectedModel: string;
  availableResolutions: string[];
  resolution: string;
  availableDurations: number[];
  durationS: number;
  prompt: string;
  action: VideoAction;
  inputImageId: string;
  referenceCounts: ReferenceLimits;
  referenceLimitError: string | null;
  seedIsValid: boolean;
  estimate: { tokens: number; micro: number } | null;
}): string {
  return (
    videoConfigurationIssue({
      createPending,
      uploadPending,
      optionsLoading,
      options,
      selectedModel,
      availableResolutions,
      resolution,
      availableDurations,
      durationS,
    }) ??
    videoInputIssue({
      prompt,
      action,
      inputImageId,
      referenceCounts,
      referenceLimitError,
    }) ??
    videoEstimateIssue(seedIsValid, estimate) ??
    "可以提交"
  );
}

export function selectedVideoModel(
  availableModels: VideoOptionsOut["models"],
  requestedModel: string,
): string {
  return (
    availableModels.find((item) => item.model === requestedModel)?.model ??
    availableModels[0]?.model ??
    ""
  );
}

export function selectedReferenceKind(
  options: ReferenceKind[],
  requested: ReferenceKind,
): ReferenceKind {
  if (options.includes(requested)) return requested;
  return options[0] ?? "image";
}

export function effectiveVideoResolution(
  availableResolutions: string[],
  requested: string,
): string {
  return availableResolutions.includes(requested)
    ? requested
    : preferredResolution(availableResolutions);
}

export function effectiveVideoDuration(
  availableDurations: number[],
  requested: number,
): number {
  return availableDurations.includes(requested)
    ? requested
    : preferredDuration(availableDurations);
}

export function canEnhanceVideoPrompt({
  uploadPending,
  referenceUploadPending,
  prompt,
  action,
  inputImageId,
  referenceCount,
}: {
  uploadPending: boolean;
  referenceUploadPending: boolean;
  prompt: string;
  action: VideoAction;
  inputImageId: string;
  referenceCount: number;
}): boolean {
  if (uploadPending || referenceUploadPending) return false;
  if (prompt.trim()) return true;
  if (action === "i2v") return Boolean(inputImageId.trim());
  return action === "reference" && referenceCount > 0;
}

export function videoServiceSummary({
  loading,
  enabled,
  modelCount,
  unavailableReason,
}: {
  loading: boolean;
  enabled: boolean;
  modelCount: number;
  unavailableReason?: string | null;
}): string {
  if (loading) return "读取视频服务配置";
  if (enabled) return `${modelCount} 个模型可用`;
  return videoUnavailableReasonMessage(unavailableReason);
}

export function videoSourceReady(
  action: VideoAction,
  inputImageId: string,
  referenceCount: number,
): boolean {
  if (action === "t2v") return true;
  if (action === "i2v") return inputImageId.trim().length > 0;
  return referenceCount > 0;
}
