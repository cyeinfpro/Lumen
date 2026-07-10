"use client";

/* eslint-disable @next/next/no-img-element -- Video posters are authenticated API media URLs. */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  AlertCircle,
  Clapperboard,
  ChevronDown,
  CircleCheck,
  Copy,
  Download,
  Film,
  ImageIcon,
  ListVideo,
  Maximize2,
  PencilLine,
  Play,
  RefreshCw,
  RotateCw,
  Send,
  Settings2,
  Sparkles,
  Tags,
  Trash2,
  Upload,
  Video as VideoIcon,
  X,
  XCircle,
} from "lucide-react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";

import {
  cancelVideoGeneration,
  createVideoGeneration,
  deleteVideo,
  enhanceVideoPrompt,
  getVideoGeneration,
  getVideoOptions,
  imageVariantUrl,
  listVideoGenerations,
  retryVideoGeneration,
  uploadImage,
  uploadVideo,
  videoBinaryUrl,
  videoDownloadUrl,
  videoPosterUrl,
} from "@/lib/apiClient";
import { prewarmImage, prewarmVideoMetadata } from "@/lib/imagePreload";
import { useSSE } from "@/lib/useSSE";
import {
  isTerminalVideoEvent,
  mergeVideoGenerationEvent,
  mergeVideoGenerationLists as mergeById,
  videoGenerationEventId,
} from "@/lib/videoEventSnapshot";
import type {
  VideoAction,
  VideoCreateIn,
  VideoGenerationOut,
  VideoOptionsOut,
  VideoReferenceMediaIn,
} from "@/lib/types";
import { Button, IconButton, toast } from "@/components/ui/primitives";
import { DesktopTopNav, MobileTabBar } from "@/components/ui/shell";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import { formatRmb } from "@/lib/money";
import { DURATION, EASE } from "@/lib/motion";
import { cn, uuid } from "@/lib/utils";

type VideoGenerationWithVideo = VideoGenerationOut & {
  video: NonNullable<VideoGenerationOut["video"]>;
};

type ReferenceDraft = VideoReferenceMediaIn & {
  _key: string;
  label: string;
  ref_id: string;
  display: string;
  previewUrl?: string | null;
};

type ReferenceKind = VideoReferenceMediaIn["kind"];
type ReferenceLimits = Record<ReferenceKind, number>;

type PromptEnhanceAction =
  | "direct_pass"
  | "light_refine"
  | "direct_rewrite"
  | "ask_first"
  | "keep_original"
  | "optional_vc";

type PromptEnhanceCandidate = {
  id: string;
  title: string;
  prompt: string;
  action: PromptEnhanceAction;
};

const VIDEO_EVENTS = [
  "video.queued",
  "video.submitted",
  "video.progress",
  "video.fetching",
  "video.succeeded",
  "video.failed",
  "video.canceled",
];
const SMART_VIDEO_DURATION = -1;
const SMART_VIDEO_HOLD_DURATION = 15;
const VIDEO_DURATION_OPTIONS = [
  SMART_VIDEO_DURATION,
  ...Array.from({ length: 13 }, (_, index) => index + 3),
];
const VIDEO_RESOLUTION_VALUES = new Set<VideoCreateIn["resolution"]>([
  "480p",
  "720p",
  "1080p",
  "4k",
]);
const ACTIVE_VIDEO_STATUSES = [
  "queued",
  "submitting",
  "submit_unknown",
  "submitted",
  "running",
] as const;
const TERMINAL_VIDEO_STATUSES = ["succeeded", "failed", "canceled", "expired"] as const;
const SETTLING_VIDEO_STAGES = ["fetching"] as const;
const VIDEO_ACTIVE_POLL_MS = 2500;
const VIDEO_REFRESH_MIN_INTERVAL_MS = 900;
const VIDEO_REFRESH_RETRY_BASE_MS = 1500;
const VIDEO_REFRESH_RETRY_MAX_MS = 15000;
const VIDEO_PROMPT_VARIANT_COUNT = 3;
const VIDEO_HISTORY_PAGE_SIZE = 12;
const VIDEO_SEED_MIN = -1;
const VIDEO_SEED_MAX = 4_294_967_295;
const VIDEO_DRAWER_FOCUSABLE =
  'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),summary,[tabindex]:not([tabindex="-1"])';
const VIDEO_PROMPT_VARIANT_TITLES = [
  "推荐镜头版",
  "动作节奏版",
  "参考一致版",
];
const REFERENCE_REF_ID_RE = /^ref:(image|video|audio):([1-9][0-9]{0,2})$/;
const REFERENCE_KINDS: ReferenceKind[] = ["image", "video", "audio"];
const DEFAULT_REFERENCE_LIMITS: ReferenceLimits = { image: 9, video: 3, audio: 1 };
const NEWAPI_REFERENCE_LIMITS: ReferenceLimits = { image: 4, video: 3, audio: 1 };
const CHINESE_DIGITS: Record<number, string> = {
  1: "一",
  2: "二",
  3: "三",
  4: "四",
  5: "五",
  6: "六",
  7: "七",
  8: "八",
  9: "九",
};

type VideoHistoryFilter = "all" | "succeeded" | "failed";

const MODE_COPY: Record<
  VideoAction,
  {
    title: string;
    eyebrow: string;
    description: string;
    requirement: string;
  }
> = {
  t2v: {
    title: "文字生成",
    eyebrow: "无参考素材",
    description: "只根据描述生成视频。",
    requirement: "填写描述",
  },
  i2v: {
    title: "首帧生成",
    eyebrow: "从图片开始",
    description: "用一张图片确定第一帧和构图。",
    requirement: "上传首帧",
  },
  reference: {
    title: "参考生成",
    eyebrow: "参考图片/视频",
    description: "用素材约束人物、物体或风格。",
    requirement: "添加素材",
  },
};

const PROMPT_CHIPS = [
  "近景",
  "推镜",
  "跟拍",
  "侧光",
  "转台",
  "干净背景",
  "浅景深",
  "轻微运动模糊",
];

const STAGE_COPY: Record<
  string,
  {
    label: string;
    detail: string;
  }
> = {
  queued: {
    label: "排队中",
    detail: "等待开始。",
  },
  submitting: {
    label: "提交中",
    detail: "正在提交。",
  },
  submitted: {
    label: "已提交",
    detail: "等待处理。",
  },
  rendering: {
    label: "生成中",
    detail: "正在生成。",
  },
  running: {
    label: "生成中",
    detail: "正在生成。",
  },
  fetching: {
    label: "取回结果",
    detail: "正在取回文件。",
  },
  finished: {
    label: "已完成",
    detail: "已保存。",
  },
  succeeded: {
    label: "已完成",
    detail: "已保存。",
  },
  failed: {
    label: "失败",
    detail: "失败，可重试。",
  },
  canceled: {
    label: "已取消",
    detail: "已取消。",
  },
  expired: {
    label: "已过期",
    detail: "已过期。",
  },
};

function holdEstimateDurationS(durationS: number): number {
  return durationS === SMART_VIDEO_DURATION ? SMART_VIDEO_HOLD_DURATION : durationS;
}

function formatDurationLabel(durationS: number): string {
  return durationS === SMART_VIDEO_DURATION ? "自动时长" : `${durationS}s`;
}

function formatTaskElapsed(ms?: number | null): string | null {
  if (typeof ms !== "number" || !Number.isFinite(ms) || ms < 0) return null;
  const totalSeconds = Math.max(0, Math.round(ms / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m ${seconds}s`;
  return `${seconds}s`;
}

function taskElapsedLabel(item: VideoGenerationOut): string | null {
  const elapsed = formatTaskElapsed(item.elapsed_ms);
  if (!elapsed) return null;
  return `${isTerminalVideo(item) ? "耗时" : "已耗时"} ${elapsed}`;
}

function isActiveVideo(item: VideoGenerationOut): boolean {
  if (ACTIVE_VIDEO_STATUSES.includes(
    item.status as (typeof ACTIVE_VIDEO_STATUSES)[number],
  )) {
    return true;
  }
  return SETTLING_VIDEO_STAGES.includes(
    item.progress_stage as (typeof SETTLING_VIDEO_STAGES)[number],
  );
}

function isTerminalVideo(item: VideoGenerationOut): boolean {
  return TERMINAL_VIDEO_STATUSES.includes(
    item.status as (typeof TERMINAL_VIDEO_STATUSES)[number],
  );
}

function isFailedHistoryVideo(item: VideoGenerationOut): boolean {
  return ["failed", "canceled", "expired"].includes(item.status);
}

function videoHistoryFilterLabel(filter: VideoHistoryFilter): string {
  if (filter === "succeeded") return "成功";
  if (filter === "failed") return "失败";
  return "全部";
}

function nestedVideoErrorText(value: unknown, depth = 0): string | null {
  if (depth > 4 || value == null) return null;
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (!trimmed) return null;
    if (/^[{["]/.test(trimmed)) {
      try {
        const parsed: unknown = JSON.parse(trimmed);
        const nested = nestedVideoErrorText(parsed, depth + 1);
        if (nested) return nested;
      } catch {
        // Keep the original upstream text when it is not valid JSON.
      }
    }
    return trimmed;
  }
  if (Array.isArray(value)) {
    for (const item of value) {
      const nested = nestedVideoErrorText(item, depth + 1);
      if (nested) return nested;
    }
    return null;
  }
  if (typeof value === "object") {
    const record = value as Record<string, unknown>;
    for (const key of ["message", "detail", "error_description", "error"]) {
      const nested = nestedVideoErrorText(record[key], depth + 1);
      if (nested) return nested;
    }
  }
  return null;
}

function taskErrorSummary(raw: string): string {
  const extracted = nestedVideoErrorText(raw) ?? raw;
  if (/specified asset is not an image/i.test(extracted)) {
    return "参考素材不是有效图片，请检查素材类型或重新上传后再试。";
  }
  const normalized = extracted
    .replace(/\\n/g, " ")
    .replace(/\s*Request id:\s*[A-Za-z0-9_-]+/gi, "")
    .replace(/\s+/g, " ")
    .trim();
  if (normalized.length <= 180) return normalized;
  return `${normalized.slice(0, 177)}...`;
}

function actionLabel(action: VideoAction): string {
  return MODE_COPY[action]?.title ?? action.toUpperCase();
}

function stageCopy(item: VideoGenerationOut): { label: string; detail: string } {
  return (
    STAGE_COPY[item.progress_stage] ??
    STAGE_COPY[item.status] ?? {
      label: item.status,
      detail: item.progress_stage,
    }
  );
}

function progressForItem(item: VideoGenerationOut): number {
  if (item.status === "succeeded") return 100;
  if (["failed", "canceled", "expired"].includes(item.status)) {
    return Math.max(0, Math.min(100, item.progress_pct || 0));
  }
  return Math.max(4, Math.min(98, item.progress_pct || 0));
}

function toVideoResolution(value: string): VideoCreateIn["resolution"] {
  return VIDEO_RESOLUTION_VALUES.has(value as VideoCreateIn["resolution"])
    ? (value as VideoCreateIn["resolution"])
    : "720p";
}

function parseSeed(value: string): number | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  const parsed = Number(trimmed);
  return Number.isSafeInteger(parsed) &&
    parsed >= VIDEO_SEED_MIN &&
    parsed <= VIDEO_SEED_MAX
    ? parsed
    : null;
}

function firstModelForAction(options: VideoOptionsOut | undefined, action: VideoAction): string {
  return options?.models.find((item) => item.actions.includes(action))?.model ?? "";
}

function resolutionOptionsForModel(
  options: VideoOptionsOut | undefined,
  model: string,
): string[] {
  const modelOptions = options?.models.find((item) => item.model === model);
  if (modelOptions?.resolutions?.length) return modelOptions.resolutions;
  return options?.resolutions?.length ? options.resolutions : ["480p", "720p", "1080p"];
}

function firstAvailableDurationOptions(
  candidates: Array<number[] | undefined>,
): number[] {
  for (const candidate of candidates) {
    if (candidate?.length) return candidate;
  }
  return VIDEO_DURATION_OPTIONS;
}

function durationOptionsForModel(
  options: VideoOptionsOut | undefined,
  model: string,
  action: VideoAction,
  resolution: string,
): number[] {
  const modelOptions = options?.models.find((item) => item.model === model);
  const actionResolutionDurations =
    modelOptions?.durations_by_action_resolution?.[action]?.[resolution];
  const actionDurations = modelOptions?.durations_by_action?.[action];
  return firstAvailableDurationOptions([
    actionResolutionDurations,
    actionDurations,
    modelOptions?.durations_s,
    options?.durations_s,
  ]);
}

function billingModelForAction(
  options: VideoOptionsOut | undefined,
  model: string,
  action: VideoAction,
): string {
  const modelOptions = options?.models.find((item) => item.model === model);
  const actionBillingModel = modelOptions?.billing_models?.[action]?.trim();
  if (actionBillingModel) return actionBillingModel;
  const billingModel = modelOptions?.billing_model?.trim();
  return billingModel || model;
}

function preferredResolution(options: string[]): string {
  return options.includes("720p") ? "720p" : options[0] ?? "720p";
}

function preferredDuration(options: number[]): number {
  return options.includes(5) ? 5 : options[0] ?? 5;
}

function durationOrPreferred(current: number, options: number[]): number {
  return options.includes(current) ? current : preferredDuration(options);
}

type VideoPricingAction = VideoOptionsOut["pricing"][number]["action"];

function estimateActionsFor(
  action: VideoAction,
  referenceHasVideo: boolean,
): string[] {
  if (action !== "reference") return [action];
  return referenceHasVideo
    ? ["reference_video"]
    : ["reference_image", "reference", "i2v", "t2v"];
}

function pricingActionsFor(
  action: VideoAction,
  referenceHasVideo: boolean,
): VideoPricingAction[] {
  if (action !== "reference") return [action];
  return referenceHasVideo
    ? ["reference_video", "reference"]
    : ["reference_image", "reference", "i2v"];
}

function findHoldEstimateTokens(
  options: VideoOptionsOut | undefined,
  modelCandidates: string[],
  estimateActions: string[],
  estimateKey: string,
): unknown {
  for (const modelCandidate of modelCandidates) {
    const tokenMap = options?.hold_estimates?.[modelCandidate];
    if (!tokenMap || typeof tokenMap !== "object") continue;
    const tokenRecord = tokenMap as Record<string, unknown>;
    for (const estimateAction of estimateActions) {
      const actionMap = tokenRecord[estimateAction];
      if (!actionMap || typeof actionMap !== "object") continue;
      const tokens = (actionMap as Record<string, unknown>)[estimateKey];
      if (tokens != null) return tokens;
    }
  }
  return undefined;
}

function findVideoPrice(
  options: VideoOptionsOut | undefined,
  modelCandidates: string[],
  priceActions: VideoPricingAction[],
  resolution: string,
): VideoOptionsOut["pricing"][number] | undefined {
  for (const priceAction of priceActions) {
    for (const modelCandidate of modelCandidates) {
      const exact = options?.pricing.find(
        (item) =>
          item.model === modelCandidate &&
          item.action === priceAction &&
          item.resolution === resolution &&
          item.enabled,
      );
      if (exact) return exact;
      const generic = options?.pricing.find(
        (item) =>
          item.model === modelCandidate &&
          item.action === priceAction &&
          !item.resolution &&
          item.enabled,
      );
      if (generic) return generic;
    }
  }
  return undefined;
}

function estimateHoldMicro(
  options: VideoOptionsOut | undefined,
  {
    model,
    billingModel,
    action,
    resolution,
    durationS,
    referenceHasVideo,
  }: {
    model: string;
    billingModel?: string;
    action: VideoAction;
    resolution: string;
    durationS: number;
    referenceHasVideo?: boolean;
  },
): { tokens: number; micro: number } | null {
  const modelCandidates = Array.from(
    new Set([billingModel, model].filter(Boolean) as string[]),
  );
  const estimateKey = `${resolution}:${holdEstimateDurationS(durationS)}`;
  const tokensRaw = findHoldEstimateTokens(
    options,
    modelCandidates,
    estimateActionsFor(action, Boolean(referenceHasVideo)),
    estimateKey,
  );
  const tokens = Number(tokensRaw);
  if (!Number.isFinite(tokens) || tokens <= 0) return null;
  const price = findVideoPrice(
    options,
    modelCandidates,
    pricingActionsFor(action, Boolean(referenceHasVideo)),
    resolution,
  );
  if (!price) return null;
  return { tokens, micro: Math.round((tokens * price.price.micro) / 1_000_000) };
}

function videoSrc(video: VideoGenerationWithVideo["video"]): string {
  return video.url?.trim() || videoBinaryUrl(video.id);
}

function posterSrc(video: VideoGenerationWithVideo["video"]): string | undefined {
  return video.poster_url?.trim() || undefined;
}

function cleanReferencePreviewUrl(value: string | null | undefined): string | null {
  const clean = value?.trim();
  if (!clean || /^asset:\/\//i.test(clean)) return null;
  return clean;
}

function imageReferencePreviewUrl(image: {
  id: string;
  thumb_url?: string | null;
  preview_url?: string | null;
  display_url?: string | null;
  url?: string | null;
}): string {
  return (
    cleanReferencePreviewUrl(image.preview_url) ??
    cleanReferencePreviewUrl(image.display_url) ??
    cleanReferencePreviewUrl(image.thumb_url) ??
    cleanReferencePreviewUrl(image.url) ??
    imageVariantUrl(image.id, "display2048")
  );
}

function prewarmVideoItem(item: VideoGenerationWithVideo | null | undefined): void {
  if (!item) return;
  prewarmImage(posterSrc(item.video));
  prewarmVideoMetadata(videoSrc(item.video));
}

function hasVideo(item: VideoGenerationOut): item is VideoGenerationWithVideo {
  return item.video != null;
}

function activeTemporaryDownload(item: VideoGenerationOut) {
  const download = item.temporary_download;
  const url = download?.url?.trim();
  if (!download || !url) return null;
  const expiresAtMs = Date.parse(download.expires_at);
  if (!Number.isFinite(expiresAtMs) || download.expires_in_s <= 30) {
    return null;
  }
  return { ...download, url };
}

function videoDownloadName(item: VideoGenerationOut): string {
  const ext = hasVideo(item) && item.video.mime === "video/quicktime" ? "mov" : "mp4";
  return `lumen-video-${item.id.slice(0, 8)}.${ext}`;
}

function motionSafeScrollBehavior(): ScrollBehavior {
  if (
    typeof window !== "undefined" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  ) {
    return "auto";
  }
  return "smooth";
}

function cleanPromptEnhanceText(value: string): string {
  return value
    .replace(/\r\n/g, "\n")
    .replace(/^```(?:json|text)?\s*/i, "")
    .replace(/\s*```$/i, "")
    .replace(/^(?:提示词|prompt)\s*[:：]\s*/i, "")
    .trim()
    .replace(/^["“]|["”]$/g, "")
    .trim();
}

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

function shouldAutoApplyPromptEnhanceCandidate(
  candidate: PromptEnhanceCandidate,
): boolean {
  return !(
    candidate.action === "ask_first" ||
    candidate.action === "keep_original" ||
    candidate.action === "optional_vc"
  );
}

function canApplyPromptEnhanceCandidate(candidate: PromptEnhanceCandidate): boolean {
  return candidate.action !== "ask_first" && candidate.action !== "keep_original";
}

function promptEnhanceCandidateButtonText(
  candidate: PromptEnhanceCandidate,
  selected: boolean,
): string {
  if (!canApplyPromptEnhanceCandidate(candidate)) return "仅查看";
  if (selected) return "已应用";
  return "应用此版本";
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
      action: normalizePromptEnhanceAction(promptEnhanceAttribute(attrs, "action")),
    });
  }
  if (candidates.length > 0) return candidates.slice(0, VIDEO_PROMPT_VARIANT_COUNT);
  const fallback = cleanPromptEnhanceText(normalized);
  return fallback
    ? [
        {
          id: "variant-1",
          title: "优化结果",
          prompt: fallback,
          action: "direct_rewrite",
        },
      ]
    : [];
}

function normalizeAssetUrl(value: string): string {
  const raw = value.trim().replace(/^["'`“”‘’]+|["'`“”‘’]+$/g, "").trim();
  if (!raw) return "";
  const stripped = raw.replace(/^asset\s*:\s*\/\s*\//i, "").replace(/^[/\\]+/, "").trim();
  const assetId = stripped.toLowerCase();
  return /^asset-[a-z0-9][a-z0-9_-]*$/.test(assetId) ? `asset://${assetId}` : "";
}

function isNewApiVideoModel(model: string): boolean {
  const value = model.trim().toLowerCase().replace(/[_.]/g, "-");
  return value === "video-ds-2-0" || value.startsWith("video-ds-2-0-");
}

function referenceLimitsForModel(model: string): ReferenceLimits {
  return isNewApiVideoModel(model) ? NEWAPI_REFERENCE_LIMITS : DEFAULT_REFERENCE_LIMITS;
}

function referenceRefId(kind: ReferenceKind, index: number): string {
  return `ref:${kind}:${index}`;
}

function referenceRefIndex(
  refId: string | null | undefined,
  kind: ReferenceKind,
): number | null {
  const match = (refId ?? "").trim().toLowerCase().match(REFERENCE_REF_ID_RE);
  if (!match || match[1] !== kind) return null;
  const index = Number(match[2]);
  return Number.isInteger(index) && index > 0 ? index : null;
}

function referenceKindNoun(kind: ReferenceKind): string {
  if (kind === "image") return "图片";
  if (kind === "audio") return "音频";
  return "视频";
}

function referenceKindShortNoun(kind: ReferenceKind): string {
  if (kind === "image") return "图";
  return referenceKindNoun(kind);
}

function referenceLabel(kind: ReferenceKind, index: number): string {
  return `${referenceKindNoun(kind)} ${index}`;
}

function referenceLimitMessage(kind: ReferenceKind, limit: number): string {
  const unit = kind === "image" ? "张" : "个";
  return `参考${referenceKindNoun(kind)}最多 ${limit} ${unit}`;
}

function referenceCountsFor(refs: ReferenceDraft[]): ReferenceLimits {
  return {
    image: refs.filter((item) => item.kind === "image").length,
    video: refs.filter((item) => item.kind === "video").length,
    audio: refs.filter((item) => item.kind === "audio").length,
  };
}

function referenceLimitViolation(
  refs: ReferenceDraft[],
  limits: ReferenceLimits,
): string | null {
  const counts = referenceCountsFor(refs);
  for (const kind of REFERENCE_KINDS) {
    if (counts[kind] > limits[kind]) {
      return referenceLimitMessage(kind, limits[kind]);
    }
  }
  return null;
}

function nextReferenceIdentity(
  kind: ReferenceKind,
  refs: ReferenceDraft[],
): { refId: string; label: string } {
  const maxIndex = refs.reduce((max, item) => {
    if (item.kind !== kind) return max;
    return Math.max(max, referenceRefIndex(item.ref_id, kind) ?? 0);
  }, 0);
  const index = maxIndex + 1;
  return { refId: referenceRefId(kind, index), label: referenceLabel(kind, index) };
}

function referencePromptToken(
  item: Pick<VideoReferenceMediaIn, "kind" | "ref_id">,
  fallbackIndex = 1,
): string {
  const rawRefId = item.ref_id?.trim().toLowerCase() ?? "";
  const index = referenceRefIndex(rawRefId, item.kind);
  return `[${index ? rawRefId : referenceRefId(item.kind, fallbackIndex)}]`;
}

function referenceDisplayToken(
  item: Pick<VideoReferenceMediaIn, "kind" | "ref_id">,
  fallbackIndex = 1,
): string {
  const rawRefId = item.ref_id?.trim().toLowerCase() ?? "";
  const index = referenceRefIndex(rawRefId, item.kind) ?? fallbackIndex;
  return `@${referenceKindNoun(item.kind)}${index}`;
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function referenceDisplayAliases(item: ReferenceDraft): string[] {
  const index = referenceRefIndex(item.ref_id, item.kind);
  if (!index) return [];
  const noun = referenceKindNoun(item.kind);
  const shortNoun = referenceKindShortNoun(item.kind);
  return [
    referenceDisplayToken(item),
    `@${noun} ${index}`,
    `@${shortNoun}${index}`,
    `@${shortNoun} ${index}`,
  ];
}

function referenceRoleAliases(kind: ReferenceKind, index: number): string[] {
  if (kind === "video") {
    return [
      `视频素材 ${index}`,
      `视频素材${index}`,
      `参考视频 ${index}`,
      `参考视频${index}`,
      `动作参考 ${index}`,
      `动作参考${index}`,
      `运动参考 ${index}`,
      `运动参考${index}`,
    ];
  }
  if (kind === "audio") {
    return [
      `音频素材 ${index}`,
      `音频素材${index}`,
      `参考音频 ${index}`,
      `参考音频${index}`,
    ];
  }
  return [];
}

function numberedReferenceAliases(
  kind: ReferenceKind,
  index: number,
  noun: string,
  shortNoun: string,
): string[] {
  if (kind === "image") {
    return [`第${index}张${noun}`, `第${index}张${shortNoun}`];
  }
  if (kind === "video") {
    return [
      `第${index}个${noun}`,
      `第${index}段${noun}`,
      `第${index}段素材`,
      `第${index}个视频素材`,
    ];
  }
  return [
    `第${index}个${noun}`,
    `第${index}段${noun}`,
    `第${index}段音频素材`,
    `第${index}个音频素材`,
  ];
}

function chineseNumberedReferenceAliases(
  kind: ReferenceKind,
  zh: string | undefined,
  noun: string,
  shortNoun: string,
): string[] {
  if (!zh) return [];
  if (kind === "image") {
    return [`第${zh}张${noun}`, `第${zh}张${shortNoun}`];
  }
  if (kind === "video") {
    return [
      `第${zh}个${noun}`,
      `第${zh}段${noun}`,
      `第${zh}段素材`,
      `第${zh}个视频素材`,
    ];
  }
  return [
    `第${zh}个${noun}`,
    `第${zh}段${noun}`,
    `第${zh}段音频素材`,
    `第${zh}个音频素材`,
  ];
}

function referenceMentionAliases(item: ReferenceDraft): string[] {
  const index = referenceRefIndex(item.ref_id, item.kind);
  if (!index) return [];
  const aliases = new Set<string>();
  const noun = referenceKindNoun(item.kind);
  const shortNoun = referenceKindShortNoun(item.kind);
  const zh = CHINESE_DIGITS[index];
  for (const alias of [
    item.label,
    `[${item.label}]`,
    `${noun} ${index}`,
    `${noun}${index}`,
    `${shortNoun}${index}`,
    ...referenceRoleAliases(item.kind, index),
    ...numberedReferenceAliases(item.kind, index, noun, shortNoun),
    ...chineseNumberedReferenceAliases(item.kind, zh, noun, shortNoun),
  ]) {
    const clean = alias.trim();
    if (clean) aliases.add(clean);
  }
  return Array.from(aliases);
}

function replaceReferenceDisplayMentionsWithAnchors(
  text: string,
  refs: ReferenceDraft[],
): string {
  let next = text;
  for (const item of refs) {
    const token = referencePromptToken(item);
    for (const alias of referenceDisplayAliases(item)) {
      next = next.replace(new RegExp(escapeRegExp(alias), "g"), token);
    }
  }
  return next;
}

function normalizePromptReferenceMentions(
  text: string,
  refs: ReferenceDraft[],
): string {
  if (!text.trim() || refs.length === 0) return text;
  let next = text;
  for (const item of refs) {
    const token = referencePromptToken(item);
    if (next.includes(token)) continue;
    for (const alias of referenceMentionAliases(item)) {
      const pattern = new RegExp(escapeRegExp(alias), "i");
      if (!pattern.test(next)) continue;
      next = next.replace(pattern, (match) => `${match} ${token}`);
      break;
    }
  }

  for (const kind of ["image", "video"] as const) {
    const sameKindRefs = refs.filter((item) => item.kind === kind);
    if (sameKindRefs.length !== 1) continue;
    const item = sameKindRefs[0];
    const token = referencePromptToken(item);
    if (next.includes(token)) continue;
    const phrases =
      kind === "image"
        ? ["这张参考图", "这个参考图", "这张图片", "这个图片", "这张图", "这个图"]
        : [
            "这段参考视频",
            "这个参考视频",
            "这段视频素材",
            "这个视频素材",
            "这段动作参考",
            "这个动作参考",
            "这段运动参考",
            "这个运动参考",
            "这段素材",
            "这个素材",
            "这段视频",
            "这个视频",
          ];
    for (const phrase of phrases) {
      const pattern = new RegExp(escapeRegExp(phrase), "i");
      if (!pattern.test(next)) continue;
      next = next.replace(pattern, (match) => `${match} ${token}`);
      break;
    }
  }
  return next;
}

function serializePromptReferenceMentions(
  text: string,
  refs: ReferenceDraft[],
): string {
  return normalizePromptReferenceMentions(
    replaceReferenceDisplayMentionsWithAnchors(text, refs),
    refs,
  );
}

function displayPromptReferenceMentions(
  text: string,
  refs: ReferenceDraft[],
): string {
  let next = text;
  for (const item of refs) {
    next = next.replace(
      new RegExp(escapeRegExp(referencePromptToken(item)), "g"),
      referenceDisplayToken(item),
    );
  }
  return next;
}

function displayPromptEnhanceCandidates(
  candidates: PromptEnhanceCandidate[],
  refs: ReferenceDraft[],
): PromptEnhanceCandidate[] {
  if (refs.length === 0) return candidates;
  return candidates.map((candidate) => ({
    ...candidate,
    prompt: displayPromptReferenceMentions(candidate.prompt, refs),
  }));
}

function promptContainsReferenceMention(text: string, item: ReferenceDraft): boolean {
  return (
    text.includes(referencePromptToken(item)) ||
    referenceDisplayAliases(item).some((alias) => text.includes(alias))
  );
}

function preservePromptReferenceTokens(
  promptText: string,
  sourceText: string,
  refs: ReferenceDraft[],
): string {
  if (!promptText.trim() || refs.length === 0) return promptText;
  const missingTokens = refs
    .map((item) => referencePromptToken(item))
    .filter((token) => sourceText.includes(token) && !promptText.includes(token));
  if (missingTokens.length === 0) return promptText;
  const trimmed = promptText.trimEnd();
  const suffix = `保持参考锚点 ${missingTokens.join("、")} 对应的素材约束。`;
  return `${trimmed}${/[。.!?？]$/.test(trimmed) ? " " : "。"}${suffix}`;
}

function anchorPromptEnhanceCandidates(
  candidates: PromptEnhanceCandidate[],
  sourceText: string,
  refs: ReferenceDraft[],
): PromptEnhanceCandidate[] {
  if (refs.length === 0) return candidates;
  return candidates.map((candidate) => ({
    ...candidate,
    prompt: preservePromptReferenceTokens(candidate.prompt, sourceText, refs),
  }));
}

function buildPromptEnhanceCandidates(
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

function applyPromptEnhanceCandidateState(
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

function notifyCompletedPromptEnhancement(
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
    candidateCount > 1
      ? `已生成 ${candidateCount} 个优化方案`
      : "提示词已优化",
  );
}

function interruptedPromptEnhanceDescription(description?: string): string {
  return description
    ? `${description} 已保留已生成内容，可继续编辑或重试。`
    : "已保留已生成内容，可继续编辑或重试。";
}

function referenceMediaPayload(item: ReferenceDraft): VideoReferenceMediaIn {
  if (item.url) {
    return {
      kind: item.kind,
      url: item.url,
      label: item.label,
      ref_id: item.ref_id,
    };
  }
  return {
    kind: item.kind,
    image_id: item.kind === "image" ? item.image_id ?? null : null,
    video_id: item.kind === "video" ? item.video_id ?? null : null,
    label: item.label,
    ref_id: item.ref_id,
  };
}

function referencesForVideoAction(
  action: VideoAction,
  references: ReferenceDraft[],
): ReferenceDraft[] {
  return action === "reference" ? references : [];
}

function promptForVideoAction(
  action: VideoAction,
  prompt: string,
  references: ReferenceDraft[],
): string {
  const trimmed = prompt.trim();
  return action === "reference"
    ? serializePromptReferenceMentions(trimmed, references)
    : trimmed;
}

function inputImageForVideoAction(
  action: VideoAction,
  inputImageId: string,
): string | null {
  return action === "i2v" ? inputImageId.trim() || null : null;
}

function referencePayloadForVideoAction(
  action: VideoAction,
  references: ReferenceDraft[],
): VideoReferenceMediaIn[] {
  return referencesForVideoAction(action, references).map(referenceMediaPayload);
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

function referenceDraftFromHistory(
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
    image_id: ref.kind === "image" ? ref.image_id ?? null : null,
    video_id: ref.kind === "video" ? ref.video_id ?? null : null,
    url: ref.url ?? null,
    label: ref.label || fallbackLabel,
    ref_id: ref.ref_id || referenceRefId(ref.kind, kindIndex),
    display: historyReferenceDisplay(ref),
    previewUrl: historyReferencePreviewUrl(ref),
  };
}

function videoConfigurationIssue({
  createPending,
  optionsLoading,
  options,
  selectedModel,
  availableResolutions,
  resolution,
  availableDurations,
  durationS,
}: {
  createPending: boolean;
  optionsLoading: boolean;
  options: VideoOptionsOut | undefined;
  selectedModel: string;
  availableResolutions: string[];
  resolution: string;
  availableDurations: number[];
  durationS: number;
}): string | null {
  if (createPending) return "正在提交";
  if (optionsLoading) return "正在读取配置";
  if (!options?.enabled) return options?.unavailable_reason ?? "功能未启用";
  if (!selectedModel) return "没有可用模型";
  if (!availableResolutions.includes(resolution)) return "当前模型不支持该分辨率";
  if (!availableDurations.includes(durationS)) return "当前模型不支持该时长";
  return null;
}

function videoInputIssue({
  prompt,
  action,
  inputImageId,
  referenceCount,
  referenceLimitError,
}: {
  prompt: string;
  action: VideoAction;
  inputImageId: string;
  referenceCount: number;
  referenceLimitError: string | null;
}): string | null {
  if (!prompt.trim()) return "先填写描述";
  if (action === "i2v" && !inputImageId.trim()) return "需要上传首帧或填写图片 ID";
  if (action === "reference" && referenceCount === 0) return "先添加参考素材";
  if (action === "reference" && referenceLimitError) return referenceLimitError;
  return null;
}

function videoEstimateIssue(
  seedIsValid: boolean,
  estimate: { tokens: number; micro: number } | null,
): string | null {
  if (!seedIsValid) return "Seed 需为 -1 到 4294967295 的整数";
  if (estimate === null) return "缺少预扣估算";
  return null;
}

function videoSubmitDisabledReason({
  createPending,
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
  referenceCount,
  referenceLimitError,
  seedIsValid,
  estimate,
}: {
  createPending: boolean;
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
  referenceCount: number;
  referenceLimitError: string | null;
  seedIsValid: boolean;
  estimate: { tokens: number; micro: number } | null;
}): string {
  return (
    videoConfigurationIssue({
      createPending,
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
      referenceCount,
      referenceLimitError,
    }) ??
    videoEstimateIssue(seedIsValid, estimate) ??
    "可以提交"
  );
}

export default function VideoPage() {
  const qc = useQueryClient();
  const fileRef = useRef<HTMLInputElement | null>(null);
  const referenceFileRef = useRef<HTMLInputElement | null>(null);
  const promptRef = useRef<HTMLTextAreaElement | null>(null);
  const promptEnhanceAbortRef = useRef<AbortController | null>(null);
  const terminalHistorySyncedRef = useRef<Set<string>>(new Set());
  const refreshInFlightRef = useRef<Set<string>>(new Set());
  const scheduledRefreshTimersRef = useRef<Map<string, number>>(new Map());
  const pendingHistoryRefreshRef = useRef<Set<string>>(new Set());
  const lastRefreshAtRef = useRef<Map<string, number>>(new Map());
  const refreshBackoffUntilRef = useRef<Map<string, number>>(new Map());
  const refreshFailureCountRef = useRef<Map<string, number>>(new Map());
  const [action, setAction] = useState<VideoAction>("t2v");
  const [prompt, setPrompt] = useState("");
  const [model, setModel] = useState("");
  const [durationS, setDurationS] = useState(5);
  const [resolution, setResolution] = useState("720p");
  const [aspectRatio, setAspectRatio] = useState("adaptive");
  const [generateAudio, setGenerateAudio] = useState(true);
  const [seed, setSeed] = useState("");
  const [inputImageId, setInputImageId] = useState("");
  const [uploadedLabel, setUploadedLabel] = useState("");
  const [referenceMedia, setReferenceMedia] = useState<ReferenceDraft[]>([]);
  const [referencePreviewItem, setReferencePreviewItem] = useState<ReferenceDraft | null>(null);
  const referenceMediaRef = useRef<ReferenceDraft[]>([]);
  const [assetUrlInput, setAssetUrlInput] = useState("");
  const [assetReferenceKind, setAssetReferenceKind] = useState<ReferenceKind>("video");
  const [items, setItems] = useState<VideoGenerationOut[]>([]);
  const [selectedVideoId, setSelectedVideoId] = useState("");
  const [isEnhancingPrompt, setIsEnhancingPrompt] = useState(false);
  const [promptEnhancePreview, setPromptEnhancePreview] = useState("");
  const [promptEnhanceCandidates, setPromptEnhanceCandidates] = useState<
    PromptEnhanceCandidate[]
  >([]);
  const [selectedPromptEnhanceCandidateId, setSelectedPromptEnhanceCandidateId] =
    useState("");
  const [historyFilter, setHistoryFilter] = useState<VideoHistoryFilter>("all");
  const [isTaskPanelOpen, setIsTaskPanelOpen] = useState(false);
  const promptEnhancePanelVisible =
    isEnhancingPrompt ||
    Boolean(promptEnhancePreview.trim()) ||
    promptEnhanceCandidates.length > 0;
  useBodyScrollLock(isTaskPanelOpen, {
    bodyOverscrollBehavior: "none",
    documentOverscrollBehavior: "none",
  });

  const optionsQ = useQuery({
    queryKey: ["video", "options"],
    queryFn: getVideoOptions,
    retry: false,
    staleTime: 60_000,
    gcTime: 5 * 60_000,
  });
  const historyQ = useInfiniteQuery({
    queryKey: ["video", "generations"],
    queryFn: ({ pageParam }) =>
      listVideoGenerations({
        cursor: pageParam,
        limit: VIDEO_HISTORY_PAGE_SIZE,
      }),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    retry: false,
    staleTime: 20_000,
    gcTime: 5 * 60_000,
  });
  const historyItems = useMemo(
    () => historyQ.data?.pages.flatMap((page) => page.items) ?? [],
    [historyQ.data?.pages],
  );

  const options = optionsQ.data;

  useEffect(() => {
    referenceMediaRef.current = referenceMedia;
  }, [referenceMedia]);

  const effectiveItems = useMemo(
    () => mergeById(historyItems, items),
    [historyItems, items],
  );
  const activeItems = useMemo(
    () => effectiveItems.filter(isActiveVideo),
    [effectiveItems],
  );
  const seedIsValid = !seed.trim() || parseSeed(seed) !== null;
  const completedVideoItems = useMemo(
    () => effectiveItems.filter(hasVideo),
    [effectiveItems],
  );
  const playbackVideoItem = useMemo(
    () =>
      selectedVideoId
        ? completedVideoItems.find((item) => item.video.id === selectedVideoId)
        : undefined,
    [completedVideoItems, selectedVideoId],
  );
  const settledHistoryItems = useMemo(
    () => effectiveItems.filter((item) => !isActiveVideo(item)),
    [effectiveItems],
  );
  const succeededHistoryItems = useMemo(
    () => settledHistoryItems.filter((item) => item.status === "succeeded"),
    [settledHistoryItems],
  );
  const failedHistoryItems = useMemo(
    () => settledHistoryItems.filter(isFailedHistoryVideo),
    [settledHistoryItems],
  );
  const filteredHistoryItems = useMemo(() => {
    if (historyFilter === "succeeded") return succeededHistoryItems;
    if (historyFilter === "failed") return failedHistoryItems;
    return settledHistoryItems;
  }, [failedHistoryItems, historyFilter, settledHistoryItems, succeededHistoryItems]);
  const channels = useMemo(
    () => activeItems.map((item) => `task:${item.id}`),
    [activeItems],
  );
  const activeItemIdsKey = useMemo(
    () => activeItems.map((item) => item.id).join("|"),
    [activeItems],
  );

  useEffect(() => {
    prewarmVideoItem(playbackVideoItem);
  }, [playbackVideoItem]);

  const refreshGeneration = useCallback(
    async (id: string, opts: { forceHistorySync?: boolean } = {}) => {
      const next = await getVideoGeneration(id);
      setItems((prev) => mergeById(prev, [next]));
      if (next.video) {
        prewarmVideoItem(next as VideoGenerationWithVideo);
      }

      const terminal = isTerminalVideo(next);
      if (!terminal) {
        terminalHistorySyncedRef.current.delete(id);
      }
      if (
        opts.forceHistorySync ||
        (terminal && !terminalHistorySyncedRef.current.has(id))
      ) {
        if (terminal) terminalHistorySyncedRef.current.add(id);
        await qc.invalidateQueries({ queryKey: ["video", "generations"] });
      }
    },
    [qc],
  );

  const refreshGenerationSafe = useCallback(
    async (id: string, opts: { forceHistorySync?: boolean } = {}) => {
      if (opts.forceHistorySync) {
        pendingHistoryRefreshRef.current.add(id);
      }
      if (refreshInFlightRef.current.has(id)) return;

      refreshInFlightRef.current.add(id);
      const forceHistorySync =
        opts.forceHistorySync || pendingHistoryRefreshRef.current.has(id);
      pendingHistoryRefreshRef.current.delete(id);

      try {
        await refreshGeneration(id, { forceHistorySync });
        refreshFailureCountRef.current.delete(id);
        refreshBackoffUntilRef.current.delete(id);
      } catch (err) {
        const nextFailures = (refreshFailureCountRef.current.get(id) ?? 0) + 1;
        refreshFailureCountRef.current.set(id, nextFailures);
        const backoffMs = Math.min(
          VIDEO_REFRESH_RETRY_MAX_MS,
          VIDEO_REFRESH_RETRY_BASE_MS * 2 ** Math.min(nextFailures - 1, 4),
        );
        refreshBackoffUntilRef.current.set(id, Date.now() + backoffMs);
        try {
          console.warn("[video] generation refresh failed", {
            id,
            failures: nextFailures,
            retryInMs: backoffMs,
            err,
          });
        } catch {
          /* console unavailable */
        }
      } finally {
        refreshInFlightRef.current.delete(id);
      }
    },
    [refreshGeneration],
  );

  const scheduleGenerationRefresh = useCallback(
    (
      id: string,
      opts: { forceHistorySync?: boolean; delayMs?: number } = {},
    ) => {
      if (!id) return;
      if (opts.forceHistorySync) {
        pendingHistoryRefreshRef.current.add(id);
      }
      if (scheduledRefreshTimersRef.current.has(id)) return;

      const now = Date.now();
      const lastRefreshAt = lastRefreshAtRef.current.get(id) ?? 0;
      const minIntervalDelay = Math.max(
        0,
        VIDEO_REFRESH_MIN_INTERVAL_MS - (now - lastRefreshAt),
      );
      const backoffDelay = Math.max(
        0,
        (refreshBackoffUntilRef.current.get(id) ?? 0) - now,
      );
      const delayMs = Math.max(opts.delayMs ?? 0, minIntervalDelay, backoffDelay);

      const timer = window.setTimeout(() => {
        scheduledRefreshTimersRef.current.delete(id);
        lastRefreshAtRef.current.set(id, Date.now());
        const forceHistorySync = pendingHistoryRefreshRef.current.has(id);
        pendingHistoryRefreshRef.current.delete(id);
        void refreshGenerationSafe(id, { forceHistorySync });
      }, delayMs);
      scheduledRefreshTimersRef.current.set(id, timer);
    },
    [refreshGenerationSafe],
  );

  const applyVideoEventSnapshot = useCallback(
    (data: unknown): { id: string; terminal: boolean } | null => {
      const id = videoGenerationEventId(data);
      if (!id) return null;
      setItems((prev) =>
        prev.map((item) =>
          item.id === id ? mergeVideoGenerationEvent(item, data) : item,
        ),
      );
      return { id, terminal: isTerminalVideoEvent(data) };
    },
    [],
  );

  const handlers = useMemo(
    () =>
      Object.fromEntries(
        VIDEO_EVENTS.map((eventName) => [
          eventName,
          (data: unknown) => {
            const snapshot = applyVideoEventSnapshot(data);
            if (snapshot) {
              scheduleGenerationRefresh(snapshot.id, {
                forceHistorySync: snapshot.terminal,
              });
            }
          },
        ]),
      ),
    [applyVideoEventSnapshot, scheduleGenerationRefresh],
  );
  useSSE(channels, handlers);

  useEffect(() => {
    const ids = activeItemIdsKey.split("|").filter(Boolean);
    if (ids.length === 0) return;

    let alive = true;
    const poll = () => {
      if (!alive) return;
      for (const id of ids) scheduleGenerationRefresh(id);
    };

    const initialTimer = window.setTimeout(poll, 800);
    const interval = window.setInterval(poll, VIDEO_ACTIVE_POLL_MS);

    return () => {
      alive = false;
      window.clearTimeout(initialTimer);
      window.clearInterval(interval);
    };
  }, [activeItemIdsKey, scheduleGenerationRefresh]);

  useEffect(() => {
    const refreshVisibleTasks = () => {
      if (document.visibilityState !== "visible") return;
      void qc.invalidateQueries({ queryKey: ["video", "generations"] });
      const ids = activeItemIdsKey.split("|").filter(Boolean);
      for (const id of ids) scheduleGenerationRefresh(id);
    };

    window.addEventListener("focus", refreshVisibleTasks);
    document.addEventListener("visibilitychange", refreshVisibleTasks);
    return () => {
      window.removeEventListener("focus", refreshVisibleTasks);
      document.removeEventListener("visibilitychange", refreshVisibleTasks);
    };
  }, [activeItemIdsKey, qc, scheduleGenerationRefresh]);

  useEffect(
    () => () => {
      for (const timer of scheduledRefreshTimersRef.current.values()) {
        window.clearTimeout(timer);
      }
      scheduledRefreshTimersRef.current.clear();
    },
    [],
  );

  const availableModels = useMemo(
    () => options?.models.filter((item) => item.actions.includes(action)) ?? [],
    [action, options?.models],
  );
  const selectedModel = model || firstModelForAction(options, action);
  const referenceLimits = useMemo(
    () => referenceLimitsForModel(selectedModel),
    [selectedModel],
  );
  const assetReferenceKindOptions = useMemo<ReferenceKind[]>(
    () =>
      isNewApiVideoModel(selectedModel) ? REFERENCE_KINDS : ["image", "video"],
    [selectedModel],
  );
  const selectedAssetReferenceKind = assetReferenceKindOptions.includes(
    assetReferenceKind,
  )
    ? assetReferenceKind
    : "video";
  const referenceCounts = useMemo(
    () => referenceCountsFor(referenceMedia),
    [referenceMedia],
  );
  const referenceLimitError = referenceLimitViolation(referenceMedia, referenceLimits);
  const selectedBillingModel = billingModelForAction(options, selectedModel, action);
  const availableResolutions = useMemo(
    () => resolutionOptionsForModel(options, selectedModel),
    [options, selectedModel],
  );
  const effectiveResolution = availableResolutions.includes(resolution)
    ? resolution
    : preferredResolution(availableResolutions);
  const availableDurations = useMemo(
    () => durationOptionsForModel(options, selectedModel, action, effectiveResolution),
    [action, effectiveResolution, options, selectedModel],
  );
  const effectiveDurationS = availableDurations.includes(durationS)
    ? durationS
    : preferredDuration(availableDurations);
  const estimate = estimateHoldMicro(options, {
    model: selectedModel,
    billingModel: selectedBillingModel,
    action,
    resolution: effectiveResolution,
    durationS: effectiveDurationS,
    referenceHasVideo: referenceMedia.some((item) => item.kind === "video"),
  });
  const clearPromptEnhanceChoices = useCallback(() => {
    setPromptEnhancePreview("");
    setPromptEnhanceCandidates([]);
    setSelectedPromptEnhanceCandidateId("");
  }, []);

  const clearPromptEnhanceSelection = useCallback(() => {
    setPromptEnhancePreview("");
    setSelectedPromptEnhanceCandidateId("");
  }, []);

  const insertPromptText = useCallback((text: string) => {
    clearPromptEnhanceSelection();
    const target = promptRef.current;
    if (!target) {
      setPrompt((prev) => `${prev}${prev.endsWith(" ") || !prev ? "" : " "}${text}`);
      return;
    }
    const start = target.selectionStart ?? prompt.length;
    const end = target.selectionEnd ?? prompt.length;
    const before = prompt.slice(0, start);
    const after = prompt.slice(end);
    const spacer = before && !before.endsWith(" ") ? " " : "";
    const next = `${before}${spacer}${text}${after.startsWith(" ") || !after ? "" : " "}${after}`;
    setPrompt(next);
    requestAnimationFrame(() => {
      const pos = (before + spacer + text).length;
      target.focus();
      target.setSelectionRange(pos, pos);
    });
  }, [clearPromptEnhanceSelection, prompt]);

  const insertReferenceTag = useCallback((item: ReferenceDraft) => {
    insertPromptText(referenceDisplayToken(item));
  }, [insertPromptText]);

  const uploadMut = useMutation({
    mutationFn: (file: File) => uploadImage(file),
    onSuccess: (img) => {
      clearPromptEnhanceChoices();
      setInputImageId(img.id);
      setUploadedLabel(`${img.width}x${img.height}`);
      toast.success("首帧已上传");
    },
    onError: (err) => toast.error("上传失败", { description: err instanceof Error ? err.message : undefined }),
  });

  const referenceUploadMut = useMutation({
    mutationFn: async (file: File) => {
      if (file.type.startsWith("image/")) {
        if (
          referenceMediaRef.current.filter((item) => item.kind === "image").length >=
          referenceLimits.image
        ) {
          throw new Error(referenceLimitMessage("image", referenceLimits.image));
        }
        const img = await uploadImage(file);
        return {
          kind: "image" as const,
          image_id: img.id,
          display: `${img.width}x${img.height}`,
          previewUrl: imageReferencePreviewUrl(img),
        };
      }
      if (file.type.startsWith("video/")) {
        if (
          referenceMediaRef.current.filter((item) => item.kind === "video").length >=
          referenceLimits.video
        ) {
          throw new Error(referenceLimitMessage("video", referenceLimits.video));
        }
        const video = await uploadVideo(file);
        return {
          kind: "video" as const,
          video_id: video.id,
          display: video.size_bytes ? `${Math.round(video.size_bytes / 1024 / 1024)}MB` : "视频",
          previewUrl: cleanReferencePreviewUrl(video.poster_url) ?? videoPosterUrl(video.id),
        };
      }
      throw new Error("只支持图片或视频");
    },
    onSuccess: (ref) => {
      clearPromptEnhanceChoices();
      let accepted = false;
      setReferenceMedia((prev) => {
        const limit = referenceLimits[ref.kind];
        const currentCount = prev.filter((item) => item.kind === ref.kind).length;
        if (currentCount >= limit) {
          toast.error(referenceLimitMessage(ref.kind, limit));
          return prev;
        }
        accepted = true;
        const identity = nextReferenceIdentity(ref.kind, prev);
        return [
          ...prev,
          {
            _key: uuid(),
            kind: ref.kind,
            image_id: ref.kind === "image" ? ref.image_id : null,
            video_id: ref.kind === "video" ? ref.video_id : null,
            label: identity.label,
            ref_id: identity.refId,
            display: ref.display,
            previewUrl: ref.previewUrl,
          },
        ];
      });
      if (accepted) toast.success("参考素材已上传");
    },
    onError: (err) => toast.error("上传失败", { description: err instanceof Error ? err.message : undefined }),
  });

  const addAssetReference = useCallback(() => {
    const url = normalizeAssetUrl(assetUrlInput);
    const kind = selectedAssetReferenceKind;
    if (!url) {
      if (assetUrlInput.trim()) {
        toast.error("请输入 asset-* 或 asset://asset-* 官方素材 ID");
      }
      return;
    }
    if (
      referenceMedia.filter((item) => item.kind === kind).length >=
      referenceLimits[kind]
    ) {
      toast.error(referenceLimitMessage(kind, referenceLimits[kind]));
      return;
    }
    clearPromptEnhanceChoices();
    setReferenceMedia((prev) => [
      ...prev,
      (() => {
        const identity = nextReferenceIdentity(kind, prev);
        return {
          _key: uuid(),
          kind,
          url,
          label: identity.label,
          ref_id: identity.refId,
          display: url,
          previewUrl: null,
        };
      })(),
    ]);
    setAssetUrlInput("");
    toast.success(`官方${referenceKindNoun(kind)}已添加`);
  }, [
    assetUrlInput,
    clearPromptEnhanceChoices,
    referenceLimits,
    referenceMedia,
    selectedAssetReferenceKind,
  ]);

  const createMut = useMutation({
    mutationFn: () =>
      createVideoGeneration({
        action,
        model: selectedModel,
        prompt: promptForVideoAction(action, prompt, referenceMedia),
        input_image_id: inputImageForVideoAction(action, inputImageId),
        reference_media: referencePayloadForVideoAction(action, referenceMedia),
        duration_s: effectiveDurationS,
        resolution: toVideoResolution(effectiveResolution),
        aspect_ratio: aspectRatio,
        generate_audio: generateAudio,
        seed: parseSeed(seed),
        watermark: false,
      }),
    onSuccess: (gen) => {
      terminalHistorySyncedRef.current.delete(gen.id);
      setItems((prev) => mergeById(prev, [gen]));
      setIsTaskPanelOpen(true);
      toast.success("任务已提交");
      scheduleGenerationRefresh(gen.id, { delayMs: 800 });
      void qc.invalidateQueries({ queryKey: ["video", "generations"] });
    },
    onError: (err) => toast.error("提交失败", { description: err instanceof Error ? err.message : undefined }),
  });

  const cancelMut = useMutation({
    mutationFn: cancelVideoGeneration,
    onSuccess: (gen) => {
      setItems((prev) => mergeById(prev, [gen]));
      toast.success("已请求取消", {
        description:
          gen.provider_kind === "dashscope" ||
          gen.provider_kind === "omni_flash" ||
          gen.provider_kind === "volcano_newapi"
            ? "该供应商可能无法中止已提交任务，若上游最终成功仍会按结果计费。"
            : undefined,
      });
      scheduleGenerationRefresh(gen.id, { forceHistorySync: true });
    },
    onError: (err) => toast.error("取消失败", { description: err instanceof Error ? err.message : undefined }),
  });
  const retryMut = useMutation({
    mutationFn: retryVideoGeneration,
    onSuccess: (gen) => {
      terminalHistorySyncedRef.current.delete(gen.id);
      setItems((prev) => mergeById(prev, [gen]));
      toast.success("已重新生成");
      scheduleGenerationRefresh(gen.id, { delayMs: 800 });
    },
    onError: (err) => toast.error("重试失败", { description: err instanceof Error ? err.message : undefined }),
  });
  const deleteMut = useMutation({
    mutationFn: deleteVideo,
    onSuccess: async (_data, videoId) => {
      setItems((prev) =>
        prev.map((item) =>
          item.video?.id === videoId ? { ...item, video: null } : item,
        ),
      );
      setSelectedVideoId((current) => (current === videoId ? "" : current));
      toast.success("视频已删除");
      await qc.invalidateQueries({ queryKey: ["video", "generations"] });
    },
    onError: (err) => toast.error("删除失败", { description: err instanceof Error ? err.message : undefined }),
  });

  const loadAsDraft = useCallback((item: VideoGenerationOut) => {
    clearPromptEnhanceChoices();
    setAction(item.action);
    setModel(item.model);
    setDurationS(item.duration_s);
    setResolution(item.resolution);
    setAspectRatio(item.aspect_ratio);
    setGenerateAudio(item.generate_audio);
    setSeed(item.seed != null ? String(item.seed) : "");
    setInputImageId(item.input_image_id ?? "");
    setUploadedLabel(item.input_image_id ? "已从历史任务载入" : "");
    const draftReferenceMedia = item.reference_media.map((ref, index) =>
      referenceDraftFromHistory(ref, index, item.reference_media),
    );
    setReferenceMedia(draftReferenceMedia);
    setPrompt(displayPromptReferenceMentions(item.prompt, draftReferenceMedia));
    requestAnimationFrame(() => promptRef.current?.focus());
    toast.success("已套用参数");
  }, [clearPromptEnhanceChoices]);

  const canEnhancePrompt = Boolean(
    prompt.trim() ||
      (action === "i2v" && inputImageId.trim()) ||
      (action === "reference" && referenceMedia.length > 0),
  );

  const enhancePromptAction = useCallback(async () => {
    if (isEnhancingPrompt || !canEnhancePrompt) return;
    const original = prompt;
    const activeReferenceMedia = referencesForVideoAction(action, referenceMedia);
    const current = promptForVideoAction(action, prompt, activeReferenceMedia);
    const ctl = new AbortController();
    promptEnhanceAbortRef.current?.abort();
    promptEnhanceAbortRef.current = ctl;
    clearPromptEnhanceChoices();
    setIsEnhancingPrompt(true);
    let accumulated = "";
    try {
      await enhanceVideoPrompt(
        {
          text: current,
          action,
          model: selectedModel,
          duration_s: effectiveDurationS,
          resolution: effectiveResolution,
          aspect_ratio: aspectRatio,
          generate_audio: generateAudio,
          input_image_id: inputImageForVideoAction(action, inputImageId),
          variant_count: VIDEO_PROMPT_VARIANT_COUNT,
          reference_media: referencePayloadForVideoAction(action, referenceMedia),
        },
        (delta) => {
          if (ctl.signal.aborted || promptEnhanceAbortRef.current !== ctl) return;
          accumulated += delta;
          setPromptEnhancePreview(
            displayPromptReferenceMentions(accumulated, activeReferenceMedia),
          );
        },
        ctl.signal,
      );
      const candidates = buildPromptEnhanceCandidates(
        accumulated,
        current,
        activeReferenceMedia,
      );
      const applied = applyPromptEnhanceCandidateState(
        candidates,
        setPrompt,
        setPromptEnhanceCandidates,
        setSelectedPromptEnhanceCandidateId,
      );
      if (applied) {
        setPromptEnhancePreview("");
        notifyCompletedPromptEnhancement(
          applied.recommended,
          applied.autoApply,
          candidates.length,
        );
      } else {
        setPromptEnhancePreview("");
        toast.error("优化失败", { description: "没有收到有效提示词" });
        setPrompt(original);
      }
    } catch (err) {
      if (!ctl.signal.aborted) {
        const description = err instanceof Error ? err.message : undefined;
        if (accumulated.trim()) {
          const candidates = buildPromptEnhanceCandidates(
            accumulated,
            current,
            activeReferenceMedia,
          );
          const applied = applyPromptEnhanceCandidateState(
            candidates,
            setPrompt,
            setPromptEnhanceCandidates,
            setSelectedPromptEnhanceCandidateId,
          );
          if (!applied) {
            setPrompt(
              displayPromptReferenceMentions(
                cleanPromptEnhanceText(accumulated),
                activeReferenceMedia,
              ),
            );
          }
          setPromptEnhancePreview("");
          toast.error("优化中断", {
            description: interruptedPromptEnhanceDescription(description),
          });
        } else {
          toast.error("优化失败", { description });
          setPrompt(original);
        }
      }
    } finally {
      if (promptEnhanceAbortRef.current === ctl) {
        promptEnhanceAbortRef.current = null;
      }
      setIsEnhancingPrompt(false);
    }
  }, [
    action,
    aspectRatio,
    canEnhancePrompt,
    clearPromptEnhanceChoices,
    effectiveDurationS,
    effectiveResolution,
    generateAudio,
    inputImageId,
    isEnhancingPrompt,
    prompt,
    referenceMedia,
    selectedModel,
  ]);

  const scrollPromptEditorIntoView = useCallback(() => {
    const target = promptRef.current;
    if (!target) return;
    target.scrollIntoView({ behavior: motionSafeScrollBehavior(), block: "center" });
    requestAnimationFrame(() => target.focus());
  }, []);

  const applyPromptEnhanceCandidate = useCallback(
    (candidate: PromptEnhanceCandidate) => {
      if (!canApplyPromptEnhanceCandidate(candidate)) return;
      setPrompt(candidate.prompt);
      setSelectedPromptEnhanceCandidateId(candidate.id);
      requestAnimationFrame(() => promptRef.current?.focus({ preventScroll: true }));
    },
    [],
  );

  const handlePromptChange = useCallback(
    (value: string) => {
      clearPromptEnhanceSelection();
      setPrompt(
        action === "reference"
          ? displayPromptReferenceMentions(value, referenceMedia)
          : value,
      );
    },
    [action, clearPromptEnhanceSelection, referenceMedia],
  );

  const resizePromptEditor = useCallback(() => {
    const target = promptRef.current;
    if (!target) return;
    target.style.height = "0px";
    target.style.height = `${target.scrollHeight}px`;
  }, []);

  useEffect(() => {
    resizePromptEditor();
  }, [prompt, resizePromptEditor]);

  useEffect(() => {
    window.addEventListener("resize", resizePromptEditor);
    return () => window.removeEventListener("resize", resizePromptEditor);
  }, [resizePromptEditor]);

  const scrollParametersIntoView = useCallback(() => {
    document.getElementById("video-generation-settings")?.scrollIntoView({
      behavior: motionSafeScrollBehavior(),
      block: "start",
    });
  }, []);

  const submitDisabledReason = useMemo(() => {
    return videoSubmitDisabledReason({
      createPending: createMut.isPending,
      optionsLoading: optionsQ.isLoading,
      options,
      selectedModel,
      availableResolutions,
      resolution: effectiveResolution,
      availableDurations,
      durationS: effectiveDurationS,
      prompt,
      action,
      inputImageId,
      referenceCount: referenceMedia.length,
      referenceLimitError,
      seedIsValid,
      estimate,
    });
  }, [
    action,
    availableDurations,
    availableResolutions,
    createMut.isPending,
    effectiveDurationS,
    estimate,
    inputImageId,
    options,
    optionsQ.isLoading,
    prompt,
    referenceLimitError,
    referenceMedia.length,
    seedIsValid,
    effectiveResolution,
    selectedModel,
  ]);

  const canSubmit =
    Boolean(options?.enabled) &&
    Boolean(selectedModel) &&
    prompt.trim().length > 0 &&
    availableResolutions.includes(effectiveResolution) &&
    availableDurations.includes(effectiveDurationS) &&
    (action === "t2v" ||
      (action === "i2v" && inputImageId.trim().length > 0) ||
      (action === "reference" && referenceMedia.length > 0)) &&
    (action !== "reference" || !referenceLimitError) &&
    seedIsValid &&
    estimate !== null &&
    !createMut.isPending;
  const serviceEnabled = Boolean(options?.enabled);
  const serviceSummary = optionsQ.isLoading
    ? "读取视频服务配置"
    : serviceEnabled
      ? `${availableModels.length} 个模型可用`
      : options?.unavailable_reason ?? "需要先配置可用的视频供应商";
  const parameterProfile = `${effectiveResolution} · ${formatDurationLabel(effectiveDurationS)}`;
  const sourceReady =
    action === "t2v" ||
    (action === "i2v" && inputImageId.trim().length > 0) ||
    (action === "reference" && referenceMedia.length > 0);
  const modelOptionValues = availableModels.map((item) => item.model);
  const durationOptionValues = availableDurations.map(String);
  const aspectRatioOptionValues = options?.aspect_ratios ?? [
    "adaptive",
    "16:9",
    "9:16",
    "1:1",
  ];

  return (
    <div className="min-h-[100dvh] overflow-x-hidden bg-[var(--bg-0)] text-[var(--fg-0)]">
      <div className="hidden md:block">
        <DesktopTopNav active="video" />
      </div>
      <main className="lumen-studio-bg mx-auto flex h-[calc(100dvh-var(--mobile-tabbar-height))] w-full max-w-[1600px] flex-col gap-3 overflow-x-clip overflow-y-auto overscroll-contain px-3 pb-[calc(var(--mobile-tabbar-height)+1rem)] pt-2 md:h-[calc(100dvh-3rem)] md:px-5 md:pb-4">
        <VideoWorkbenchHeader
          mode={actionLabel(action)}
          profile={parameterProfile}
          audio={generateAudio}
          enabled={serviceEnabled}
          loading={optionsQ.isLoading}
          activeCount={activeItems.length}
          historyCount={settledHistoryItems.length}
          serviceSummary={serviceSummary}
          submitState={submitDisabledReason}
          onOpenParameters={scrollParametersIntoView}
          onOpenTasks={() => setIsTaskPanelOpen(true)}
        />

        <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_300px] md:items-start lg:grid-cols-[minmax(0,1fr)_320px] xl:grid-cols-[minmax(0,1fr)_340px] 2xl:grid-cols-[minmax(0,1fr)_360px]">
          <section className="min-w-0">
            <div className="flex flex-col overflow-hidden rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-1)]/72 shadow-[var(--shadow-2)] backdrop-blur-xl">
              <div className="shrink-0 border-b border-[var(--border-subtle)] bg-[var(--bg-1)]/86 p-2.5 sm:p-3">
                <div className="mb-2 flex flex-wrap items-end justify-between gap-2 px-1">
                  <div>
                    <p className="text-sm font-semibold text-[var(--fg-0)]">生成方式</p>
                    <p className="mt-0.5 text-xs text-[var(--fg-2)]">
                      {MODE_COPY[action].description}
                    </p>
                  </div>
                  <span className="text-xs font-medium text-[var(--fg-1)]">
                    {MODE_COPY[action].requirement}
                  </span>
                </div>
                <div className="grid min-w-0 grid-cols-[repeat(3,minmax(0,1fr))] gap-1 rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/74 p-1">
                  {(Object.keys(MODE_COPY) as VideoAction[]).map((key) => (
                    <ModeCard
                      key={key}
                      actionKey={key}
                      selected={action === key}
                      onSelect={() => {
                        clearPromptEnhanceChoices();
                        const nextModel = firstModelForAction(options, key);
                        const nextResolutions = resolutionOptionsForModel(
                          options,
                          nextModel,
                        );
                        const nextResolution = nextResolutions.includes(resolution)
                          ? resolution
                          : preferredResolution(nextResolutions);
                        const nextDurations = durationOptionsForModel(
                          options,
                          nextModel,
                          key,
                          nextResolution,
                        );
                        setAction(key);
                        setModel(nextModel);
                        setDurationS((prev) =>
                          durationOrPreferred(prev, nextDurations),
                        );
                      }}
                    />
                  ))}
                </div>
              </div>

              <div className="space-y-3 p-3 pb-[calc(var(--mobile-tabbar-height)+2rem)] sm:p-4 sm:pb-[calc(var(--mobile-tabbar-height)+2rem)] md:pb-5 lg:pb-6">
                {action === "i2v" && (
                  <section className="overflow-hidden rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)]/66">
                    <input
                      ref={fileRef}
                      type="file"
                      accept="image/png,image/jpeg,image/webp,image/mpo"
                      className="hidden"
                      onChange={(event) => {
                        const file = event.target.files?.[0];
                        if (file) uploadMut.mutate(file);
                        event.target.value = "";
                      }}
                    />
                    <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[var(--border-subtle)] px-3 py-2.5">
                      <div className="flex items-center gap-2">
                        <ImageIcon className="h-4 w-4 text-[var(--accent)]" />
                        <p className="text-sm font-semibold text-[var(--fg-0)]">首帧素材</p>
                      </div>
                      <span className="text-xs text-[var(--fg-2)]">
                        用图片确定构图与起始状态
                      </span>
                    </div>
                    <div className="grid gap-3 p-3 lg:grid-cols-[minmax(0,1fr)_minmax(220px,0.42fr)] lg:items-end">
                      <button
                        type="button"
                        onClick={() => fileRef.current?.click()}
                        disabled={uploadMut.isPending}
                        className="group flex min-h-16 items-center gap-3 rounded-[var(--radius-control)] border border-dashed border-[var(--border)] bg-[var(--bg-1)]/72 p-3 text-left transition-[background-color,border-color] hover:border-[var(--border-strong)] hover:bg-[var(--bg-2)] disabled:pointer-events-none disabled:opacity-60"
                      >
                        <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-[var(--radius-control)] border border-[var(--accent-border)] bg-[var(--accent-soft)] text-[var(--accent)]">
                          {uploadMut.isPending ? (
                            <RefreshCw className="h-4 w-4 animate-spin" />
                          ) : (
                            <Upload className="h-4 w-4" />
                          )}
                        </span>
                        <span className="min-w-0">
                          <span className="block text-sm font-semibold text-[var(--fg-0)]">
                            {inputImageId ? "替换首帧" : "上传首帧图片"}
                          </span>
                          <span className="mt-1 block truncate text-xs text-[var(--fg-2)]">
                            {uploadedLabel || inputImageId
                              ? uploadedLabel || "已填写图片 ID"
                              : "PNG、JPEG、WEBP"}
                          </span>
                        </span>
                      </button>
                      <label className="space-y-1.5">
                        <span className="type-caption text-[var(--fg-2)]">或粘贴图片 ID</span>
                        <input
                          value={inputImageId}
                          onChange={(event) => {
                            clearPromptEnhanceChoices();
                            setInputImageId(event.target.value);
                            setUploadedLabel("");
                          }}
                          placeholder="image_id"
                          className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-1)] px-3 font-mono text-xs text-[var(--fg-0)] outline-none transition-colors focus:border-[var(--accent)]/60"
                        />
                      </label>
                    </div>
                  </section>
                )}

                {action === "reference" && (
                  <section className="overflow-hidden rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)]/66">
                    <input
                      ref={referenceFileRef}
                      type="file"
                      accept="image/png,image/jpeg,image/webp,image/mpo,video/mp4,video/quicktime"
                      className="hidden"
                      onChange={(event) => {
                        const file = event.target.files?.[0];
                        if (file) referenceUploadMut.mutate(file);
                        event.target.value = "";
                      }}
                    />
                    <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[var(--border-subtle)] px-3 py-2.5">
                      <div className="flex items-center gap-2">
                        <VideoIcon className="h-4 w-4 text-[var(--accent)]" />
                        <p className="text-sm font-semibold text-[var(--fg-0)]">参考素材</p>
                      </div>
                      <div className="flex flex-wrap items-center gap-2 text-[11px] text-[var(--fg-2)]">
                        <span>图片 {referenceCounts.image}/{referenceLimits.image}</span>
                        <span>视频 {referenceCounts.video}/{referenceLimits.video}</span>
                        <span>音频 {referenceCounts.audio}/{referenceLimits.audio}</span>
                      </div>
                    </div>
                    <div className="space-y-3 p-3">
                      <div className="flex flex-wrap items-center gap-2">
                        <Button
                          variant="outline"
                          size="sm"
                          loading={referenceUploadMut.isPending}
                          onClick={() => referenceFileRef.current?.click()}
                          leftIcon={<Upload className="h-3.5 w-3.5" />}
                        >
                          上传参考
                        </Button>
                        <p className="min-w-0 flex-1 text-xs leading-5 text-[var(--fg-2)]">
                          点击素材可预览，点击文字可将 @图片1 / @视频1 插入描述。
                        </p>
                      </div>
                      <div className="flex min-w-0 gap-2 overflow-x-auto pb-1">
                        {referenceMedia.map((item) => (
                          <ReferenceChip
                            key={item._key}
                            item={item}
                            active={promptContainsReferenceMention(prompt, item)}
                            onInsert={() => insertReferenceTag(item)}
                            onPreview={() => setReferencePreviewItem(item)}
                            onRemove={() => {
                              clearPromptEnhanceChoices();
                              setReferencePreviewItem((current) =>
                                current?._key === item._key ? null : current,
                              );
                              setReferenceMedia((prev) =>
                                prev.filter((ref) => ref._key !== item._key),
                              );
                            }}
                          />
                        ))}
                        {referenceMedia.length === 0 && (
                          <button
                            type="button"
                            onClick={() => referenceFileRef.current?.click()}
                            className="flex min-h-24 min-w-[240px] flex-col items-center justify-center gap-2 rounded-[var(--radius-control)] border border-dashed border-[var(--border)] bg-[var(--bg-1)]/50 px-5 text-center text-xs text-[var(--fg-2)] transition-colors hover:border-[var(--border-strong)] hover:bg-[var(--bg-2)]"
                          >
                            <Upload className="h-4 w-4" />
                            添加图片或视频参考
                          </button>
                        )}
                      </div>
                    </div>
                    <details className="group border-t border-[var(--border-subtle)]">
                      <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-3 py-2.5 text-xs font-medium text-[var(--fg-1)] transition-colors hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)]">
                        <span className="inline-flex items-center gap-2">
                          <Tags className="h-3.5 w-3.5 text-[var(--fg-2)]" />
                          添加官方素材 ID
                        </span>
                        <ChevronDown className="h-3.5 w-3.5 transition-transform group-open:rotate-180" />
                      </summary>
                      <div className="flex flex-wrap items-center gap-2 border-t border-[var(--border-subtle)] bg-[var(--bg-1)]/56 p-3">
                        <div className="inline-flex h-10 shrink-0 overflow-hidden rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] p-0.5">
                          {assetReferenceKindOptions.map((kind) => {
                            const active = selectedAssetReferenceKind === kind;
                            return (
                              <button
                                key={kind}
                                type="button"
                                aria-pressed={active}
                                onClick={() => setAssetReferenceKind(kind)}
                                className={cn(
                                  "inline-flex min-w-12 items-center justify-center rounded-[calc(var(--radius-control)-2px)] px-2.5 text-xs font-semibold transition-colors",
                                  active
                                    ? "bg-[var(--accent)] text-[var(--accent-on)]"
                                    : "text-[var(--fg-2)] hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)]",
                                )}
                              >
                                {referenceKindNoun(kind)}
                              </button>
                            );
                          })}
                        </div>
                        <div className="relative min-w-[190px] flex-1">
                          <Tags className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-[var(--fg-2)]" />
                          <input
                            value={assetUrlInput}
                            onChange={(event) => setAssetUrlInput(event.target.value)}
                            onKeyDown={(event) => {
                              if (event.key === "Enter") {
                                event.preventDefault();
                                addAssetReference();
                              }
                            }}
                            placeholder="asset://asset-..."
                            className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] pl-9 pr-3 font-mono text-xs text-[var(--fg-0)] outline-none transition-colors focus:border-[var(--accent)]/60"
                          />
                        </div>
                        <Button
                          variant="secondary"
                          size="sm"
                          disabled={!assetUrlInput.trim()}
                          onClick={addAssetReference}
                        >
                          添加素材
                        </Button>
                      </div>
                    </details>
                  </section>
                )}

                <section className="overflow-hidden rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)]/72 shadow-[var(--shadow-1)]">
                  <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[var(--border-subtle)] px-3 py-2.5 sm:px-4">
                    <div>
                      <p className="text-sm font-semibold text-[var(--fg-0)]">镜头描述</p>
                      <p className="mt-0.5 text-xs text-[var(--fg-2)]">
                        描述主体、动作、运镜与时间推进
                      </p>
                    </div>
                    <div className="flex items-center gap-2">
                      <span className="text-xs tabular-nums text-[var(--fg-2)]">
                        {prompt.length.toLocaleString()} / 10,000
                      </span>
                      <Button
                        variant="secondary"
                        size="sm"
                        loading={isEnhancingPrompt}
                        disabled={!canEnhancePrompt}
                        onClick={() => void enhancePromptAction()}
                        leftIcon={<Sparkles className="h-3.5 w-3.5" />}
                      >
                        优化描述
                      </Button>
                    </div>
                  </div>
                  <textarea
                    ref={promptRef}
                    value={prompt}
                    onChange={(event) => handlePromptChange(event.target.value)}
                    readOnly={isEnhancingPrompt}
                    rows={9}
                    maxLength={10000}
                    placeholder="写清主体、动作轨迹、镜头运动、首尾时间推进；点击参考素材插入 @图片1 / @视频1 来指定素材。"
                    className={cn(
                      "min-h-[240px] w-full resize-none overflow-y-hidden bg-transparent px-3 py-3 text-sm leading-7 text-[var(--fg-0)] outline-none placeholder:text-[var(--fg-2)] sm:min-h-[320px] sm:px-4 sm:py-4 lg:min-h-[360px]",
                      isEnhancingPrompt && "cursor-wait",
                    )}
                  />
                  <div className="border-t border-[var(--border-subtle)] bg-[var(--bg-1)]/62 px-3 py-2.5 sm:px-4">
                    <div className="flex gap-2 overflow-x-auto pb-0.5">
                      {PROMPT_CHIPS.map((chip) => (
                        <button
                          key={chip}
                          type="button"
                          disabled={isEnhancingPrompt}
                          onClick={() => insertPromptText(chip)}
                          className="shrink-0 rounded-full border border-[var(--border)] bg-[var(--bg-0)] px-3 py-1.5 text-xs text-[var(--fg-1)] transition-colors hover:border-[var(--border-strong)] hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)] disabled:pointer-events-none disabled:opacity-50"
                        >
                          {chip}
                        </button>
                      ))}
                    </div>
                  </div>
                </section>

                {promptEnhancePanelVisible && (
                  <div className="scroll-mt-4 md:scroll-mt-6">
                    <PromptEnhanceChooser
                      loading={isEnhancingPrompt}
                      preview={promptEnhancePreview}
                      candidates={promptEnhanceCandidates}
                      selectedId={selectedPromptEnhanceCandidateId}
                      onSelect={applyPromptEnhanceCandidate}
                      onDismiss={clearPromptEnhanceChoices}
                      onReturnToEditor={scrollPromptEditorIntoView}
                    />
                  </div>
                )}
              </div>
            </div>
          </section>

          <VideoParameterPanel
            className="scroll-mt-20 md:sticky md:top-[76px]"
            selectedModel={selectedModel}
            modelOptions={modelOptionValues}
            durationS={effectiveDurationS}
            durationOptions={durationOptionValues}
            resolution={effectiveResolution}
            resolutionOptions={availableResolutions}
            aspectRatio={aspectRatio}
            aspectRatioOptions={aspectRatioOptionValues}
            seed={seed}
            generateAudio={generateAudio}
            estimate={estimate}
            canSubmit={canSubmit}
            reason={submitDisabledReason}
            loading={createMut.isPending}
            sourceReady={sourceReady}
            onSubmit={() => createMut.mutate()}
            onModelChange={(value) => {
              clearPromptEnhanceChoices();
              const nextResolutions = resolutionOptionsForModel(options, value);
              const nextResolution = nextResolutions.includes(resolution)
                ? resolution
                : preferredResolution(nextResolutions);
              const nextDurations = durationOptionsForModel(
                options,
                value,
                action,
                nextResolution,
              );
              setModel(value);
              setDurationS((prev) => durationOrPreferred(prev, nextDurations));
            }}
            onDurationChange={(value) => {
              clearPromptEnhanceChoices();
              setDurationS(Number(value));
            }}
            onResolutionChange={(value) => {
              clearPromptEnhanceChoices();
              const nextDurations = durationOptionsForModel(
                options,
                selectedModel,
                action,
                value,
              );
              setResolution(value);
              setDurationS((prev) => durationOrPreferred(prev, nextDurations));
            }}
            onAspectRatioChange={(value) => {
              clearPromptEnhanceChoices();
              setAspectRatio(value);
            }}
            onSeedChange={setSeed}
            onGenerateAudioChange={(value) => {
              clearPromptEnhanceChoices();
              setGenerateAudio(value);
            }}
          />
        </div>
      </main>
      <VideoTaskDrawer
        open={isTaskPanelOpen}
        onClose={() => setIsTaskPanelOpen(false)}
        activeItems={activeItems}
        historyItems={filteredHistoryItems}
        historyFilter={historyFilter}
        historyCounts={{
          all: settledHistoryItems.length,
          succeeded: succeededHistoryItems.length,
          failed: failedHistoryItems.length,
        }}
        historyLoading={historyQ.isLoading}
        historyHasNextPage={Boolean(historyQ.hasNextPage)}
        historyFetchingNextPage={historyQ.isFetchingNextPage}
        retryDisabled={retryMut.isPending}
        selectedVideoId={selectedVideoId}
        onHistoryFilterChange={setHistoryFilter}
        onRefresh={() => void historyQ.refetch()}
        onLoadMore={() => void historyQ.fetchNextPage()}
        onCancel={(item) => cancelMut.mutate(item.id)}
        onRetry={(item) => retryMut.mutate(item.id)}
        onCopy={(item) => {
          void navigator.clipboard?.writeText(item.prompt);
          toast.success("描述已复制");
        }}
        onUseDraft={(item) => {
          loadAsDraft(item);
          setIsTaskPanelOpen(false);
        }}
        onDelete={(item) => {
          if (item.video) deleteMut.mutate(item.video.id);
        }}
        onPreview={(item) => {
          if (!hasVideo(item)) return;
          setSelectedVideoId(item.video.id);
          setIsTaskPanelOpen(false);
        }}
      />
      {playbackVideoItem && (
        <VideoPreviewDialog
          item={playbackVideoItem}
          onClose={() => setSelectedVideoId("")}
          onUseDraft={() => loadAsDraft(playbackVideoItem)}
          onRetry={() => retryMut.mutate(playbackVideoItem.id)}
          onCopy={() => {
            void navigator.clipboard?.writeText(playbackVideoItem.prompt);
            toast.success("描述已复制");
          }}
          onDelete={() => deleteMut.mutate(playbackVideoItem.video.id)}
        />
      )}
      {referencePreviewItem && (
        <ReferenceMediaPreviewDialog
          item={referencePreviewItem}
          onClose={() => setReferencePreviewItem(null)}
          onInsert={() => {
            insertReferenceTag(referencePreviewItem);
            setReferencePreviewItem(null);
          }}
        />
      )}
      <div className="md:hidden">
        <MobileTabBar />
      </div>
    </div>
  );
}

function activeVideoTaskSummary(
  activeCount: number,
  historyCount: number,
): string {
  return activeCount > 0
    ? `${activeCount} 个任务正在处理`
    : `${historyCount} 条历史记录`;
}

function videoHistoryCountText({
  loading,
  count,
  hasNextPage,
}: {
  loading: boolean;
  count: number;
  hasNextPage: boolean;
}): string {
  if (loading) return "读取中";
  return `${count}${hasNextPage ? "+" : ""} 条`;
}

function videoHistoryEmptyCopy(
  historyFilter: VideoHistoryFilter,
  activeCount: number,
  loading: boolean,
): { title: string; description: string } {
  if (loading) {
    return { title: "读取中", description: "正在读取视频任务记录。" };
  }
  if (activeCount > 0) {
    return {
      title: `暂无${videoHistoryFilterLabel(historyFilter)}记录`,
      description: "当前任务完成后会进入历史。",
    };
  }
  return {
    title: `暂无${videoHistoryFilterLabel(historyFilter)}记录`,
    description:
      historyFilter === "all"
        ? "提交后的任务会在这里保留参数、状态和结果。"
        : "切换筛选可查看其他状态。",
  };
}

function ActiveVideoTaskSection({
  items,
  retryDisabled,
  onCancel,
  onRetry,
  onCopy,
  onUseDraft,
}: {
  items: VideoGenerationOut[];
  retryDisabled: boolean;
  onCancel: (item: VideoGenerationOut) => void;
  onRetry: (item: VideoGenerationOut) => void;
  onCopy: (item: VideoGenerationOut) => void;
  onUseDraft: (item: VideoGenerationOut) => void;
}) {
  if (items.length === 0) return null;
  return (
    <section className="space-y-2.5">
      <div className="flex items-center justify-between gap-3 px-1">
        <p className="type-caption text-[var(--fg-2)]">正在进行</p>
        <span className="text-xs tabular-nums text-[var(--fg-2)]">
          {items.length} 条
        </span>
      </div>
      <div className="grid gap-2.5">
        {items.map((item) => (
          <TaskRow
            key={item.id}
            item={item}
            onCancel={() => onCancel(item)}
            onRetry={() => onRetry(item)}
            retryDisabled={retryDisabled}
            onCopy={() => onCopy(item)}
            onUseDraft={() => onUseDraft(item)}
            showPreview={false}
          />
        ))}
      </div>
    </section>
  );
}

function VideoTaskHistorySection({
  items,
  activeCount,
  historyFilter,
  historyCounts,
  loading,
  hasNextPage,
  fetchingNextPage,
  retryDisabled,
  selectedVideoId,
  onHistoryFilterChange,
  onLoadMore,
  onCancel,
  onRetry,
  onCopy,
  onUseDraft,
  onDelete,
  onPreview,
}: {
  items: VideoGenerationOut[];
  activeCount: number;
  historyFilter: VideoHistoryFilter;
  historyCounts: Record<VideoHistoryFilter, number>;
  loading: boolean;
  hasNextPage: boolean;
  fetchingNextPage: boolean;
  retryDisabled: boolean;
  selectedVideoId: string;
  onHistoryFilterChange: (value: VideoHistoryFilter) => void;
  onLoadMore: () => void;
  onCancel: (item: VideoGenerationOut) => void;
  onRetry: (item: VideoGenerationOut) => void;
  onCopy: (item: VideoGenerationOut) => void;
  onUseDraft: (item: VideoGenerationOut) => void;
  onDelete: (item: VideoGenerationOut) => void;
  onPreview: (item: VideoGenerationOut) => void;
}) {
  const emptyCopy = videoHistoryEmptyCopy(historyFilter, activeCount, loading);
  return (
    <section className="space-y-2.5">
      <div className="flex items-center justify-between gap-3 px-1">
        <p className="type-caption text-[var(--fg-2)]">历史记录</p>
        <span className="text-xs tabular-nums text-[var(--fg-2)]">
          {videoHistoryCountText({
            loading,
            count: items.length,
            hasNextPage,
          })}
        </span>
      </div>
      <HistoryFilterTabs
        value={historyFilter}
        counts={historyCounts}
        loading={loading}
        onChange={onHistoryFilterChange}
      />
      <div className="grid gap-2.5">
        {items.map((item) => (
          <TaskRow
            key={item.id}
            item={item}
            onCancel={() => onCancel(item)}
            onRetry={() => onRetry(item)}
            retryDisabled={retryDisabled}
            onCopy={() => onCopy(item)}
            onUseDraft={() => onUseDraft(item)}
            onDelete={() => onDelete(item)}
            onPreview={hasVideo(item) ? () => onPreview(item) : undefined}
            selected={selectedVideoId === item.video?.id}
            showPreview={false}
          />
        ))}
        {items.length === 0 && (
          <EmptyPanel
            icon={<Film className="h-5 w-5" />}
            title={emptyCopy.title}
            description={emptyCopy.description}
          />
        )}
        {hasNextPage && (
          <Button
            variant="outline"
            size="sm"
            className="w-full"
            loading={fetchingNextPage}
            onClick={onLoadMore}
            leftIcon={<RefreshCw className="h-3.5 w-3.5" />}
          >
            {fetchingNextPage ? "加载中" : "加载更早记录"}
          </Button>
        )}
      </div>
    </section>
  );
}

function VideoTaskDrawer({
  open,
  onClose,
  activeItems,
  historyItems,
  historyFilter,
  historyCounts,
  historyLoading,
  historyHasNextPage,
  historyFetchingNextPage,
  retryDisabled,
  selectedVideoId,
  onHistoryFilterChange,
  onRefresh,
  onLoadMore,
  onCancel,
  onRetry,
  onCopy,
  onUseDraft,
  onDelete,
  onPreview,
}: {
  open: boolean;
  onClose: () => void;
  activeItems: VideoGenerationOut[];
  historyItems: VideoGenerationOut[];
  historyFilter: VideoHistoryFilter;
  historyCounts: Record<VideoHistoryFilter, number>;
  historyLoading: boolean;
  historyHasNextPage: boolean;
  historyFetchingNextPage: boolean;
  retryDisabled: boolean;
  selectedVideoId: string;
  onHistoryFilterChange: (value: VideoHistoryFilter) => void;
  onRefresh: () => void;
  onLoadMore: () => void;
  onCancel: (item: VideoGenerationOut) => void;
  onRetry: (item: VideoGenerationOut) => void;
  onCopy: (item: VideoGenerationOut) => void;
  onUseDraft: (item: VideoGenerationOut) => void;
  onDelete: (item: VideoGenerationOut) => void;
  onPreview: (item: VideoGenerationOut) => void;
}) {
  const reduceMotion = useReducedMotion();
  const panelRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const previousFocus =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = Array.from(
        panelRef.current?.querySelectorAll<HTMLElement>(
          VIDEO_DRAWER_FOCUSABLE,
        ) ?? [],
      ).filter((element) => element.offsetParent !== null);
      if (focusable.length === 0) {
        event.preventDefault();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
      previousFocus?.focus();
    };
  }, [onClose, open]);

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          className="mobile-dialog-shell fixed inset-0 z-[var(--z-dialog)] flex justify-end bg-[var(--surface-scrim)] sm:p-3"
          initial={{ opacity: reduceMotion ? 1 : 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: reduceMotion ? 1 : 0 }}
          transition={{ duration: reduceMotion ? 0 : DURATION.quick }}
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) onClose();
          }}
        >
          <motion.section
            ref={panelRef}
            role="dialog"
            aria-modal="true"
            aria-labelledby="video-task-panel-title"
            className="mobile-dialog-panel ml-auto flex h-full w-full max-w-[460px] flex-col overflow-hidden rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)] text-[var(--fg-0)] shadow-[var(--shadow-3)]"
            initial={{ x: reduceMotion ? 0 : 36, opacity: reduceMotion ? 1 : 0 }}
            animate={{ x: 0, opacity: 1 }}
            exit={{ x: reduceMotion ? 0 : 36, opacity: reduceMotion ? 1 : 0 }}
            transition={{
              duration: reduceMotion ? 0 : DURATION.normal,
              ease: EASE.develop,
            }}
          >
            <header className="flex shrink-0 items-start justify-between gap-3 border-b border-[var(--border)] bg-[var(--bg-1)]/95 px-4 py-3.5">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <span className="flex h-8 w-8 items-center justify-center rounded-[var(--radius-control)] border border-[var(--accent-border)] bg-[var(--accent-soft)] text-[var(--accent)]">
                    <ListVideo className="h-4 w-4" />
                  </span>
                  <div>
                    <h2
                      id="video-task-panel-title"
                      className="text-sm font-semibold text-[var(--fg-0)]"
                    >
                      视频任务
                    </h2>
                    <p className="mt-0.5 text-xs text-[var(--fg-2)]">
                      {activeVideoTaskSummary(
                        activeItems.length,
                        historyCounts.all,
                      )}
                    </p>
                  </div>
                </div>
              </div>
              <div className="flex shrink-0 items-center gap-1">
                <IconButton
                  variant="ghost"
                  size="sm"
                  aria-label="刷新视频任务"
                  tooltip="刷新"
                  onClick={onRefresh}
                >
                  <RefreshCw className="h-4 w-4" />
                </IconButton>
                <IconButton
                  autoFocus
                  variant="ghost"
                  size="sm"
                  aria-label="关闭视频任务"
                  tooltip="关闭"
                  onClick={onClose}
                >
                  <X className="h-4 w-4" />
                </IconButton>
              </div>
            </header>

            <div className="mobile-dialog-scroll min-h-0 flex-1 space-y-5 overflow-y-auto p-3 sm:p-4">
              <ActiveVideoTaskSection
                items={activeItems}
                retryDisabled={retryDisabled}
                onCancel={onCancel}
                onRetry={onRetry}
                onCopy={onCopy}
                onUseDraft={onUseDraft}
              />
              <VideoTaskHistorySection
                items={historyItems}
                activeCount={activeItems.length}
                historyFilter={historyFilter}
                historyCounts={historyCounts}
                loading={historyLoading}
                hasNextPage={historyHasNextPage}
                fetchingNextPage={historyFetchingNextPage}
                retryDisabled={retryDisabled}
                selectedVideoId={selectedVideoId}
                onHistoryFilterChange={onHistoryFilterChange}
                onLoadMore={onLoadMore}
                onCancel={onCancel}
                onRetry={onRetry}
                onCopy={onCopy}
                onUseDraft={onUseDraft}
                onDelete={onDelete}
                onPreview={onPreview}
              />
            </div>
          </motion.section>
        </motion.div>
      )}
    </AnimatePresence>
  );
}

function SelectField({
  label,
  value,
  onChange,
  options,
  renderOption,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: string[];
  renderOption?: (value: string) => string;
}) {
  return (
    <label className="block min-w-0 space-y-1.5">
      {label && <span className="type-caption text-[var(--fg-2)]">{label}</span>}
      <select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="h-10 w-full min-w-0 truncate rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm text-[var(--fg-0)] outline-none transition-colors focus:border-[var(--accent)]/60"
      >
        {options.map((item) => (
          <option key={item || "auto"} value={item}>
            {renderOption ? renderOption(item) : item || "自动"}
          </option>
        ))}
      </select>
    </label>
  );
}

function VideoParameterPanel({
  className,
  selectedModel,
  modelOptions,
  durationS,
  durationOptions,
  resolution,
  resolutionOptions,
  aspectRatio,
  aspectRatioOptions,
  seed,
  generateAudio,
  estimate,
  canSubmit,
  reason,
  loading,
  sourceReady,
  onSubmit,
  onModelChange,
  onDurationChange,
  onResolutionChange,
  onAspectRatioChange,
  onSeedChange,
  onGenerateAudioChange,
}: {
  className?: string;
  selectedModel: string;
  modelOptions: string[];
  durationS: number;
  durationOptions: string[];
  resolution: string;
  resolutionOptions: string[];
  aspectRatio: string;
  aspectRatioOptions: string[];
  seed: string;
  generateAudio: boolean;
  estimate: { tokens: number; micro: number } | null;
  canSubmit: boolean;
  reason: string;
  loading: boolean;
  sourceReady: boolean;
  onSubmit: () => void;
  onModelChange: (value: string) => void;
  onDurationChange: (value: string) => void;
  onResolutionChange: (value: string) => void;
  onAspectRatioChange: (value: string) => void;
  onSeedChange: (value: string) => void;
  onGenerateAudioChange: (value: boolean) => void;
}) {
  return (
    <aside
      id="video-generation-settings"
      className={cn(
        "flex min-w-0 flex-col overflow-hidden rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-1)]/82 shadow-[var(--shadow-2)] backdrop-blur-xl",
        className,
      )}
    >
      <div className="flex shrink-0 items-start justify-between gap-3 border-b border-[var(--border-subtle)] bg-[var(--bg-1)]/90 p-3.5">
        <div className="flex min-w-0 items-center gap-2.5">
          <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-[var(--radius-control)] border border-[var(--accent-border)] bg-[var(--accent-soft)] text-[var(--accent)]">
            <Settings2 className="h-4 w-4" />
          </span>
          <div className="min-w-0">
            <p className="text-sm font-semibold text-[var(--fg-0)]">视频生成参数</p>
            <p className="mt-0.5 truncate text-xs text-[var(--fg-2)]">
              {selectedModel || "未选择模型"}
            </p>
          </div>
        </div>
        <span
          className={cn(
            "shrink-0 whitespace-nowrap rounded-full border px-2 py-1 text-xs",
            canSubmit
              ? "border-success-border bg-success-soft text-success"
              : sourceReady
                ? "border-[var(--border)] bg-[var(--bg-0)] text-[var(--fg-2)]"
                : "border-warning-border bg-warning-soft text-[var(--warning-fg)]",
          )}
        >
          {canSubmit ? "就绪" : sourceReady ? "草稿" : "缺素材"}
        </span>
      </div>

      <div className="min-w-0 flex-1 space-y-4 p-3.5">
        <section className="space-y-2.5">
          <div className="flex items-center justify-between gap-2">
            <p className="type-caption text-[var(--fg-2)]">模型</p>
            <span className="text-[11px] text-[var(--fg-2)]">
              自动匹配当前生成方式
            </span>
          </div>
          <SelectField
            label=""
            value={selectedModel}
            onChange={onModelChange}
            options={modelOptions}
          />
        </section>

        <section className="space-y-2.5">
          <p className="type-caption text-[var(--fg-2)]">画面与时长</p>
          <div className="grid min-w-0 grid-cols-2 gap-2">
            <SelectField
              label="分辨率"
              value={resolution}
              onChange={onResolutionChange}
              options={resolutionOptions}
            />
            <SelectField
              label="画面比例"
              value={aspectRatio}
              onChange={onAspectRatioChange}
              options={aspectRatioOptions}
            />
          </div>
          <SelectField
            label="视频时长"
            value={String(durationS)}
            onChange={onDurationChange}
            options={durationOptions}
            renderOption={(value) => formatDurationLabel(Number(value))}
          />
        </section>

        <label className="flex min-h-12 min-w-0 cursor-pointer items-center justify-between gap-4 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)]/72 px-3">
          <span className="min-w-0">
            <span className="block text-sm font-medium text-[var(--fg-0)]">
              生成音频
            </span>
            <span className="mt-0.5 block text-xs text-[var(--fg-2)]">
              同步生成环境声或对白
            </span>
          </span>
          <input
            type="checkbox"
            checked={generateAudio}
            onChange={(event) => onGenerateAudioChange(event.target.checked)}
            className="peer sr-only"
          />
          <span className="relative h-6 w-10 shrink-0 rounded-full border border-[var(--border-strong)] bg-[var(--bg-2)] transition-colors peer-checked:border-[var(--accent-border)] peer-checked:bg-[var(--accent)] peer-checked:[&>span]:translate-x-4">
            <span className="absolute left-0.5 top-0.5 h-5 w-5 rounded-full bg-[var(--fg-0)] shadow-[var(--shadow-1)] transition-transform" />
          </span>
        </label>

        <details className="group overflow-hidden rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/48">
          <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-3 py-2.5 text-xs font-medium text-[var(--fg-1)] transition-colors hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)]">
            <span>高级设置</span>
            <ChevronDown className="h-3.5 w-3.5 transition-transform group-open:rotate-180" />
          </summary>
          <div className="border-t border-[var(--border-subtle)] p-3">
            <label className="block min-w-0 space-y-1.5">
              <span className="type-caption text-[var(--fg-2)]">Seed</span>
              <input
                value={seed}
                onChange={(event) => onSeedChange(event.target.value)}
                inputMode="numeric"
                placeholder="留空为随机"
                className="h-10 w-full min-w-0 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 font-mono text-xs text-[var(--fg-0)] outline-none transition-colors focus:border-[var(--accent)]/60"
              />
            </label>
            <p className="mt-2 text-xs leading-5 text-[var(--fg-2)]">
              使用相同 Seed 可提高同一模型与参数下的结果可复现性。
            </p>
          </div>
        </details>
      </div>

      <div className="mt-auto shrink-0 border-t border-[var(--border)] bg-[var(--bg-1)]/94 p-3.5">
        <div className="mb-3 grid grid-cols-2 gap-2 rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/72 p-3">
          <div className="min-w-0">
            <p className="type-caption text-[var(--fg-2)]">预计预扣</p>
            <p className="mt-1 truncate text-lg font-semibold tabular-nums text-[var(--fg-0)]">
              {estimate ? formatRmb(estimate.micro / 1_000_000) : "-"}
            </p>
          </div>
          <div className="min-w-0 border-l border-[var(--border-subtle)] pl-3">
            <p className="type-caption text-[var(--fg-2)]">Token 上限</p>
            <p className="mt-1 truncate text-sm font-semibold tabular-nums text-[var(--fg-0)]">
              {estimate ? estimate.tokens.toLocaleString() : "-"}
            </p>
          </div>
        </div>
        <SubmitPanel
          canSubmit={canSubmit}
          reason={reason}
          loading={loading}
          onSubmit={onSubmit}
        />
      </div>
    </aside>
  );
}

function VideoWorkbenchHeader({
  mode,
  profile,
  audio,
  enabled,
  loading,
  activeCount,
  historyCount,
  serviceSummary,
  submitState,
  onOpenParameters,
  onOpenTasks,
}: {
  mode: string;
  profile: string;
  audio: boolean;
  enabled: boolean;
  loading: boolean;
  activeCount: number;
  historyCount: number;
  serviceSummary: string;
  submitState: string;
  onOpenParameters: () => void;
  onOpenTasks: () => void;
}) {
  const serviceValue = loading ? "读取中" : enabled ? "在线" : "离线";

  return (
    <section className="sticky top-0 z-30 -mx-1 flex shrink-0 flex-wrap items-center justify-between gap-3 border-b border-[var(--border)] bg-[var(--bg-0)]/92 px-1 pb-2.5 pt-1 backdrop-blur-xl">
      <div className="flex min-w-0 items-center gap-3">
        <span className="hidden h-10 w-10 shrink-0 items-center justify-center rounded-[var(--radius-card)] border border-[var(--accent-border)] bg-[var(--accent-soft)] text-[var(--accent)] shadow-[var(--shadow-1)] sm:flex">
          <Clapperboard className="h-5 w-5" />
        </span>
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <h1 className="text-xl font-semibold leading-tight tracking-normal text-[var(--fg-0)] sm:text-2xl">
              AI 视频
            </h1>
            <span
              className={cn(
                "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-medium",
                enabled
                  ? "border-success-border bg-success-soft text-success"
                  : "border-[var(--border)] bg-[var(--bg-1)] text-[var(--fg-2)]",
              )}
            >
              <span
                className={cn(
                  "h-1.5 w-1.5 rounded-full",
                  enabled ? "bg-[var(--success)]" : "bg-[var(--fg-3)]",
                )}
              />
              {serviceValue}
            </span>
          </div>
          <p className="mt-1 truncate text-xs text-[var(--fg-2)]">
            {loading ? "正在读取视频服务" : serviceSummary}
          </p>
        </div>
      </div>
      <div className="flex min-w-0 flex-1 items-center justify-end gap-2 sm:flex-none">
        <div className="hidden items-center gap-1.5 lg:flex">
          <span className="inline-flex items-center gap-1.5 rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-1)]/72 px-2.5 py-1.5 text-xs text-[var(--fg-1)]">
            <Film className="h-3.5 w-3.5 text-[var(--fg-2)]" />
            {mode}
          </span>
          <span className="max-w-[160px] truncate px-1 text-xs text-[var(--fg-2)]">
            {audio ? "含音频" : "无音频"} · {submitState}
          </span>
        </div>
        <Button
          variant="secondary"
          size="sm"
          onClick={onOpenParameters}
          leftIcon={<Settings2 className="h-4 w-4" />}
          className="shrink-0"
        >
          <span className="sm:hidden">参数</span>
          <span className="hidden sm:inline">参数 · {profile}</span>
        </Button>
        <Button
          variant={activeCount > 0 ? "secondary" : "outline"}
          size="sm"
          onClick={onOpenTasks}
          leftIcon={<ListVideo className="h-4 w-4" />}
          className="shrink-0"
        >
          {activeCount > 0
            ? `${activeCount} 进行中`
            : historyCount > 0
              ? `任务 ${historyCount}`
              : "任务"}
        </Button>
      </div>
    </section>
  );
}

function ModeCard({
  actionKey,
  selected,
  onSelect,
}: {
  actionKey: VideoAction;
  selected: boolean;
  onSelect: () => void;
}) {
  const copy = MODE_COPY[actionKey];
  const icon =
    actionKey === "t2v" ? (
      <Film className="h-4 w-4" />
    ) : actionKey === "i2v" ? (
      <ImageIcon className="h-4 w-4" />
    ) : (
      <VideoIcon className="h-4 w-4" />
    );
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      className={cn(
        "group flex min-h-12 min-w-0 items-center gap-2 rounded-[var(--radius-control)] border px-2.5 py-2 text-left transition-[background-color,border-color,color,box-shadow] duration-[var(--dur-normal)] sm:px-3",
        selected
          ? "border-[var(--accent-border)] bg-[var(--accent-soft)] text-[var(--fg-0)] shadow-[var(--shadow-1)]"
          : "border-transparent text-[var(--fg-1)] hover:border-[var(--border)] hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)]",
      )}
    >
      <span
        className={cn(
          "hidden h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-control)] border sm:flex",
          selected
            ? "border-[var(--accent-border)] bg-[var(--bg-0)] text-[var(--accent)]"
            : "border-[var(--border-subtle)] bg-[var(--bg-1)] text-[var(--fg-2)]",
        )}
      >
        {icon}
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-xs font-semibold text-[var(--fg-0)] sm:text-sm">
          {copy.title}
        </span>
        <span className="mt-0.5 hidden truncate text-[11px] text-[var(--fg-2)] md:block">
          {copy.eyebrow}
        </span>
      </span>
      {selected && (
        <CircleCheck className="h-4 w-4 shrink-0 text-[var(--accent)]" />
      )}
    </button>
  );
}

function promptEnhancePreviewCandidateId(
  candidates: PromptEnhanceCandidate[],
  previewCandidateId: string,
  selectedId: string,
): string {
  if (candidates.some((candidate) => candidate.id === previewCandidateId)) {
    return previewCandidateId;
  }
  if (candidates.some((candidate) => candidate.id === selectedId)) {
    return selectedId;
  }
  return candidates[0]?.id ?? "";
}

function promptEnhanceChooserSubtitle({
  loading,
  candidateCount,
  autoApplied,
}: {
  loading: boolean;
  candidateCount: number;
  autoApplied: boolean;
}): string {
  if (candidateCount > 1) {
    return autoApplied
      ? `${candidateCount} 个候选，已应用推荐版`
      : `${candidateCount} 个候选，未自动替换`;
  }
  if (loading) return "按火山视频结构补动作、运镜和参考一致性";
  return autoApplied ? "已应用到描述" : "已保留原描述";
}

function promptEnhanceActionLabel(action: PromptEnhanceAction): string {
  if (action === "light_refine") return "轻度优化";
  if (action === "direct_pass") return "直接优化";
  if (action === "ask_first") return "需要补充";
  if (action === "keep_original") return "建议保留原稿";
  if (action === "optional_vc") return "可选改写";
  return "完整改写";
}

function PromptEnhanceLoadingState({ preview }: { preview: string }) {
  return (
    <div className="space-y-3 p-3 sm:p-4">
      <div className="flex items-center justify-between gap-3">
        <div>
          <p className="text-sm font-medium text-[var(--fg-0)]">
            正在生成可比较版本
          </p>
          <p className="mt-0.5 text-xs text-[var(--fg-2)]">
            完成后可逐个预览，不会直接覆盖当前描述。
          </p>
        </div>
        <span className="shrink-0 rounded-full border border-[var(--accent-border)] bg-[var(--accent-soft)] px-2 py-1 text-[10px] font-medium text-[var(--accent)]">
          AI 整理中
        </span>
      </div>
      <div className="h-1 overflow-hidden rounded-full bg-[var(--bg-2)]">
        <div className="h-full w-1/2 animate-pulse rounded-full bg-[var(--accent)]" />
      </div>
      <div
        role="status"
        aria-live="polite"
        className="min-h-36 whitespace-pre-wrap break-words rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/72 p-3 text-sm leading-7 text-[var(--fg-1)]"
      >
        {preview || "等待模型返回优化方案..."}
      </div>
    </div>
  );
}

function PromptEnhanceCandidateCard({
  candidate,
  index,
  selected,
  previewing,
  onPreview,
}: {
  candidate: PromptEnhanceCandidate;
  index: number;
  selected: boolean;
  previewing: boolean;
  onPreview: () => void;
}) {
  return (
    <button
      type="button"
      aria-pressed={previewing}
      onClick={onPreview}
      className={cn(
        "flex min-h-32 w-[min(82vw,20rem)] shrink-0 flex-col rounded-[var(--radius-control)] border p-3 text-left transition-[background-color,border-color,box-shadow] lg:w-auto lg:min-w-0",
        selected
          ? "border-success-border bg-success-soft shadow-[var(--shadow-1)]"
          : previewing
            ? "border-[var(--accent-border)] bg-[var(--accent-soft)] shadow-[var(--shadow-1)]"
            : "border-[var(--border-subtle)] bg-[var(--bg-0)]/72 hover:border-[var(--border-strong)] hover:bg-[var(--bg-2)]",
      )}
    >
      <span className="flex min-w-0 items-center gap-2">
        <span
          className={cn(
            "flex h-7 w-7 shrink-0 items-center justify-center rounded-[var(--radius-control)] border font-mono text-[10px]",
            selected
              ? "border-success-border text-success"
              : previewing
                ? "border-[var(--accent-border)] text-[var(--accent)]"
                : "border-[var(--border)] text-[var(--fg-2)]",
          )}
        >
          {String(index + 1).padStart(2, "0")}
        </span>
        <span className="min-w-0 flex-1">
          <span className="flex min-w-0 items-center gap-1.5">
            <span className="min-w-0 flex-1 truncate text-sm font-semibold text-[var(--fg-0)]">
              {candidate.title}
            </span>
            {index === 0 && (
              <span className="shrink-0 rounded-full border border-[var(--accent-border)] bg-[var(--bg-0)] px-1.5 py-0.5 text-[10px] font-medium text-[var(--accent)]">
                推荐
              </span>
            )}
          </span>
          <span className="mt-0.5 block text-[11px] text-[var(--fg-2)]">
            {promptEnhanceActionLabel(candidate.action)}
          </span>
        </span>
        {selected && <CircleCheck className="h-4 w-4 shrink-0 text-success" />}
      </span>
      <span className="mt-2 line-clamp-2 text-xs leading-5 text-[var(--fg-1)]">
        {candidate.prompt}
      </span>
      <span className="mt-auto flex items-center justify-between gap-2 pt-2 text-[10px] text-[var(--fg-2)]">
        <span>{candidate.prompt.length.toLocaleString()} 字</span>
        <span className={selected ? "text-success" : previewing ? "text-[var(--accent)]" : ""}>
          {selected ? "已应用" : previewing ? "正在预览" : "查看方案"}
        </span>
      </span>
    </button>
  );
}

function PromptEnhanceCandidatePreview({
  candidate,
  selected,
  onApply,
  onCopy,
}: {
  candidate: PromptEnhanceCandidate;
  selected: boolean;
  onApply: () => void;
  onCopy: () => void;
}) {
  const applicable = canApplyPromptEnhanceCandidate(candidate);
  return (
    <article className="overflow-hidden rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)]">
      <header className="flex flex-wrap items-start justify-between gap-3 border-b border-[var(--border-subtle)] bg-[var(--bg-1)]/68 px-3 py-2.5 sm:px-4">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-sm font-semibold text-[var(--fg-0)]">
              {candidate.title}
            </h3>
            <span className="rounded-full border border-[var(--border)] bg-[var(--bg-0)] px-2 py-0.5 text-[10px] text-[var(--fg-2)]">
              {promptEnhanceActionLabel(candidate.action)}
            </span>
            {selected && (
              <span className="rounded-full border border-success-border bg-success-soft px-2 py-0.5 text-[10px] font-medium text-success">
                当前已应用
              </span>
            )}
          </div>
          <p className="mt-1 text-xs text-[var(--fg-2)]">
            {applicable
              ? "先完整预览，再决定是否替换编辑器中的描述。"
              : "这是 AI 的判断与补充建议，仅供查看。"}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          <Button
            variant={selected ? "secondary" : "primary"}
            size="sm"
            disabled={selected || !applicable}
            onClick={onApply}
          >
            {promptEnhanceCandidateButtonText(candidate, selected)}
          </Button>
          <IconButton
            variant="ghost"
            size="sm"
            onClick={onCopy}
            aria-label="复制优化提示词"
            tooltip="复制提示词"
          >
            <Copy className="h-3.5 w-3.5" />
          </IconButton>
        </div>
      </header>
      <div className="whitespace-pre-wrap break-words px-3 py-3 text-sm leading-7 text-[var(--fg-1)] sm:px-4 sm:py-4">
        {candidate.prompt}
      </div>
      <footer className="border-t border-[var(--border-subtle)] px-3 py-2 text-[10px] tabular-nums text-[var(--fg-2)] sm:px-4">
        完整提示词 · {candidate.prompt.length.toLocaleString()} 字
      </footer>
    </article>
  );
}

function PromptEnhanceChooser({
  loading,
  preview,
  candidates,
  selectedId,
  onSelect,
  onDismiss,
  onReturnToEditor,
}: {
  loading: boolean;
  preview: string;
  candidates: PromptEnhanceCandidate[];
  selectedId: string;
  onSelect: (candidate: PromptEnhanceCandidate) => void;
  onDismiss: () => void;
  onReturnToEditor: () => void;
}) {
  const cleanPreview = cleanPromptEnhanceText(preview);
  const visibleCandidates = candidates;
  const firstCandidate = visibleCandidates[0];
  const [previewCandidateId, setPreviewCandidateId] = useState("");
  const effectivePreviewCandidateId = promptEnhancePreviewCandidateId(
    visibleCandidates,
    previewCandidateId,
    selectedId,
  );
  const previewCandidate =
    visibleCandidates.find((candidate) => candidate.id === effectivePreviewCandidateId) ??
    firstCandidate ??
    null;
  const autoApplied =
    firstCandidate != null &&
    firstCandidate.id === selectedId &&
    shouldAutoApplyPromptEnhanceCandidate(firstCandidate);

  const copyCandidate = async (candidate: PromptEnhanceCandidate) => {
    try {
      await navigator.clipboard.writeText(candidate.prompt);
      toast.success("已复制提示词");
    } catch {
      toast.error("复制失败");
    }
  };

  return (
    <section className="overflow-hidden rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-1)]/88 shadow-[var(--shadow-2)] backdrop-blur-xl">
      <header className="flex flex-wrap items-center justify-between gap-2 border-b border-[var(--border-subtle)] px-3 py-2.5 sm:px-4">
        <div className="flex min-w-0 items-center gap-2">
          <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-control)] border border-[var(--accent-border)] bg-[var(--bg-0)] text-[var(--accent)]">
            {loading ? (
              <RefreshCw className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Sparkles className="h-3.5 w-3.5" />
            )}
          </span>
          <span className="min-w-0">
            <span className="block text-sm font-semibold text-[var(--fg-0)]">
              {loading ? "正在优化提示词" : "AI 优化结果"}
            </span>
            <span className="block truncate text-xs text-[var(--fg-2)]">
              {promptEnhanceChooserSubtitle({
                loading,
                candidateCount: visibleCandidates.length,
                autoApplied,
              })}
            </span>
          </span>
        </div>
        {!loading && (
          <div className="flex shrink-0 items-center gap-1">
            <Button
              variant="ghost"
              size="sm"
              onClick={onReturnToEditor}
              leftIcon={<PencilLine className="h-3.5 w-3.5" />}
            >
              回到编辑
            </Button>
            <IconButton
              variant="ghost"
              size="sm"
              onClick={onDismiss}
              aria-label="关闭优化结果"
              tooltip="关闭优化结果"
            >
              <X className="h-4 w-4" />
            </IconButton>
          </div>
        )}
      </header>

      {loading && <PromptEnhanceLoadingState preview={cleanPreview} />}

      {!loading && visibleCandidates.length > 0 && previewCandidate && (
        <div className="space-y-3 p-3 sm:p-4">
          <div className="flex items-center justify-between gap-3">
            <p className="text-xs font-medium text-[var(--fg-1)]">
              选择一个优化方向
            </p>
            <p className="text-[10px] text-[var(--fg-2)]">
              点击卡片切换完整预览
            </p>
          </div>
          <div className="flex gap-2 overflow-x-auto pb-1 lg:grid lg:grid-cols-3 lg:overflow-visible">
            {visibleCandidates.map((candidate, index) => (
              <PromptEnhanceCandidateCard
                key={candidate.id}
                candidate={candidate}
                index={index}
                selected={candidate.id === selectedId}
                previewing={candidate.id === previewCandidate.id}
                onPreview={() => setPreviewCandidateId(candidate.id)}
              />
            ))}
          </div>
          <PromptEnhanceCandidatePreview
            candidate={previewCandidate}
            selected={previewCandidate.id === selectedId}
            onApply={() => onSelect(previewCandidate)}
            onCopy={() => void copyCandidate(previewCandidate)}
          />
        </div>
      )}
    </section>
  );
}

function ReferenceChip({
  item,
  active,
  onInsert,
  onPreview,
  onRemove,
}: {
  item: ReferenceDraft;
  active: boolean;
  onInsert: () => void;
  onPreview: () => void;
  onRemove: () => void;
}) {
  const displayToken = referenceDisplayToken(item);
  const anchorToken = referencePromptToken(item);
  return (
    <div
      className={cn(
        "relative flex h-24 w-[min(82vw,19rem)] shrink-0 overflow-hidden rounded-[var(--radius-control)] border bg-[var(--bg-1)] text-xs text-[var(--fg-1)] transition-[background-color,border-color,box-shadow]",
        active
          ? "border-[var(--accent-border)] bg-[var(--accent-soft)] shadow-[var(--shadow-1)]"
          : "border-[var(--border)]",
      )}
    >
      <button
        type="button"
        onClick={onPreview}
        title={`查看 ${displayToken} 预览`}
        aria-label={`查看 ${displayToken} 预览`}
        className="shrink-0 cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]/50"
      >
        <ReferenceThumbnail item={item} active={active} />
      </button>
      <button
        type="button"
        onClick={onInsert}
        title={active ? `已引用 ${displayToken}，提交时映射为 ${anchorToken}` : `插入 ${displayToken}`}
        className="flex min-w-0 flex-1 cursor-pointer flex-col justify-center gap-1 px-3 py-2.5 pr-9 text-left transition-colors hover:bg-[var(--bg-2)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]/50"
      >
        <span className="flex min-w-0 items-center gap-2">
          <span className="shrink-0 font-semibold text-[var(--fg-0)]">{displayToken}</span>
          <span className="min-w-0 truncate text-[var(--fg-2)]">{item.label}</span>
        </span>
        <span className="max-w-full truncate font-mono text-[11px] text-[var(--fg-2)]">
          {item.display}
        </span>
        <span className="text-[11px] text-[var(--fg-2)]">
          {active ? "已用于提示词" : "点击文字插入引用"}
        </span>
      </button>
      <button
        type="button"
        aria-label="移除参考素材"
        onClick={onRemove}
        className="absolute right-1.5 top-1.5 shrink-0 rounded-full bg-[var(--bg-1)]/85 p-0.5 text-[var(--fg-2)] shadow-[var(--shadow-1)] transition-colors hover:bg-[var(--bg-3)] hover:text-[var(--fg-0)]"
      >
        <XCircle className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}

function ReferenceThumbnail({
  item,
  active,
}: {
  item: ReferenceDraft;
  active: boolean;
}) {
  const [failed, setFailed] = useState(false);
  const previewUrl = cleanReferencePreviewUrl(item.previewUrl);
  const showPreview = Boolean(previewUrl && !failed);
  const Icon = item.kind === "video" ? VideoIcon : item.url ? Tags : ImageIcon;

  return (
    <span className="relative flex h-24 w-32 shrink-0 overflow-hidden border-r border-[var(--border-subtle)] bg-[var(--bg-0)] text-[var(--fg-2)]">
      {showPreview ? (
        <img
          src={previewUrl ?? ""}
          alt=""
          className="h-full w-full object-cover"
          loading="lazy"
          decoding="async"
          onError={() => setFailed(true)}
        />
      ) : (
        <span className="flex h-full w-full flex-col items-center justify-center gap-1 px-2 text-center">
          <Icon className="h-5 w-5" aria-hidden="true" />
          <span className="text-[10px] font-medium leading-3">
            {failed ? "预览失败" : "暂无预览"}
          </span>
        </span>
      )}
      {showPreview && (
        <span className="absolute bottom-1.5 left-1.5 rounded-full border border-[var(--border-subtle)] bg-[var(--bg-0)]/82 p-1 text-[var(--fg-1)] shadow-[var(--shadow-1)]">
          <Maximize2 className="h-3 w-3" aria-hidden="true" />
        </span>
      )}
      {active && (
        <span className="absolute right-1.5 top-1.5 rounded-full border border-[var(--bg-1)] bg-[var(--accent)] p-0.5 text-[var(--accent-on)] shadow-[var(--shadow-1)]">
          <CircleCheck className="h-2.5 w-2.5" aria-hidden="true" />
        </span>
      )}
      {item.kind === "video" && showPreview && (
        <span className="absolute bottom-1.5 right-1.5 rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/85 p-0.5 text-[var(--fg-1)]">
          <VideoIcon className="h-2.5 w-2.5" aria-hidden="true" />
        </span>
      )}
    </span>
  );
}

function ReferenceMediaPreviewDialog({
  item,
  onClose,
  onInsert,
}: {
  item: ReferenceDraft;
  onClose: () => void;
  onInsert: () => void;
}) {
  const [failed, setFailed] = useState(false);
  const previewUrl = cleanReferencePreviewUrl(item.previewUrl);
  const displayToken = referenceDisplayToken(item);
  const Icon = item.kind === "video" ? VideoIcon : item.url ? Tags : ImageIcon;

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  return (
    <div
      className="mobile-dialog-shell mobile-perf-surface fixed inset-0 z-[var(--z-dialog)] flex items-end justify-center bg-black/70 backdrop-blur-md sm:items-center sm:p-5"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section
        role="dialog"
        aria-modal="true"
        aria-labelledby={`reference-preview-${item._key}`}
        className="mobile-dialog-panel flex h-[var(--mobile-dialog-max-height)] w-full max-w-4xl flex-col overflow-hidden rounded-t-[var(--radius-panel)] border border-b-0 border-[var(--border)] bg-[var(--bg-1)] text-[var(--fg-0)] shadow-[var(--shadow-3)] sm:h-[min(760px,calc(100dvh-2.5rem))] sm:rounded-[var(--radius-panel)] sm:border-b"
      >
        <header className="flex shrink-0 items-start justify-between gap-3 border-b border-[var(--border)] bg-[var(--bg-1)]/95 px-4 py-3 sm:px-5">
          <div className="min-w-0">
            <p className="type-caption text-[var(--fg-2)]">
              {item.kind === "video" ? "参考视频" : "参考图片"}
            </p>
            <h2
              id={`reference-preview-${item._key}`}
              className="mt-1 truncate text-base font-semibold text-[var(--fg-0)]"
            >
              {displayToken} · {item.label}
            </h2>
            <p className="mt-1 truncate font-mono text-xs text-[var(--fg-2)]">
              {item.display}
            </p>
          </div>
          <Button
            variant="ghost"
            size="sm"
            className="h-9 w-9 px-0"
            onClick={onClose}
            aria-label="关闭参考素材预览"
          >
            <XCircle className="h-4 w-4" />
          </Button>
        </header>
        <div className="min-h-0 flex-1 overflow-hidden bg-[var(--bg-0)] p-3 sm:p-5">
          <div className="flex h-full min-h-0 items-center justify-center overflow-hidden rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-1)] sm:min-h-[18rem]">
            {previewUrl && !failed ? (
              <img
                src={previewUrl}
                alt={`${displayToken} 预览`}
                className="h-full w-full object-contain"
                decoding="async"
                onError={() => setFailed(true)}
              />
            ) : (
              <div className="flex flex-col items-center justify-center gap-2 px-5 text-center text-[var(--fg-2)]">
                <Icon className="h-8 w-8" aria-hidden="true" />
                <p className="text-sm font-medium text-[var(--fg-1)]">
                  {failed ? "预览加载失败" : "这个素材暂无可显示预览"}
                </p>
                <p className="max-w-md text-xs leading-5">
                  官方 asset 素材可能只有素材 ID；上传图片会优先显示展示图。
                </p>
              </div>
            )}
          </div>
        </div>
        <footer className="mobile-dialog-footer flex shrink-0 flex-nowrap items-center justify-between gap-2 overflow-x-auto border-t border-[var(--border)] bg-[var(--bg-1)]/88 px-4 py-3 sm:px-5">
          <span className="shrink-0 text-xs text-[var(--fg-2)]">
            提交时映射为 {referencePromptToken(item)}
          </span>
          <div className="flex shrink-0 items-center gap-2">
            <Button variant="outline" size="sm" onClick={onClose}>
              关闭
            </Button>
            <Button
              variant="secondary"
              size="sm"
              onClick={onInsert}
              leftIcon={<Tags className="h-3.5 w-3.5" />}
            >
              插入引用
            </Button>
          </div>
        </footer>
      </section>
    </div>
  );
}

function SubmitPanel({
  canSubmit,
  reason,
  loading,
  onSubmit,
}: {
  canSubmit: boolean;
  reason: string;
  loading: boolean;
  onSubmit: () => void;
}) {
  return (
    <div className="space-y-2">
      <p
        className={cn(
          "flex min-w-0 items-center gap-2 text-xs leading-5",
          canSubmit ? "text-success" : "text-[var(--fg-2)]",
        )}
      >
        <span
          className={cn(
            "h-1.5 w-1.5 shrink-0 rounded-full",
            canSubmit ? "bg-[var(--success)]" : "bg-[var(--fg-3)]",
          )}
        />
        <span className="truncate">{reason}</span>
      </p>
      <Button
        variant="primary"
        size="lg"
        fullWidth
        disabled={!canSubmit}
        loading={loading}
        onClick={onSubmit}
        leftIcon={<Send className="h-4 w-4" />}
      >
        生成视频
      </Button>
    </div>
  );
}

function EmptyPanel({
  icon,
  title,
  description,
}: {
  icon: React.ReactNode;
  title: string;
  description: string;
}) {
  return (
    <div className="flex min-h-[132px] flex-col items-center justify-center rounded-[var(--radius-card)] border border-dashed border-[var(--border)] bg-[var(--bg-0)]/60 p-6 text-center">
      <div className="mb-3 flex h-10 w-10 items-center justify-center rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-1)] text-[var(--fg-2)]">
        {icon}
      </div>
      <p className="text-sm font-medium text-[var(--fg-0)]">{title}</p>
      <p className="mt-1 max-w-sm text-xs leading-5 text-[var(--fg-2)]">{description}</p>
    </div>
  );
}

function VideoDownloadLink({
  item,
  fullWidth = false,
}: {
  item: VideoGenerationOut;
  fullWidth?: boolean;
}) {
  const temporaryDownload = activeTemporaryDownload(item);
  const stableHref = hasVideo(item) ? videoDownloadUrl(item.video.id) : "";
  const href = temporaryDownload?.url || stableHref;
  if (!href) return null;
  const isTemporary = temporaryDownload != null;
  const expiresTitle =
    isTemporary
      ? `火山临时链接，约 ${Math.max(1, Math.floor(temporaryDownload.expires_in_s / 60))} 分钟后过期`
      : undefined;
  return (
    <a
      href={href}
      download={isTemporary ? undefined : videoDownloadName(item)}
      target={isTemporary ? "_blank" : undefined}
      rel={isTemporary ? "noopener noreferrer" : undefined}
      title={expiresTitle}
      className={cn(
        "inline-flex h-9 items-center justify-center gap-1.5 rounded-[var(--radius-control)] border border-[var(--border)] bg-transparent px-3 text-xs font-medium leading-tight text-[var(--fg-0)] transition-[background-color,border-color,color] hover:border-[var(--border-strong)] hover:bg-[var(--bg-2)]",
        fullWidth && "w-full",
      )}
    >
      <Download className="h-3.5 w-3.5 shrink-0" />
      {isTemporary ? "快速下载" : "下载"}
    </a>
  );
}

function VideoPosterButton({
  item,
  onPreview,
  selected = false,
  compact = false,
}: {
  item: VideoGenerationWithVideo;
  onPreview: () => void;
  selected?: boolean;
  compact?: boolean;
}) {
  const [posterFailure, setPosterFailure] = useState<{
    videoId: string;
    failed: boolean;
  } | null>(null);
  const poster = posterSrc(item.video);
  const videoUrl = videoSrc(item.video);
  const posterFailed =
    posterFailure?.videoId === item.video.id ? posterFailure.failed : false;
  const prewarmPreview = useCallback(() => {
    prewarmImage(poster);
    prewarmVideoMetadata(videoUrl);
  }, [poster, videoUrl]);
  const handlePreview = useCallback(() => {
    prewarmPreview();
    onPreview();
  }, [onPreview, prewarmPreview]);

  useEffect(() => {
    if (selected) prewarmPreview();
  }, [prewarmPreview, selected]);

  return (
    <button
      type="button"
      onClick={handlePreview}
      onFocus={prewarmPreview}
      onPointerDown={prewarmPreview}
      onPointerEnter={prewarmPreview}
      aria-pressed={selected}
      className={cn(
        "group relative w-full overflow-hidden rounded-[var(--radius-control)] border bg-[var(--bg-0)] text-left transition-colors",
        compact ? "aspect-video" : "mt-3 aspect-video",
        selected
          ? "border-[var(--accent-border)] shadow-[var(--shadow-1)]"
          : "border-[var(--border-subtle)] hover:border-[var(--border)]",
      )}
    >
      {poster && !posterFailed ? (
        <img
          src={poster}
          alt=""
          loading={selected ? "eager" : "lazy"}
          decoding="async"
          fetchPriority={selected ? "high" : "low"}
          onError={() =>
            setPosterFailure({ videoId: item.video.id, failed: true })
          }
          className="h-full w-full object-contain"
        />
      ) : (
        <div className="grid h-full place-items-center text-[var(--fg-2)]">
          <Film className="h-6 w-6" />
        </div>
      )}
      <span className="absolute inset-0 flex items-center justify-center bg-black/0 transition-colors group-hover:bg-black/20">
        <span className="inline-flex items-center gap-1.5 rounded-full border border-[var(--border)] bg-[var(--fg-0)]/85 px-3 py-1.5 text-xs font-medium text-[var(--bg-0)] shadow-[var(--shadow-2)]">
          <Play className="h-3.5 w-3.5" />
          播放预览
        </span>
      </span>
    </button>
  );
}

type VideoPlayerStatus = "loading" | "metadata" | "ready" | "buffering" | "error";

function videoPlayerStatusLabel(status: VideoPlayerStatus): string {
  switch (status) {
    case "loading":
      return "读取视频";
    case "metadata":
      return "准备播放";
    case "buffering":
      return "缓冲中";
    case "error":
      return "载入失败";
    default:
      return "";
  }
}

function PrimaryVideoPlayer({
  item,
  className,
}: {
  item: VideoGenerationWithVideo;
  className?: string;
}) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const [statusState, setStatusState] = useState<{
    videoId: string;
    status: VideoPlayerStatus;
  }>(() => ({ videoId: item.video.id, status: "loading" }));
  const poster = posterSrc(item.video);
  const src = videoSrc(item.video);
  const status =
    statusState.videoId === item.video.id ? statusState.status : "loading";
  const setVideoStatus = useCallback(
    (next: VideoPlayerStatus) =>
      setStatusState({ videoId: item.video.id, status: next }),
    [item.video.id],
  );

  useEffect(() => {
    prewarmImage(poster);
    prewarmVideoMetadata(src);
  }, [poster, src]);

  const retryLoad = useCallback(() => {
    setVideoStatus("loading");
    prewarmImage(poster);
    prewarmVideoMetadata(src);
    videoRef.current?.load();
  }, [poster, setVideoStatus, src]);

  const showState =
    status === "loading" || status === "buffering" || status === "error";

  return (
    <div
      className={cn(
        "relative flex min-h-0 overflow-hidden rounded-[var(--radius-panel)] border border-[var(--border-strong)] bg-[var(--bg-2)] shadow-[var(--shadow-2)]",
        className,
      )}
    >
      <video
        key={item.video.id}
        ref={videoRef}
        controls
        playsInline
        preload="metadata"
        poster={poster}
        src={src}
        onLoadStart={() => setVideoStatus("loading")}
        onLoadedMetadata={() => setVideoStatus("metadata")}
        onCanPlay={() => setVideoStatus("ready")}
        onPlaying={() => setVideoStatus("ready")}
        onWaiting={() => setVideoStatus("buffering")}
        onError={() => setVideoStatus("error")}
        className="h-full min-h-0 w-full bg-[var(--bg-2)] object-contain"
      />
      {showState && (
        <div
          className={cn(
            "absolute inset-0 flex items-center justify-center bg-[var(--bg-1)]/70 text-[var(--fg-0)]",
            status !== "error" && "pointer-events-none",
          )}
        >
          <div
            role={status === "error" ? "alert" : "status"}
            aria-live={status === "error" ? "assertive" : "polite"}
            className="inline-flex items-center gap-2 rounded-full border border-[var(--border-strong)] bg-[var(--bg-0)]/90 px-3 py-1.5 text-xs font-medium text-[var(--fg-0)] shadow-[var(--shadow-2)] backdrop-blur-md"
          >
            {status === "error" ? (
              <button
                type="button"
                onClick={retryLoad}
                className="inline-flex cursor-pointer items-center gap-1.5 text-[var(--fg-0)]"
              >
                <RefreshCw className="h-3.5 w-3.5" />
                重试
              </button>
            ) : (
              <>
                <RefreshCw className="h-3.5 w-3.5 animate-spin" />
                {videoPlayerStatusLabel(status)}
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function VideoPreviewDialog({
  item,
  onClose,
  onUseDraft,
  onRetry,
  onCopy,
  onDelete,
}: {
  item: VideoGenerationWithVideo;
  onClose: () => void;
  onUseDraft: () => void;
  onRetry: () => void;
  onCopy: () => void;
  onDelete: () => void;
}) {
  const elapsedLabel = taskElapsedLabel(item);
  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  return (
    <div
      className="mobile-dialog-shell mobile-perf-surface fixed inset-0 z-[var(--z-dialog)] flex items-end justify-center bg-black/70 backdrop-blur-md sm:items-center sm:p-5"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section
        role="dialog"
        aria-modal="true"
        aria-labelledby={`video-preview-${item.id}`}
        className="mobile-dialog-panel flex h-[var(--mobile-dialog-max-height)] w-full max-w-6xl flex-col overflow-hidden rounded-t-[var(--radius-panel)] border border-b-0 border-[var(--border)] bg-[var(--bg-1)] text-[var(--fg-0)] shadow-[var(--shadow-3)] sm:h-[min(900px,calc(100dvh-2.5rem))] sm:rounded-[var(--radius-panel)] sm:border-b"
      >
        <header className="flex shrink-0 items-start justify-between gap-3 border-b border-[var(--border)] bg-[var(--bg-1)]/95 px-4 py-3 sm:px-5">
          <div className="min-w-0">
            <div className="mb-2 flex flex-wrap gap-2">
              <StatusPill item={item} />
              <span className="rounded-full border border-[var(--border)] bg-[var(--bg-0)] px-2 py-1 text-xs text-[var(--fg-2)]">
                {actionLabel(item.action)} · {item.resolution} · {formatDurationLabel(item.duration_s)}
              </span>
              {elapsedLabel && (
                <span className="rounded-full border border-[var(--border)] bg-[var(--bg-0)] px-2 py-1 text-xs text-[var(--fg-2)]">
                  {elapsedLabel}
                </span>
              )}
            </div>
            <h2
              id={`video-preview-${item.id}`}
              className="truncate text-base font-semibold text-[var(--fg-0)]"
            >
              视频播放
            </h2>
          </div>
          <Button
            variant="ghost"
            size="sm"
            className="h-9 w-9 px-0"
            onClick={onClose}
            aria-label="关闭视频播放"
          >
            <XCircle className="h-4 w-4" />
          </Button>
        </header>
        <div className="min-h-0 flex-1 overflow-hidden p-3 sm:p-5">
          <div className="flex h-full min-h-0 flex-col gap-3 lg:grid lg:grid-cols-[minmax(0,1fr)_minmax(280px,340px)]">
            <div className="min-h-0 flex-1 lg:h-full">
              <PrimaryVideoPlayer item={item} className="h-full" />
            </div>
            <aside className="max-h-[34%] shrink-0 overflow-y-auto rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)]/64 p-3 shadow-[var(--shadow-1)] lg:h-full lg:max-h-none">
              <p className="type-caption text-[var(--fg-2)]">提示词</p>
              <p className="mt-2 text-sm leading-6 text-[var(--fg-0)]">
                {item.prompt}
              </p>
              <div className="mt-3 flex flex-wrap gap-1.5 text-xs text-[var(--fg-2)]">
                <span className="rounded-full border border-[var(--border)] bg-[var(--bg-1)] px-2 py-1">
                  {item.video.width}x{item.video.height}
                </span>
                <span className="rounded-full border border-[var(--border)] bg-[var(--bg-1)] px-2 py-1">
                  {formatDurationLabel(item.duration_s)}
                </span>
                {elapsedLabel && (
                  <span className="rounded-full border border-[var(--border)] bg-[var(--bg-1)] px-2 py-1">
                    {elapsedLabel}
                  </span>
                )}
                <span className="rounded-full border border-[var(--border)] bg-[var(--bg-1)] px-2 py-1">
                  {item.video.has_audio ? "含音频" : "无音频"}
                </span>
              </div>
            </aside>
          </div>
        </div>
        <footer className="mobile-dialog-footer flex shrink-0 flex-nowrap items-center gap-2 overflow-x-auto border-t border-[var(--border)] bg-[var(--bg-1)]/88 px-4 py-3 sm:flex-wrap sm:justify-between sm:overflow-visible sm:px-5">
          <VideoDownloadLink item={item} />
          <div className="flex shrink-0 flex-nowrap items-center gap-2 sm:flex-wrap">
            <Button
              variant="secondary"
              size="sm"
              onClick={onUseDraft}
              leftIcon={<RotateCw className="h-3.5 w-3.5" />}
            >
              套用参数
            </Button>
            {isFailedHistoryVideo(item) && (
              <Button
                variant="outline"
                size="sm"
                onClick={onRetry}
                leftIcon={<Play className="h-3.5 w-3.5" />}
              >
                重新生成
              </Button>
            )}
            <Button
              variant="outline"
              size="sm"
              onClick={onCopy}
              leftIcon={<Copy className="h-3.5 w-3.5" />}
            >
              复制
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={onDelete}
              leftIcon={<Trash2 className="h-3.5 w-3.5" />}
            >
              删除
            </Button>
          </div>
        </footer>
      </section>
    </div>
  );
}

function HistoryFilterTabs({
  value,
  counts,
  loading,
  onChange,
}: {
  value: VideoHistoryFilter;
  counts: Record<VideoHistoryFilter, number>;
  loading: boolean;
  onChange: (value: VideoHistoryFilter) => void;
}) {
  const filters: Array<{ value: VideoHistoryFilter; label: string }> = [
    { value: "all", label: "全部" },
    { value: "succeeded", label: "成功" },
    { value: "failed", label: "失败" },
  ];

  return (
    <div className="grid grid-cols-3 gap-1 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)] p-1">
      {filters.map((filter) => {
        const active = filter.value === value;
        return (
          <button
            key={filter.value}
            type="button"
            onClick={() => onChange(filter.value)}
            className={cn(
              "min-h-8 rounded-[var(--radius-control)] px-2 text-xs transition-colors",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]",
              active
                ? "bg-[var(--bg-2)] text-[var(--fg-0)] shadow-[var(--shadow-1)]"
                : "text-[var(--fg-2)] hover:bg-[var(--bg-1)] hover:text-[var(--fg-1)]",
            )}
          >
            <span className="inline-flex min-w-0 items-center justify-center gap-1.5">
              <span>{filter.label}</span>
              <span className="rounded-full border border-[var(--border)] px-1.5 py-0.5 font-mono text-[10px] tabular-nums">
                {loading ? "..." : counts[filter.value]}
              </span>
            </span>
          </button>
        );
      })}
    </div>
  );
}

function TaskErrorDetails({
  raw,
  summary,
}: {
  raw: string;
  summary: string;
}) {
  return (
    <details className="group mt-2 overflow-hidden rounded-[var(--radius-control)] border border-danger-border bg-danger-soft">
      <summary className="flex cursor-pointer list-none items-start gap-2 px-2.5 py-2 text-xs leading-5 text-[var(--danger-fg)]">
        <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
        <span className="min-w-0 flex-1">{summary}</span>
        <ChevronDown className="mt-0.5 h-3.5 w-3.5 shrink-0 transition-transform group-open:rotate-180" />
      </summary>
      <div className="border-t border-danger-border px-2.5 py-2">
        <p className="type-caption text-[var(--danger-fg)]">技术详情</p>
        <pre className="mt-1.5 max-h-36 overflow-auto whitespace-pre-wrap break-all rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-0)] p-2 font-mono text-[10px] leading-4 text-[var(--fg-1)]">
          {raw}
        </pre>
      </div>
    </details>
  );
}

function TaskRowActions({
  item,
  active,
  retryable,
  retryDisabled,
  videoItem,
  selected,
  showPreview,
  canDownload,
  onCancel,
  onRetry,
  onCopy,
  onUseDraft,
  onDelete,
  onPreview,
}: {
  item: VideoGenerationOut;
  active: boolean;
  retryable: boolean;
  retryDisabled: boolean;
  videoItem: VideoGenerationWithVideo | null;
  selected: boolean;
  showPreview: boolean;
  canDownload: boolean;
  onCancel: () => void;
  onRetry: () => void;
  onCopy: () => void;
  onUseDraft?: () => void;
  onDelete?: () => void;
  onPreview?: () => void;
}) {
  return (
    <div className="mt-3 flex flex-wrap items-center gap-2">
      {active && (
        <Button
          variant="outline"
          size="sm"
          onClick={onCancel}
          leftIcon={<XCircle className="h-3.5 w-3.5" />}
        >
          取消
        </Button>
      )}
      {retryable && (
        <Button
          variant="outline"
          size="sm"
          disabled={retryDisabled}
          loading={retryDisabled}
          onClick={onRetry}
          leftIcon={<Play className="h-3.5 w-3.5" />}
        >
          重新生成
        </Button>
      )}
      {!showPreview && videoItem && onPreview && (
        <Button
          variant={selected ? "secondary" : "outline"}
          size="sm"
          onClick={onPreview}
          leftIcon={<Play className="h-3.5 w-3.5" />}
        >
          预览
        </Button>
      )}
      {canDownload && <VideoDownloadLink item={item} />}
      {onUseDraft && (
        <Button
          variant="outline"
          size="sm"
          onClick={onUseDraft}
          leftIcon={<RotateCw className="h-3.5 w-3.5" />}
        >
          套用参数
        </Button>
      )}
      <div className="ml-auto flex items-center gap-1">
        <IconButton
          variant="ghost"
          size="sm"
          onClick={onCopy}
          aria-label="复制视频描述"
          tooltip="复制描述"
        >
          <Copy className="h-3.5 w-3.5" />
        </IconButton>
        {onDelete && videoItem && (
          <IconButton
            variant="ghost"
            size="sm"
            onClick={onDelete}
            aria-label="删除视频"
            tooltip="删除"
            className="text-[var(--danger-fg)] hover:text-[var(--danger-fg)]"
          >
            <Trash2 className="h-3.5 w-3.5" />
          </IconButton>
        )}
      </div>
    </div>
  );
}

function TaskRow({
  item,
  onCancel,
  onRetry,
  retryDisabled = false,
  onCopy,
  onUseDraft,
  onDelete,
  onPreview,
  selected = false,
  showPreview = true,
}: {
  item: VideoGenerationOut;
  onCancel: () => void;
  onRetry: () => void;
  retryDisabled?: boolean;
  onCopy: () => void;
  onUseDraft?: () => void;
  onDelete?: () => void;
  onPreview?: () => void;
  selected?: boolean;
  showPreview?: boolean;
}) {
  const active = isActiveVideo(item);
  const progress = progressForItem(item);
  const progressScale = Math.max(0, Math.min(1, progress / 100));
  const reduceMotion = useReducedMotion();
  const copy = stageCopy(item);
  const videoItem = hasVideo(item) ? item : null;
  const retryable = isFailedHistoryVideo(item);
  const canDownload = videoItem != null || activeTemporaryDownload(item) != null;
  const elapsedLabel = taskElapsedLabel(item);
  const errorSummary = item.error_message
    ? taskErrorSummary(item.error_message)
    : null;
  return (
    <article
      className={cn(
        "relative overflow-hidden rounded-[var(--radius-card)] border p-3 transition-colors hover:border-[var(--border)]",
        active || selected
          ? "border-[var(--accent-border)] bg-[var(--accent-soft)] shadow-[var(--shadow-1)]"
          : "border-[var(--border-subtle)] bg-[var(--bg-0)]/60",
      )}
    >
      {(active || selected) && (
        <span aria-hidden="true" className="absolute inset-y-3 left-0 w-1 rounded-r-full bg-[var(--accent)]" />
      )}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2 text-xs text-[var(--fg-2)]">
            <span className="font-medium text-[var(--fg-1)]">{item.model}</span>
            <span>{actionLabel(item.action)}</span>
            <span>{item.resolution}</span>
            <span>{formatDurationLabel(item.duration_s)}</span>
            {elapsedLabel && <span>{elapsedLabel}</span>}
          </div>
          <p className="mt-1 line-clamp-2 text-sm text-[var(--fg-0)]">{item.prompt}</p>
          <p className="mt-1 text-xs leading-5 text-[var(--fg-2)]">{copy.detail}</p>
        </div>
        <StatusPill item={item} />
      </div>
      <div className="mt-3 h-2 overflow-hidden rounded-full bg-[var(--bg-2)]">
        <motion.div
          className={cn(
            "h-full w-full origin-left rounded-full",
            active ? "bg-[var(--accent)]" : item.status === "succeeded" ? "bg-[var(--success)]" : "bg-[var(--fg-3)]",
          )}
          initial={false}
          animate={{ scaleX: progressScale }}
          transition={{ duration: reduceMotion ? 0 : DURATION.normal, ease: EASE.develop }}
        />
      </div>
      {showPreview && videoItem && onPreview && (
        <VideoPosterButton
          item={videoItem}
          selected={selected}
          onPreview={onPreview}
        />
      )}
      {item.error_message && errorSummary && (
        <TaskErrorDetails raw={item.error_message} summary={errorSummary} />
      )}
      <TaskRowActions
        item={item}
        active={active}
        retryable={retryable}
        retryDisabled={retryDisabled}
        videoItem={videoItem}
        selected={selected}
        showPreview={showPreview}
        canDownload={canDownload}
        onCancel={onCancel}
        onRetry={onRetry}
        onCopy={onCopy}
        onUseDraft={onUseDraft}
        onDelete={onDelete}
        onPreview={onPreview}
      />
    </article>
  );
}

function StatusPill({ item }: { item: VideoGenerationOut }) {
  const terminalOk = item.status === "succeeded";
  const terminalBad = ["failed", "canceled", "expired"].includes(item.status);
  const copy = stageCopy(item);
  return (
    <span
      className={[
        "rounded-full border px-2 py-1 text-xs",
        terminalOk
          ? "border-success-border bg-success-soft text-success"
          : terminalBad
          ? "border-danger-border bg-danger-soft text-danger"
          : "border-[var(--border)] bg-[var(--bg-2)] text-[var(--fg-1)]",
      ].join(" ")}
    >
      {copy.label} · {Math.round(progressForItem(item))}%
    </span>
  );
}
