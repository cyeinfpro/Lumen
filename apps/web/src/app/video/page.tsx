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
  Clapperboard,
  CircleCheck,
  Copy,
  Download,
  Film,
  Gauge,
  ImageIcon,
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
  XCircle,
} from "lucide-react";
import { motion } from "framer-motion";

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
import type {
  VideoAction,
  VideoCreateIn,
  VideoGenerationOut,
  VideoOptionsOut,
  VideoReferenceMediaIn,
} from "@/lib/types";
import { Button, Card, toast } from "@/components/ui/primitives";
import { DesktopTopNav, MobileTabBar } from "@/components/ui/shell";
import { formatRmb } from "@/lib/money";
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
const ACTIVE_VIDEO_STATUSES = ["queued", "submitting", "submitted", "running"] as const;
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
const VIDEO_PROMPT_VARIANT_TITLES = [
  "推荐镜头版",
  "动作节奏版",
  "参考一致版",
];
const REFERENCE_REF_ID_RE = /^ref:(image|video):([1-9][0-9]{0,2})$/;
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

function isTerminalVideoStatus(status: string | undefined): boolean {
  return TERMINAL_VIDEO_STATUSES.includes(
    status as (typeof TERMINAL_VIDEO_STATUSES)[number],
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

function durationOptionsForModel(
  options: VideoOptionsOut | undefined,
  model: string,
  action: VideoAction,
  resolution: string,
): number[] {
  const modelOptions = options?.models.find((item) => item.model === model);
  const actionResolutionDurations =
    modelOptions?.durations_by_action_resolution?.[action]?.[resolution];
  if (actionResolutionDurations?.length) return actionResolutionDurations;
  const actionDurations = modelOptions?.durations_by_action?.[action];
  if (actionDurations?.length) return actionDurations;
  if (modelOptions?.durations_s?.length) return modelOptions.durations_s;
  return options?.durations_s?.length ? options.durations_s : VIDEO_DURATION_OPTIONS;
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

function mergeById(
  current: VideoGenerationOut[],
  updates: VideoGenerationOut[],
): VideoGenerationOut[] {
  const map = new Map(current.map((item) => [item.id, item]));
  for (const item of updates) map.set(item.id, item);
  return Array.from(map.values()).sort(
    (a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
  );
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
  const estimateActions =
    action === "reference"
      ? referenceHasVideo
        ? ["reference_video"]
        : ["reference_image", "reference", "i2v", "t2v"]
      : [action];
  const estimateKey = `${resolution}:${holdEstimateDurationS(durationS)}`;
  let tokensRaw: unknown;
  for (const modelCandidate of modelCandidates) {
    const tokenMap = options?.hold_estimates?.[modelCandidate];
    if (!tokenMap || typeof tokenMap !== "object") continue;
    const tokenRecord = tokenMap as Record<string, unknown>;
    for (const estimateAction of estimateActions) {
      const actionMap = tokenRecord[estimateAction];
      if (!actionMap || typeof actionMap !== "object") continue;
      tokensRaw = (actionMap as Record<string, unknown>)[estimateKey];
      if (tokensRaw != null) break;
    }
    if (tokensRaw != null) break;
  }
  const tokens = Number(tokensRaw);
  if (!Number.isFinite(tokens) || tokens <= 0) return null;
  const pricingAction =
    action === "reference"
      ? referenceHasVideo
        ? "reference_video"
        : "reference_image"
      : action;
  const findPrice = (priceAction: VideoAction | "reference_image" | "reference_video") => {
    for (const modelCandidate of modelCandidates) {
      const price =
        options?.pricing.find(
          (item) =>
            item.model === modelCandidate &&
            item.action === priceAction &&
            item.resolution === resolution &&
            item.enabled,
        ) ??
        options?.pricing.find(
          (item) =>
            item.model === modelCandidate &&
            item.action === priceAction &&
            (item.resolution == null || item.resolution === "") &&
            item.enabled,
        );
      if (price) return price;
    }
    return undefined;
  };
  const price =
    findPrice(pricingAction) ??
    (action === "reference" ? findPrice("reference") : undefined) ??
    (action === "reference" && !referenceHasVideo ? findPrice("i2v") : undefined);
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
  if (selected) return "已用";
  return "使用";
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

function referenceRefId(kind: "image" | "video", index: number): string {
  return `ref:${kind}:${index}`;
}

function referenceRefIndex(
  refId: string | null | undefined,
  kind: "image" | "video",
): number | null {
  const match = (refId ?? "").trim().toLowerCase().match(REFERENCE_REF_ID_RE);
  if (!match || match[1] !== kind) return null;
  const index = Number(match[2]);
  return Number.isInteger(index) && index > 0 ? index : null;
}

function referenceLabel(kind: "image" | "video", index: number): string {
  return `${kind === "image" ? "图片" : "视频"} ${index}`;
}

function nextReferenceIdentity(
  kind: "image" | "video",
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
  return `@${item.kind === "image" ? "图片" : "视频"}${index}`;
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function referenceDisplayAliases(item: ReferenceDraft): string[] {
  const index = referenceRefIndex(item.ref_id, item.kind);
  if (!index) return [];
  const noun = item.kind === "image" ? "图片" : "视频";
  const shortNoun = item.kind === "image" ? "图" : "视频";
  return [
    referenceDisplayToken(item),
    `@${noun} ${index}`,
    `@${shortNoun}${index}`,
    `@${shortNoun} ${index}`,
  ];
}

function referenceMentionAliases(item: ReferenceDraft): string[] {
  const index = referenceRefIndex(item.ref_id, item.kind);
  if (!index) return [];
  const aliases = new Set<string>();
  const noun = item.kind === "image" ? "图片" : "视频";
  const shortNoun = item.kind === "image" ? "图" : "视频";
  const zh = CHINESE_DIGITS[index];
  const videoRoleAliases =
    item.kind === "video"
      ? [
          `视频素材 ${index}`,
          `视频素材${index}`,
          `参考视频 ${index}`,
          `参考视频${index}`,
          `动作参考 ${index}`,
          `动作参考${index}`,
          `运动参考 ${index}`,
          `运动参考${index}`,
        ]
      : [];
  for (const alias of [
    item.label,
    `[${item.label}]`,
    `${noun} ${index}`,
    `${noun}${index}`,
    `${shortNoun}${index}`,
    ...videoRoleAliases,
    item.kind === "image" ? `第${index}张${noun}` : `第${index}个${noun}`,
    item.kind === "image" ? `第${index}张${shortNoun}` : `第${index}段${noun}`,
    item.kind === "video" ? `第${index}段素材` : "",
    item.kind === "video" ? `第${index}个视频素材` : "",
    zh && item.kind === "image" ? `第${zh}张${noun}` : "",
    zh && item.kind === "image" ? `第${zh}张${shortNoun}` : "",
    zh && item.kind === "video" ? `第${zh}个${noun}` : "",
    zh && item.kind === "video" ? `第${zh}段${noun}` : "",
    zh && item.kind === "video" ? `第${zh}段素材` : "",
    zh && item.kind === "video" ? `第${zh}个视频素材` : "",
  ]) {
    if (typeof alias !== "string") continue;
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
  const promptEnhancePanelVisible =
    isEnhancingPrompt ||
    Boolean(promptEnhancePreview.trim()) ||
    promptEnhanceCandidates.length > 0;

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
      if (typeof data !== "object" || data === null) return null;
      const raw = data as {
        video_generation_id?: unknown;
        status?: unknown;
        stage?: unknown;
        progress_pct?: unknown;
        error_code?: unknown;
      };
      const id =
        typeof raw.video_generation_id === "string" ? raw.video_generation_id : "";
      if (!id) return null;

      const status = typeof raw.status === "string" ? raw.status : undefined;
      const stage = typeof raw.stage === "string" ? raw.stage : undefined;
      const progressPct =
        typeof raw.progress_pct === "number" ? raw.progress_pct : undefined;
      const errorCode =
        typeof raw.error_code === "string" ? raw.error_code : undefined;

      if (status || stage || progressPct !== undefined || errorCode) {
        setItems((prev) =>
          prev.map((item) =>
            item.id === id
              ? {
                  ...item,
                  ...(status
                    ? { status: status as VideoGenerationOut["status"] }
                    : {}),
                  ...(stage
                    ? {
                        progress_stage:
                          stage as VideoGenerationOut["progress_stage"],
                      }
                    : {}),
                  ...(progressPct !== undefined ? { progress_pct: progressPct } : {}),
                  ...(errorCode ? { error_code: errorCode } : {}),
                }
              : item,
          ),
        );
      }

      return { id, terminal: isTerminalVideoStatus(status) };
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
          referenceMediaRef.current.filter((item) => item.kind === "image").length >= 9
        ) {
          throw new Error("参考图片最多 9 张");
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
          referenceMediaRef.current.filter((item) => item.kind === "video").length >= 3
        ) {
          throw new Error("参考视频最多 3 个");
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
        const limit = ref.kind === "image" ? 9 : 3;
        const currentCount = prev.filter((item) => item.kind === ref.kind).length;
        if (currentCount >= limit) {
          toast.error(ref.kind === "image" ? "参考图片最多 9 张" : "参考视频最多 3 个");
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
    if (!url) {
      if (assetUrlInput.trim()) {
        toast.error("请输入 asset-* 或 asset://asset-* 官方素材 ID");
      }
      return;
    }
    if (referenceMedia.filter((item) => item.kind === "image").length >= 9) {
      toast.error("参考图片最多 9 张");
      return;
    }
    clearPromptEnhanceChoices();
    setReferenceMedia((prev) => [
      ...prev,
      (() => {
        const identity = nextReferenceIdentity("image", prev);
        return {
          _key: uuid(),
          kind: "image" as const,
          url,
          label: identity.label,
          ref_id: identity.refId,
          display: url,
          previewUrl: null,
        };
      })(),
    ]);
    setAssetUrlInput("");
    toast.success("官方素材已添加");
  }, [assetUrlInput, clearPromptEnhanceChoices, referenceMedia]);

  const createMut = useMutation({
    mutationFn: () =>
      createVideoGeneration({
        action,
        model: selectedModel,
        prompt:
          action === "reference"
            ? serializePromptReferenceMentions(prompt.trim(), referenceMedia)
            : prompt.trim(),
        input_image_id: action === "i2v" ? inputImageId.trim() : null,
        reference_media:
          action === "reference"
            ? referenceMedia.map(referenceMediaPayload)
            : [],
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
    const draftReferenceMedia = item.reference_media.map((ref, index) => {
        const kindIndex =
          item.reference_media
            .slice(0, index + 1)
            .filter((current) => current.kind === ref.kind).length;
        const fallbackLabel = `${ref.kind === "image" ? "图片" : "视频"} ${kindIndex}`;
        const label = ref.label || fallbackLabel;
        return {
          _key: uuid(),
          kind: ref.kind,
          image_id: ref.kind === "image" ? ref.image_id ?? null : null,
          video_id: ref.kind === "video" ? ref.video_id ?? null : null,
          url: ref.url ?? null,
          label,
          ref_id: ref.ref_id || referenceRefId(ref.kind, kindIndex),
          display:
            ref.url
              ? ref.url.replace(/^asset:\/\//i, "asset://")
              : ref.kind === "image"
              ? ref.image_id?.slice(0, 8) ?? "图片"
              : ref.video_id?.slice(0, 8) ?? "视频",
          previewUrl:
            ref.kind === "image"
              ? cleanReferencePreviewUrl(ref.url) ??
                (ref.image_id ? imageVariantUrl(ref.image_id, "display2048") : null)
              : ref.video_id
              ? videoPosterUrl(ref.video_id)
              : null,
        };
      });
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
    const activeReferenceMedia = action === "reference" ? referenceMedia : [];
    const current =
      action === "reference"
        ? serializePromptReferenceMentions(prompt.trim(), activeReferenceMedia)
        : prompt.trim();
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
          input_image_id: action === "i2v" ? inputImageId.trim() || null : null,
          variant_count: VIDEO_PROMPT_VARIANT_COUNT,
          reference_media:
            action === "reference"
              ? referenceMedia.map(referenceMediaPayload)
              : [],
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
      const candidates = displayPromptEnhanceCandidates(
        anchorPromptEnhanceCandidates(
          parsePromptEnhanceCandidates(accumulated),
          current,
          activeReferenceMedia,
        ),
        activeReferenceMedia,
      );
      const recommended = candidates[0];
      if (recommended) {
        const autoApply = shouldAutoApplyPromptEnhanceCandidate(recommended);
        if (autoApply) {
          setPrompt(recommended.prompt);
        }
        setPromptEnhanceCandidates(candidates);
        setSelectedPromptEnhanceCandidateId(autoApply ? recommended.id : "");
        setPromptEnhancePreview("");
        if (recommended.action === "ask_first") {
          toast.success("需要补充信息", {
            description: "已保留原描述，请根据补问补齐后再优化。",
          });
        } else if (recommended.action === "keep_original") {
          toast.success("已判断为原样保留", {
            description: "这个需求更适合保留原工作流，不自动改写。",
          });
        } else if (recommended.action === "optional_vc" && !autoApply) {
          toast.success("已生成可选 VC 版", {
            description: "未自动替换原描述，可手动选择使用。",
          });
        } else {
          toast.success(
            candidates.length > 1
              ? `已生成 ${candidates.length} 个优化方案`
              : "提示词已优化",
          );
        }
      } else {
        setPromptEnhancePreview("");
        toast.error("优化失败", { description: "没有收到有效提示词" });
        setPrompt(original);
      }
    } catch (err) {
      if (!ctl.signal.aborted) {
        const description = err instanceof Error ? err.message : undefined;
        if (accumulated.trim()) {
          const candidates = displayPromptEnhanceCandidates(
            anchorPromptEnhanceCandidates(
              parsePromptEnhanceCandidates(accumulated),
              current,
              activeReferenceMedia,
            ),
            activeReferenceMedia,
          );
          const recommended = candidates[0];
          if (recommended) {
            const autoApply = shouldAutoApplyPromptEnhanceCandidate(recommended);
            if (autoApply) {
              setPrompt(recommended.prompt);
            }
            setPromptEnhanceCandidates(candidates);
            setSelectedPromptEnhanceCandidateId(autoApply ? recommended.id : "");
          } else {
            setPrompt(
              displayPromptReferenceMentions(
                cleanPromptEnhanceText(accumulated),
                activeReferenceMedia,
              ),
            );
          }
          setPromptEnhancePreview("");
          toast.error("优化中断", {
            description: description
              ? `${description} 已保留已生成内容，可继续编辑或重试。`
              : "已保留已生成内容，可继续编辑或重试。",
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
    target.scrollIntoView({ behavior: "smooth", block: "center" });
    requestAnimationFrame(() => target.focus());
  }, []);

  const applyPromptEnhanceCandidate = useCallback(
    (candidate: PromptEnhanceCandidate) => {
      if (!canApplyPromptEnhanceCandidate(candidate)) return;
      setPrompt(candidate.prompt);
      setSelectedPromptEnhanceCandidateId(candidate.id);
      requestAnimationFrame(() => promptRef.current?.focus());
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

  const submitDisabledReason = useMemo(() => {
    if (createMut.isPending) return "正在提交";
    if (optionsQ.isLoading) return "正在读取配置";
    if (!options?.enabled) return options?.unavailable_reason ?? "功能未启用";
    if (!selectedModel) return "没有可用模型";
    if (!availableResolutions.includes(effectiveResolution)) return "当前模型不支持该分辨率";
    if (!availableDurations.includes(effectiveDurationS)) return "当前模型不支持该时长";
    if (!prompt.trim()) return "先填写描述";
    if (action === "i2v" && !inputImageId.trim()) return "需要上传首帧或填写图片 ID";
    if (action === "reference" && referenceMedia.length === 0) {
      return "先添加参考素材";
    }
    if (!seedIsValid) return "Seed 需为 -1 到 4294967295 的整数";
    if (estimate === null) return "缺少预扣估算";
    return "可以提交";
  }, [
    action,
    availableDurations,
    availableResolutions,
    createMut.isPending,
    effectiveDurationS,
    estimate,
    inputImageId,
    options?.enabled,
    options?.unavailable_reason,
    optionsQ.isLoading,
    prompt,
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
      <main className="lumen-studio-bg mx-auto flex h-[calc(100dvh-var(--mobile-tabbar-height))] w-full max-w-[1520px] flex-col gap-3 overflow-x-clip overflow-y-auto overscroll-contain px-3 pb-[calc(var(--mobile-tabbar-height)+1rem)] pt-2 md:h-[calc(100dvh-3rem)] md:overflow-y-auto md:px-5 md:pb-4 xl:overflow-hidden">
        <VideoWorkbenchHeader
          mode={actionLabel(action)}
          profile={parameterProfile}
          audio={generateAudio}
          enabled={serviceEnabled}
          loading={optionsQ.isLoading}
          activeCount={activeItems.length}
          completedCount={completedVideoItems.length}
          serviceSummary={serviceSummary}
          submitState={submitDisabledReason}
        />

        <div className="grid min-h-0 flex-1 gap-3 xl:grid-cols-[minmax(0,1fr)_minmax(320px,380px)] xl:items-stretch">
          <section className="min-w-0">
            <Card
              variant="subtle"
              elevation={2}
              padding="none"
              className="flex h-full min-h-0 flex-col overflow-hidden border-[var(--border)]"
            >
              <div className="min-h-0 flex-1 space-y-3 overflow-y-auto overscroll-contain p-3 pb-[calc(var(--mobile-tabbar-height)+2rem)] sm:p-4 sm:pb-[calc(var(--mobile-tabbar-height)+2rem)] md:pb-4 xl:pb-6">
                <div className="space-y-1.5">
                  <div className="grid min-w-0 grid-cols-[repeat(3,minmax(0,1fr))] rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] p-1">
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
                  <div className="flex flex-wrap items-center justify-between gap-2 px-1 text-xs text-[var(--fg-2)]">
                    <span className="min-w-0 flex-1">{MODE_COPY[action].description}</span>
                    <span className="shrink-0 font-medium text-[var(--fg-1)]">{MODE_COPY[action].requirement}</span>
                  </div>
                </div>

                <div className="grid min-w-0 gap-3 xl:grid-cols-[minmax(0,1fr)_minmax(320px,360px)] xl:items-start">
                  <div className="min-w-0 space-y-3">
                    {action === "i2v" && (
                      <div className="rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-1)]/78 p-2.5 shadow-[var(--shadow-1)]">
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
                        <div className="grid gap-2 lg:grid-cols-[minmax(0,1fr)_minmax(220px,0.38fr)] lg:items-end">
                          <button
                            type="button"
                            onClick={() => fileRef.current?.click()}
                            disabled={uploadMut.isPending}
                            className="group flex min-h-14 items-center gap-3 rounded-[var(--radius-control)] border border-dashed border-[var(--border)] bg-[var(--bg-0)]/72 p-3 text-left transition-[background-color,border-color] hover:border-[var(--border-strong)] hover:bg-[var(--bg-2)] disabled:pointer-events-none disabled:opacity-60"
                          >
                            <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-[var(--radius-control)] border border-[var(--accent-border)] bg-[var(--accent-soft)] text-[var(--accent)]">
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
                              <span className="mt-1 block truncate text-xs font-medium text-[var(--fg-1)]">
                                {uploadedLabel || inputImageId
                                  ? uploadedLabel || "已填写图片 ID"
                                  : "尚未选择首帧"}
                              </span>
                            </span>
                          </button>
                          <label className="space-y-1.5">
                            <span className="type-caption text-[var(--fg-2)]">已有图片 ID</span>
                            <input
                              value={inputImageId}
                              onChange={(event) => {
                                clearPromptEnhanceChoices();
                                setInputImageId(event.target.value);
                                setUploadedLabel("");
                              }}
                              placeholder="image_id"
                              className="h-11 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-1)] px-3 font-mono text-xs text-[var(--fg-0)] outline-none focus:border-[var(--accent)]/50"
                            />
                            <span className="block text-xs leading-5 text-[var(--fg-2)]">
                              从历史或接口复制 ID 时可直接粘贴。
                            </span>
                          </label>
                        </div>
                      </div>
                    )}

                    {action === "reference" && (
                      <div className="rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-1)]/78 p-2.5 shadow-[var(--shadow-1)]">
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
                        <div className="flex flex-wrap items-center gap-2">
                          <button
                            type="button"
                            onClick={() => referenceFileRef.current?.click()}
                            disabled={referenceUploadMut.isPending}
                            className="group inline-flex min-h-10 shrink-0 items-center gap-2 rounded-[var(--radius-control)] border border-dashed border-[var(--border)] bg-[var(--bg-0)]/72 px-3 text-left transition-[background-color,border-color] hover:border-[var(--border-strong)] hover:bg-[var(--bg-2)] disabled:pointer-events-none disabled:opacity-60"
                          >
                            {referenceUploadMut.isPending ? (
                              <RefreshCw className="h-3.5 w-3.5 animate-spin text-[var(--accent)]" />
                            ) : (
                              <Upload className="h-3.5 w-3.5 text-[var(--accent)]" />
                            )}
                            <span className="text-sm font-semibold text-[var(--fg-0)]">
                              上传参考
                            </span>
                          </button>
                          <span className="rounded-full border border-[var(--border)] bg-[var(--bg-0)] px-2.5 py-1 text-xs text-[var(--fg-2)]">
                            图片 {referenceMedia.filter((item) => item.kind === "image").length}/9
                          </span>
                          <span className="rounded-full border border-[var(--border)] bg-[var(--bg-0)] px-2.5 py-1 text-xs text-[var(--fg-2)]">
                            视频 {referenceMedia.filter((item) => item.kind === "video").length}/3
                          </span>
                          <div className="flex w-full min-w-0 flex-wrap items-center gap-2 lg:w-auto lg:min-w-[360px] lg:flex-1">
                            <div className="relative min-w-[180px] flex-1">
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
                                className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] pl-9 pr-3 font-mono text-xs text-[var(--fg-0)] outline-none focus:border-[var(--accent)]/50"
                              />
                            </div>
                            <Button
                              variant="outline"
                              size="sm"
                              disabled={!assetUrlInput.trim()}
                              onClick={addAssetReference}
                            >
                              添加官方素材
                            </Button>
                          </div>
                          <div className="flex min-w-[180px] flex-1 gap-2 overflow-x-auto py-1">
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
                              <span className="shrink-0 rounded-[var(--radius-control)] border border-dashed border-[var(--border)] bg-[var(--bg-1)]/70 px-3 py-2 text-xs text-[var(--fg-2)]">
                                未添加参考素材
                              </span>
                            )}
                          </div>
                        </div>
                      </div>
                    )}

                    <div className="space-y-2">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <span className="type-caption text-[var(--fg-2)]">提示词</span>
                        <div className="flex items-center gap-2">
                          <span className="text-xs tabular-nums text-[var(--fg-2)]">
                            {prompt.length.toLocaleString()} / 10,000
                          </span>
                          <Button
                            variant="outline"
                            size="sm"
                            loading={isEnhancingPrompt}
                            disabled={!canEnhancePrompt}
                            onClick={() => void enhancePromptAction()}
                            leftIcon={<PencilLine className="h-3.5 w-3.5" />}
                          >
                            优化
                          </Button>
                        </div>
                      </div>
                      <textarea
                        ref={promptRef}
                        value={prompt}
                        onChange={(event) => handlePromptChange(event.target.value)}
                        readOnly={isEnhancingPrompt}
                        rows={6}
                        maxLength={10000}
                        placeholder="写清主体、动作轨迹、镜头运动、首尾时间推进；点击参考素材插入 @图片1 / @视频1 来指定素材。"
                        className={cn(
                          "min-h-[160px] w-full resize-none rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)]/80 p-3 text-sm leading-6 text-[var(--fg-0)] outline-none transition-[border-color,box-shadow] focus:border-[var(--accent)]/60 focus:shadow-[var(--ring)] placeholder:text-[var(--fg-2)] sm:min-h-[240px]",
                          isEnhancingPrompt && "cursor-wait border-[var(--accent)]/50",
                        )}
                      />
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
                      <div className="flex flex-wrap gap-2 pb-1">
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
                  </div>

                  <VideoParameterPanel
                    className="xl:sticky xl:top-4"
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
                      const nextResolutions = resolutionOptionsForModel(
                        options,
                        value,
                      );
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
                      setDurationS((prev) =>
                        durationOrPreferred(prev, nextDurations),
                      );
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
                      setDurationS((prev) =>
                        durationOrPreferred(prev, nextDurations),
                      );
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
              </div>
            </Card>
          </section>

          <section className="min-w-0 space-y-4 xl:sticky xl:top-4">
            <Card
              variant="subtle"
              elevation={2}
              padding="none"
              className="flex min-h-[420px] flex-col border-[var(--border)] xl:h-[min(720px,calc(100dvh-5rem))] xl:overflow-hidden"
            >
              <div className="relative flex flex-wrap items-center justify-between gap-3 border-b border-[var(--border-subtle)] bg-[var(--bg-1)]/74 p-3 sm:p-4">
                <span aria-hidden="true" className="absolute left-0 top-3 h-7 w-1 rounded-r-full bg-[var(--accent)]" />
                <div>
                  <div className="flex items-center gap-2">
                    <Clapperboard className="h-4 w-4 text-[var(--fg-2)]" />
                    <p className="type-card-title">任务</p>
                  </div>
                  <p className="mt-1 text-xs text-[var(--fg-2)]">
                    进行中与历史记录
                  </p>
                </div>
                <div className="flex flex-wrap items-center gap-2 text-xs text-[var(--fg-2)]">
                  <span className="rounded-full border border-[var(--border)] bg-[var(--bg-0)] px-2 py-1 tabular-nums">
                    {activeItems.length} 活跃
                  </span>
                  <span className="rounded-full border border-[var(--border)] bg-[var(--bg-0)] px-2 py-1 tabular-nums">
                    {historyQ.isLoading
                      ? "读取中"
                      : `${settledHistoryItems.length}${historyQ.hasNextPage ? "+" : ""} 历史`}
                  </span>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => void historyQ.refetch()}
                    leftIcon={<RefreshCw className="h-3.5 w-3.5" />}
                  >
                    刷新
                  </Button>
                </div>
              </div>
              <div className="space-y-5 p-4 pr-3 sm:p-5 sm:pr-4 xl:min-h-0 xl:flex-1 xl:overflow-y-auto xl:overscroll-contain">
                {activeItems.length > 0 && (
                  <section className="space-y-3">
                    <div className="flex items-center justify-between gap-3">
                      <p className="type-caption text-[var(--fg-2)]">正在进行</p>
                      <span className="text-xs tabular-nums text-[var(--fg-2)]">
                        {activeItems.length} 条
                      </span>
                    </div>
                    <div className="grid gap-3">
                      {activeItems.map((item) => (
                        <TaskRow
                          key={item.id}
                          item={item}
                          onCancel={() => cancelMut.mutate(item.id)}
                          onRetry={() => retryMut.mutate(item.id)}
                          retryDisabled={retryMut.isPending}
                          onCopy={() => {
                            void navigator.clipboard?.writeText(item.prompt);
                            toast.success("描述已复制");
                          }}
                          onUseDraft={() => loadAsDraft(item)}
                          showPreview={false}
                        />
                      ))}
                    </div>
                  </section>
                )}

                <section className="space-y-3">
                  <div className="flex items-center justify-between gap-3">
                    <p className="type-caption text-[var(--fg-2)]">历史记录</p>
                    <span className="text-xs tabular-nums text-[var(--fg-2)]">
                      {historyQ.isLoading
                        ? "读取中"
                        : `${filteredHistoryItems.length}${historyQ.hasNextPage ? "+" : ""} 条`}
                    </span>
                  </div>
                  <HistoryFilterTabs
                    value={historyFilter}
                    counts={{
                      all: settledHistoryItems.length,
                      succeeded: succeededHistoryItems.length,
                      failed: failedHistoryItems.length,
                    }}
                    loading={historyQ.isLoading}
                    onChange={setHistoryFilter}
                  />
                  <div className="grid gap-3">
                    {filteredHistoryItems.map((item) => (
                      <TaskRow
                        key={item.id}
                        item={item}
                        onCancel={() => cancelMut.mutate(item.id)}
                        onRetry={() => retryMut.mutate(item.id)}
                        retryDisabled={retryMut.isPending}
                        onCopy={() => {
                          void navigator.clipboard?.writeText(item.prompt);
                          toast.success("描述已复制");
                        }}
                        onUseDraft={() => loadAsDraft(item)}
                        onDelete={() => item.video && deleteMut.mutate(item.video.id)}
                        onPreview={hasVideo(item) ? () => setSelectedVideoId(item.video.id) : undefined}
                        selected={selectedVideoId === item.video?.id}
                        showPreview={false}
                      />
                    ))}
                    {filteredHistoryItems.length === 0 && (
                      <EmptyPanel
                        icon={<Film className="h-5 w-5" />}
                        title={
                          historyQ.isLoading
                            ? "读取中"
                            : `暂无${videoHistoryFilterLabel(historyFilter)}记录`
                        }
                        description={
                          activeItems.length > 0
                            ? "当前任务完成后会进入历史。"
                            : historyFilter === "all"
                              ? "提交记录会保留状态、参数和结果。"
                              : "切换标签可查看其他状态的记录。"
                        }
                      />
                    )}
                    {historyQ.hasNextPage && (
                      <Button
                        variant="outline"
                        size="sm"
                        className="w-full"
                        loading={historyQ.isFetchingNextPage}
                        onClick={() => void historyQ.fetchNextPage()}
                        leftIcon={<RefreshCw className="h-3.5 w-3.5" />}
                      >
                        {historyQ.isFetchingNextPage ? "加载中" : "加载更早记录"}
                      </Button>
                    )}
                  </div>
                </section>
              </div>
            </Card>
          </section>
        </div>
      </main>
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
      <span className="type-caption text-[var(--fg-2)]">{label}</span>
      <select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="h-10 w-full min-w-0 truncate rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm outline-none focus:border-[var(--accent)]/50"
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
      className={cn(
        "min-w-0 space-y-3 rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/88 p-2.5 shadow-[var(--shadow-2)] backdrop-blur-xl sm:p-3",
        className,
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-control)] border border-[var(--accent-border)] bg-[var(--accent-soft)] text-[var(--accent)]">
              <Settings2 className="h-4 w-4" />
            </span>
            <div className="min-w-0">
              <p className="text-sm font-semibold text-[var(--fg-0)]">生成参数</p>
              <p className="mt-0.5 truncate text-xs text-[var(--fg-2)]">
                {selectedModel || "未选择模型"}
              </p>
            </div>
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

      <div className="grid min-w-0 gap-2 sm:grid-cols-2 xl:grid-cols-1">
        <SelectField
          label="模型"
          value={selectedModel}
          onChange={onModelChange}
          options={modelOptions}
        />
        <div className="grid min-w-0 grid-cols-[minmax(0,1fr)_minmax(0,1fr)] gap-2">
          <SelectField
            label="分辨率"
            value={resolution}
            onChange={onResolutionChange}
            options={resolutionOptions}
          />
          <SelectField
            label="比例"
            value={aspectRatio}
            onChange={onAspectRatioChange}
            options={aspectRatioOptions}
          />
        </div>
        <div className="grid min-w-0 grid-cols-[minmax(0,1fr)_minmax(0,1fr)] gap-2">
          <SelectField
            label="时长"
            value={String(durationS)}
            onChange={onDurationChange}
            options={durationOptions}
            renderOption={(value) => formatDurationLabel(Number(value))}
          />
          <label className="block min-w-0 space-y-1.5">
            <span className="type-caption text-[var(--fg-2)]">Seed</span>
            <input
              value={seed}
              onChange={(event) => onSeedChange(event.target.value)}
              inputMode="numeric"
              placeholder="随机"
              className="h-10 w-full min-w-0 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 font-mono text-xs text-[var(--fg-0)] outline-none focus:border-[var(--accent)]/50"
            />
          </label>
        </div>
        <label className="flex min-h-10 min-w-0 items-center justify-between gap-4 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm">
          <span className="font-medium text-[var(--fg-0)]">生成音频</span>
          <input
            type="checkbox"
            checked={generateAudio}
            onChange={(event) => onGenerateAudioChange(event.target.checked)}
          />
        </label>
      </div>

      <div className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/72 p-3">
        <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)] gap-2">
          <div className="min-w-0">
            <p className="type-caption text-[var(--fg-2)]">预扣</p>
            <p className="mt-1 truncate text-base font-semibold tabular-nums text-[var(--fg-0)]">
              {estimate ? formatRmb(estimate.micro / 1_000_000) : "-"}
            </p>
          </div>
          <div className="min-w-0">
            <p className="type-caption text-[var(--fg-2)]">Token 上限</p>
            <p className="mt-1 truncate text-base font-semibold tabular-nums text-[var(--fg-0)]">
              {estimate ? estimate.tokens.toLocaleString() : "-"}
            </p>
          </div>
        </div>
      </div>

      <SubmitPanel
        canSubmit={canSubmit}
        reason={reason}
        loading={loading}
        onSubmit={onSubmit}
        compact
      />
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
  completedCount,
  serviceSummary,
  submitState,
}: {
  mode: string;
  profile: string;
  audio: boolean;
  enabled: boolean;
  loading: boolean;
  activeCount: number;
  completedCount: number;
  serviceSummary: string;
  submitState: string;
}) {
  const serviceValue = loading ? "读取中" : enabled ? "在线" : "离线";
  const serviceDetail = loading ? "读取配置" : serviceSummary;
  const queueValue = activeCount > 0 ? `${activeCount} 进行中` : `${completedCount} 已完成`;
  const queueDetail = activeCount > 0 ? "任务队列" : "最近结果";

  return (
    <section className="grid shrink-0 gap-2 border-b border-[var(--border)] pb-2 lg:grid-cols-[minmax(0,1fr)_minmax(520px,0.86fr)] lg:items-center">
      <div className="min-w-0">
        <div className="hidden max-w-full items-center gap-2 rounded-full border border-[var(--border)] bg-[var(--bg-1)]/72 px-2.5 py-1 text-xs font-medium text-[var(--fg-1)] shadow-[var(--shadow-1)] sm:inline-flex">
          <Sparkles className="h-3.5 w-3.5 shrink-0 text-[var(--accent)]" />
          <span className="truncate">Lumen 视频工作台</span>
        </div>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 sm:mt-1.5">
          <h1 className="text-2xl font-semibold leading-tight tracking-normal text-[var(--fg-0)] sm:type-page-title-sm">
            视频工作台
          </h1>
          <span className="rounded-full border border-[var(--border)] bg-[var(--bg-1)]/72 px-2 py-0.5 text-xs text-[var(--fg-2)]">
            {submitState}
          </span>
        </div>
      </div>
      <div className="hidden min-w-0 grid-cols-[repeat(3,minmax(0,1fr))] gap-1.5 sm:grid sm:gap-2">
        <StatusStripItem
          label="服务"
          value={serviceValue}
          detail={serviceDetail}
          icon={<Clapperboard className="h-3.5 w-3.5" />}
          active={enabled}
        />
        <StatusStripItem
          label="模式"
          value={mode}
          detail={audio ? "含音频" : "无音频"}
          icon={<Film className="h-3.5 w-3.5" />}
          active
        />
        <StatusStripItem
          label="规格"
          value={profile}
          detail={`${queueValue} · ${queueDetail}`}
          icon={<Gauge className="h-3.5 w-3.5" />}
          active={activeCount > 0}
        />
      </div>
    </section>
  );
}

function StatusStripItem({
  label,
  value,
  detail,
  icon,
  active = false,
}: {
  label: string;
  value: string;
  detail: string;
  icon: React.ReactNode;
  active?: boolean;
}) {
  return (
    <div
      className={cn(
        "relative min-w-0 overflow-hidden rounded-[var(--radius-control)] border px-2 py-1.5 sm:px-2.5 sm:py-2",
        active
          ? "border-[var(--accent-border)] bg-[var(--accent-soft)]"
          : "border-[var(--border-subtle)] bg-[var(--bg-0)]/64",
      )}
    >
      <span
        aria-hidden="true"
        className={cn(
          "absolute inset-y-2 left-0 w-0.5 rounded-r-full",
          active ? "bg-[var(--accent)]" : "bg-[var(--border-strong)]",
        )}
      />
      <div className="flex min-w-0 items-start gap-1.5 sm:gap-2.5">
        <span
          className={cn(
            "mt-0.5 hidden h-6 w-6 shrink-0 items-center justify-center rounded-[var(--radius-control)] border sm:flex",
            active
              ? "border-[var(--accent-border)] bg-[var(--bg-0)] text-[var(--accent)]"
              : "border-[var(--border-subtle)] bg-[var(--bg-1)] text-[var(--fg-2)]",
          )}
        >
          {icon}
        </span>
        <span className="min-w-0">
          <span className="block truncate text-[11px] leading-tight text-[var(--fg-2)] sm:type-caption">
            {label}
          </span>
          <span className="mt-0.5 block truncate text-[10px] font-semibold text-[var(--fg-0)] sm:text-xs">
            {value}
          </span>
          <span className="mt-0.5 hidden truncate text-[11px] text-[var(--fg-2)] sm:block">
            {detail}
          </span>
        </span>
      </div>
    </div>
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
        "group relative min-h-[54px] min-w-0 overflow-hidden rounded-[var(--radius-control)] border px-2.5 py-2 text-left transition-[background-color,border-color,color,transform] duration-200",
        selected
          ? "border-[var(--accent-border)] bg-[var(--accent-soft)] text-[var(--fg-0)] shadow-[var(--shadow-1)]"
          : "border-transparent text-[var(--fg-1)] hover:border-[var(--border)] hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)]",
      )}
    >
      <span
        aria-hidden="true"
        className={cn(
          "absolute inset-x-2 bottom-0 h-0.5 rounded-t-full transition-colors",
          selected ? "bg-[var(--accent)]" : "bg-transparent",
        )}
      />
      <div className="flex items-center justify-between gap-2">
        <span
          className={cn(
            "flex h-7 w-7 shrink-0 items-center justify-center rounded-[var(--radius-control)] border",
            selected
              ? "border-[var(--accent-border)] bg-[var(--bg-0)] text-[var(--accent)]"
              : "border-[var(--border-subtle)] bg-[var(--bg-1)] text-[var(--fg-2)]",
          )}
        >
          {icon}
        </span>
        <span
          className={cn(
            "mt-0.5 h-2 w-2 shrink-0 rounded-full",
            selected ? "bg-[var(--accent)]" : "bg-[var(--fg-3)]",
          )}
        />
      </div>
      <p className="mt-1.5 text-sm font-semibold text-[var(--fg-0)]">
        {copy.title}
      </p>
      <p className="mt-0.5 truncate text-[11px] font-medium text-[var(--fg-2)]">
        {copy.eyebrow}
      </p>
    </button>
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
  const effectivePreviewCandidateId = visibleCandidates.some(
    (candidate) => candidate.id === previewCandidateId,
  )
    ? previewCandidateId
    : visibleCandidates.some((candidate) => candidate.id === selectedId)
      ? selectedId
      : firstCandidate?.id ?? "";
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
    <div className="sticky bottom-3 z-20 flex max-h-[min(72dvh,36rem)] min-h-0 flex-col gap-2 overflow-hidden rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-1)]/95 p-3 shadow-[var(--shadow-2)] backdrop-blur-xl">
      <div className="flex flex-wrap items-center justify-between gap-2">
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
              {loading ? "正在优化提示词" : "优化方案"}
            </span>
            <span className="block truncate text-xs text-[var(--fg-2)]">
              {visibleCandidates.length > 1
                ? autoApplied
                  ? `${visibleCandidates.length} 个候选，已应用推荐版`
                  : `${visibleCandidates.length} 个候选，未自动替换`
                : loading
                  ? "按火山视频结构补动作、运镜和参考一致性"
                  : autoApplied
                    ? "已应用到描述"
                    : "已保留原描述"}
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
            <Button
              variant="ghost"
              size="sm"
              onClick={onDismiss}
              leftIcon={<XCircle className="h-3.5 w-3.5" />}
            >
              清除
            </Button>
          </div>
        )}
      </div>

      {loading && (
        <div className="min-h-20 flex-1 overflow-y-auto rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/72 p-3 text-sm leading-6 text-[var(--fg-1)]">
          {cleanPreview || "等待上游返回..."}
        </div>
      )}

      {!loading && visibleCandidates.length > 0 && previewCandidate && (
        <div className="grid min-h-0 flex-1 gap-2 xl:grid-cols-[minmax(220px,280px)_minmax(0,1fr)]">
          <div className="flex min-w-0 gap-2 overflow-x-auto pb-1 xl:max-h-[min(42dvh,24rem)] xl:flex-col xl:overflow-y-auto xl:pb-0 xl:pr-1">
            {visibleCandidates.map((candidate) => {
              const selected = candidate.id === selectedId;
              const previewing = candidate.id === previewCandidate.id;
              return (
                <button
                  key={candidate.id}
                  type="button"
                  onClick={() => setPreviewCandidateId(candidate.id)}
                  className={cn(
                    "min-h-16 w-[min(78vw,18rem)] shrink-0 rounded-[var(--radius-control)] border bg-[var(--bg-0)] p-2.5 text-left transition-[background-color,border-color] xl:w-full",
                    previewing
                      ? "border-[var(--accent-border)] bg-[var(--accent-soft)]"
                      : "border-[var(--border-subtle)] hover:border-[var(--border-strong)] hover:bg-[var(--bg-2)]",
                  )}
                >
                  <div className="flex min-w-0 items-center gap-2">
                    <span
                      className={cn(
                        "flex h-5 w-5 shrink-0 items-center justify-center rounded-full border",
                        selected
                          ? "border-[var(--accent-border)] text-[var(--accent)]"
                          : "border-[var(--border)] text-[var(--fg-2)]",
                      )}
                    >
                      {selected ? (
                        <CircleCheck className="h-3.5 w-3.5" />
                      ) : (
                        <PencilLine className="h-3 w-3" />
                      )}
                    </span>
                    <p className="min-w-0 truncate text-sm font-semibold text-[var(--fg-0)]">
                      {candidate.title}
                    </p>
                    {selected && (
                      <span className="shrink-0 rounded-full border border-[var(--accent-border)] bg-[var(--bg-0)] px-1.5 py-0.5 text-[10px] font-medium text-[var(--accent)]">
                        已应用
                      </span>
                    )}
                  </div>
                  <p className="mt-1 line-clamp-2 text-xs leading-5 text-[var(--fg-2)]">
                    {candidate.prompt}
                  </p>
                </button>
              );
            })}
          </div>

          <div className="flex min-h-0 flex-col overflow-hidden rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-0)]">
            <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-[var(--border-subtle)] px-3 py-2">
              <div className="min-w-0">
                <p className="truncate text-sm font-semibold text-[var(--fg-0)]">
                  {previewCandidate.title}
                </p>
                <p className="text-xs text-[var(--fg-2)]">
                  {previewCandidate.id === selectedId ? "当前已应用到编辑器" : "预览当前候选"}
                </p>
              </div>
              <div className="flex shrink-0 items-center gap-1">
                <Button
                  variant={previewCandidate.id === selectedId ? "secondary" : "outline"}
                  size="sm"
                  disabled={
                    previewCandidate.id === selectedId ||
                    !canApplyPromptEnhanceCandidate(previewCandidate)
                  }
                  onClick={() => onSelect(previewCandidate)}
                >
                  {promptEnhanceCandidateButtonText(
                    previewCandidate,
                    previewCandidate.id === selectedId,
                  )}
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-9 w-9 px-0"
                  onClick={() => void copyCandidate(previewCandidate)}
                  aria-label="复制优化提示词"
                >
                  <Copy className="h-3.5 w-3.5" />
                </Button>
              </div>
            </div>
            <div className="min-h-[9rem] flex-1 overflow-y-auto whitespace-pre-wrap px-3 py-3 text-sm leading-6 text-[var(--fg-1)]">
              {previewCandidate.prompt}
            </div>
          </div>
        </div>
      )}
    </div>
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
      className="mobile-dialog-shell fixed inset-0 z-[var(--z-dialog)] flex items-end justify-center bg-black/70 backdrop-blur-md sm:items-center sm:p-5"
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
          <div className="flex h-full min-h-[18rem] items-center justify-center overflow-hidden rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-1)]">
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
  compact = false,
}: {
  canSubmit: boolean;
  reason: string;
  loading: boolean;
  onSubmit: () => void;
  compact?: boolean;
}) {
  return (
    <div
      className={cn(
        "rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)]/95 shadow-[var(--shadow-2)] backdrop-blur-xl",
        compact ? "p-2.5" : "p-3",
      )}
    >
      <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
        <p
          className={cn(
            "min-w-0 flex-1 text-xs leading-5",
            canSubmit ? "text-success" : "text-[var(--fg-2)]",
          )}
        >
          {reason}
        </p>
        <Button
          variant="primary"
          size={compact ? "sm" : "md"}
          disabled={!canSubmit}
          loading={loading}
          onClick={onSubmit}
          leftIcon={<Send className="h-4 w-4" />}
          className="w-full sm:w-auto"
        >
          提交
        </Button>
      </div>
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
      className="mobile-dialog-shell fixed inset-0 z-[var(--z-dialog)] flex items-end justify-center bg-black/70 backdrop-blur-md sm:items-center sm:p-5"
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
  const copy = stageCopy(item);
  const videoItem = hasVideo(item) ? item : null;
  const retryable = isFailedHistoryVideo(item);
  const canDownload = videoItem != null || activeTemporaryDownload(item) != null;
  const elapsedLabel = taskElapsedLabel(item);
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
            "h-full rounded-full",
            active ? "bg-[var(--accent)]" : item.status === "succeeded" ? "bg-[var(--success)]" : "bg-[var(--fg-3)]",
          )}
          initial={false}
          animate={{ width: `${progress}%` }}
          transition={{ duration: 0.26, ease: [0.2, 0.8, 0.2, 1] }}
        />
      </div>
      {showPreview && videoItem && onPreview && (
        <VideoPosterButton
          item={videoItem}
          selected={selected}
          onPreview={onPreview}
        />
      )}
      {item.error_message && (
        <p className="mt-2 text-xs text-[var(--danger-fg)]">{item.error_message}</p>
      )}
      <div className="mt-3 flex flex-wrap gap-2">
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
        <Button
          variant="outline"
          size="sm"
          onClick={onCopy}
          leftIcon={<Copy className="h-3.5 w-3.5" />}
        >
          复制
        </Button>
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
        {onDelete && videoItem && (
          <Button
            variant="outline"
            size="sm"
            onClick={onDelete}
            leftIcon={<Trash2 className="h-3.5 w-3.5" />}
          >
            删除
          </Button>
        )}
      </div>
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
