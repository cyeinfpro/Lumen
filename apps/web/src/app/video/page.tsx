"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  ChevronDown,
  ImageIcon,
  RefreshCw,
  Sparkles,
  Tags,
  Upload,
  Video as VideoIcon,
} from "lucide-react";

import {
  cancelVideoGeneration,
  createVideoGeneration,
  deleteVideo,
  enhanceVideoPrompt,
  imageVariantUrl,
  retryVideoGeneration,
  uploadImage,
  videoPosterUrl,
} from "@/lib/apiClient";
import { useSSE } from "@/lib/useSSE";
import {
  isVideoRequestFenceCurrent,
  isTerminalVideoEvent,
  mergeVideoGenerationEvent,
  mergeVideoGenerationLists as mergeById,
  nextVideoRequestFence,
  videoGenerationEventId,
} from "@/lib/videoEventSnapshot";
import type { VideoRequestFence } from "@/lib/videoEventSnapshot";
import type {
  VideoAction,
  VideoGenerationOut,
  VideoOptionsOut,
} from "@/lib/types";
import { Button, toast } from "@/components/ui/primitives";
import { DesktopTopNav, MobileTabBar } from "@/components/ui/shell";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import { cn, uuid } from "@/lib/utils";
import {
  ModeCard,
  PromptEnhanceChooser,
  ReferenceChip,
  ReferenceMediaPreviewDialog,
  VideoParameterPanel,
  VideoWorkbenchHeader,
  canApplyPromptEnhanceCandidate,
  cleanPromptEnhanceText,
  focusVideoWorkbenchElement,
  shouldAutoApplyPromptEnhanceCandidate,
} from "./video-workbench-ui";
import type {
  PromptEnhanceAction,
  PromptEnhanceCandidate,
  ReferenceDraft,
} from "./video-workbench-ui";
import {
  fetchVideoGeneration,
  fetchVideoGenerations,
  fetchVideoOptions,
  generationRefreshRequestIsCurrent,
  isAbortError,
  recordGenerationRefreshFailure,
  revokeReferenceObjectUrl,
  revokeUnusedReferenceObjectUrls,
  uploadReferenceVideo,
} from "./video-request-lifecycle";
import type {
  DraftUploadRequest,
  GenerationRefreshRequest,
  ReferenceUploadRequest,
  ReferenceUploadResult,
} from "./video-request-lifecycle";
import {
  DEFAULT_REFERENCE_LIMITS,
  REFERENCE_KINDS,
  anchorPromptEnhanceCandidates,
  displayPromptEnhanceCandidates,
  displayPromptReferenceMentions,
  isNewApiVideoModel,
  nextReferenceIdentity,
  normalizeAssetUrl,
  promptContainsReferenceMention,
  promptForVideoAction,
  referenceCountsFor,
  referenceDisplayToken,
  referenceKindNoun,
  referenceLabel,
  referenceLimitMessage,
  referenceLimitViolation,
  referenceLimitsForModel,
  referencePayloadForVideoAction,
  referenceRefId,
  referencesForVideoAction,
} from "./video-reference-domain";
import type {
  ReferenceKind,
  ReferenceLimits,
} from "./video-reference-domain";
import {
  MODE_COPY,
  actionLabel,
  formatDurationLabel,
  hasVideo,
  isActiveVideo,
  isFailedHistoryVideo,
  isTerminalVideo,
} from "./video-task-model";
import type {
  VideoGenerationWithVideo,
  VideoHistoryFilter,
} from "./video-task-model";
import {
  VideoPreviewDialog,
  VideoTaskDrawer,
  prewarmVideoItem,
} from "./video-task-ui";
import {
  billingModelForAction,
  durationOptionsForModel,
  durationOrPreferred,
  estimateHoldMicro,
  firstModelForAction,
  parseSeed,
  preferredDuration,
  preferredResolution,
  resolutionOptionsForModel,
  toVideoResolution,
  videoUnavailableReasonMessage,
} from "./video-options-model";

const VIDEO_EVENTS = [
  "video.queued",
  "video.submitted",
  "video.progress",
  "video.fetching",
  "video.succeeded",
  "video.failed",
  "video.canceled",
];
const VIDEO_ACTIVE_POLL_MS = 2500;
const VIDEO_REFRESH_MIN_INTERVAL_MS = 900;
const VIDEO_PROMPT_VARIANT_COUNT = 3;
const VIDEO_HISTORY_PAGE_SIZE = 12;
const VIDEO_PROMPT_VARIANT_TITLES = [
  "推荐镜头版",
  "动作节奏版",
  "参考一致版",
];
type GenerationRefreshOptions = {
  forceHistorySync?: boolean;
};
type GenerationRefreshScheduleOptions = GenerationRefreshOptions & {
  delayMs?: number;
};
type ScheduleGenerationRefresh = (
  id: string,
  opts?: GenerationRefreshScheduleOptions,
) => void;
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

function motionSafeScrollBehavior(): ScrollBehavior {
  if (
    typeof window !== "undefined" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  ) {
    return "auto";
  }
  return "smooth";
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

function inputImageForVideoAction(
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
  referenceCount,
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
  referenceCount: number;
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
      referenceCount,
      referenceLimitError,
    }) ??
    videoEstimateIssue(seedIsValid, estimate) ??
    "可以提交"
  );
}

function filteredVideoHistoryItems(
  historyFilter: VideoHistoryFilter,
  settledItems: VideoGenerationOut[],
  succeededItems: VideoGenerationOut[],
  failedItems: VideoGenerationOut[],
): VideoGenerationOut[] {
  if (historyFilter === "succeeded") return succeededItems;
  if (historyFilter === "failed") return failedItems;
  return settledItems;
}

function hasPromptEnhancementPanel(
  isEnhancing: boolean,
  preview: string,
  candidates: PromptEnhanceCandidate[],
): boolean {
  return isEnhancing || Boolean(preview.trim()) || candidates.length > 0;
}

export default function VideoPage() {
  const qc = useQueryClient();
  const fileRef = useRef<HTMLInputElement | null>(null);
  const referenceFileRef = useRef<HTMLInputElement | null>(null);
  const promptRef = useRef<HTMLTextAreaElement | null>(null);
  const promptEnhanceAbortRef = useRef<AbortController | null>(null);
  const promptEnhanceEpochRef = useRef(0);
  const firstFrameUploadAbortRef = useRef<AbortController | null>(null);
  const firstFrameUploadEpochRef = useRef(0);
  const referenceUploadAbortRef = useRef<AbortController | null>(null);
  const referenceUploadEpochRef = useRef(0);
  const draftFenceRef = useRef<VideoRequestFence>({
    taskId: "draft:new",
    epoch: 0,
  });
  const retryRequestFenceRef = useRef<VideoRequestFence>({
    taskId: "retry:none",
    epoch: 0,
  });
  const actionRef = useRef<VideoAction>("t2v");
  const referenceLimitsRef = useRef<ReferenceLimits>(
    DEFAULT_REFERENCE_LIMITS,
  );
  const terminalHistorySyncedRef = useRef<Set<string>>(new Set());
  const generationRefreshRequestsRef = useRef<
    Map<string, GenerationRefreshRequest>
  >(new Map());
  const generationRefreshEpochRef = useRef<Map<string, number>>(new Map());
  const scheduledRefreshTimersRef = useRef<Map<string, number>>(new Map());
  const scheduleGenerationRefreshRef = useRef<ScheduleGenerationRefresh>(
    () => {},
  );
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
  const previousReferenceMediaRef = useRef<ReferenceDraft[]>([]);
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
  const promptEnhancePanelVisible = hasPromptEnhancementPanel(
    isEnhancingPrompt,
    promptEnhancePreview,
    promptEnhanceCandidates,
  );
  useBodyScrollLock(isTaskPanelOpen, {
    bodyOverscrollBehavior: "none",
    documentOverscrollBehavior: "none",
  });

  const optionsQ = useQuery({
    queryKey: ["video", "options"],
    queryFn: ({ signal }) => fetchVideoOptions(signal),
    retry: false,
    staleTime: 60_000,
    gcTime: 5 * 60_000,
  });
  const historyQ = useInfiniteQuery({
    queryKey: ["video", "generations"],
    queryFn: ({ pageParam, signal }) =>
      fetchVideoGenerations(
        {
          cursor: pageParam,
          limit: VIDEO_HISTORY_PAGE_SIZE,
        },
        signal,
      ),
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
    actionRef.current = action;
  }, [action]);

  useEffect(() => {
    referenceMediaRef.current = referenceMedia;
    revokeUnusedReferenceObjectUrls(
      previousReferenceMediaRef.current,
      referenceMedia,
    );
    previousReferenceMediaRef.current = referenceMedia;
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
  const filteredHistoryItems = useMemo(
    () =>
      filteredVideoHistoryItems(
        historyFilter,
        settledHistoryItems,
        succeededHistoryItems,
        failedHistoryItems,
      ),
    [failedHistoryItems, historyFilter, settledHistoryItems, succeededHistoryItems],
  );
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
    async (
      id: string,
      request: GenerationRefreshRequest,
      opts: GenerationRefreshOptions = {},
    ): Promise<boolean> => {
      const next = await fetchVideoGeneration(id, request.controller.signal);
      if (
        !generationRefreshRequestIsCurrent(
          request,
          generationRefreshRequestsRef.current.get(id),
          generationRefreshEpochRef.current.get(id),
        ) ||
        next.id !== id
      ) {
        return false;
      }
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
        await qc.invalidateQueries({ queryKey: ["video", "generations"] });
        if (terminal) terminalHistorySyncedRef.current.add(id);
      }
      return true;
    },
    [qc],
  );

  const refreshGenerationSafe = useCallback(
    async (id: string, opts: GenerationRefreshOptions = {}) => {
      if (opts.forceHistorySync) {
        pendingHistoryRefreshRef.current.add(id);
      }
      const existing = generationRefreshRequestsRef.current.get(id);
      if (existing && !opts.forceHistorySync) return;
      existing?.controller.abort();

      const forceHistorySync =
        opts.forceHistorySync || pendingHistoryRefreshRef.current.has(id);
      pendingHistoryRefreshRef.current.delete(id);
      const request: GenerationRefreshRequest = {
        controller: new AbortController(),
        epoch: (generationRefreshEpochRef.current.get(id) ?? 0) + 1,
      };
      generationRefreshEpochRef.current.set(id, request.epoch);
      generationRefreshRequestsRef.current.set(id, request);

      try {
        const committed = await refreshGeneration(id, request, {
          forceHistorySync,
        });
        if (!committed) return;
        refreshFailureCountRef.current.delete(id);
        refreshBackoffUntilRef.current.delete(id);
      } catch (err) {
        if (
          isAbortError(err) ||
          !generationRefreshRequestIsCurrent(
            request,
            generationRefreshRequestsRef.current.get(id),
            generationRefreshEpochRef.current.get(id),
          )
        ) {
          return;
        }
        recordGenerationRefreshFailure(
          id,
          err,
          refreshFailureCountRef.current,
          refreshBackoffUntilRef.current,
        );
        if (forceHistorySync) {
          pendingHistoryRefreshRef.current.add(id);
        }
        scheduleGenerationRefreshRef.current(id, { forceHistorySync });
      } finally {
        if (generationRefreshRequestsRef.current.get(id) === request) {
          generationRefreshRequestsRef.current.delete(id);
        }
      }
    },
    [refreshGeneration],
  );

  const abortGenerationRefresh = useCallback((id: string) => {
    const request = generationRefreshRequestsRef.current.get(id);
    request?.controller.abort();
    generationRefreshRequestsRef.current.delete(id);
    generationRefreshEpochRef.current.set(
      id,
      (generationRefreshEpochRef.current.get(id) ?? 0) + 1,
    );
    const timer = scheduledRefreshTimersRef.current.get(id);
    if (timer != null) window.clearTimeout(timer);
    scheduledRefreshTimersRef.current.delete(id);
    pendingHistoryRefreshRef.current.delete(id);
  }, []);

  const scheduleGenerationRefresh = useCallback(
    (id: string, opts: GenerationRefreshScheduleOptions = {}) => {
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

  useEffect(() => {
    scheduleGenerationRefreshRef.current = scheduleGenerationRefresh;
    return () => {
      scheduleGenerationRefreshRef.current = () => {};
    };
  }, [scheduleGenerationRefresh]);

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
      for (const request of generationRefreshRequestsRef.current.values()) {
        request.controller.abort();
      }
      generationRefreshRequestsRef.current.clear();
      retryRequestFenceRef.current = nextVideoRequestFence(
        retryRequestFenceRef.current,
        "retry:disposed",
      );
      promptEnhanceAbortRef.current?.abort();
      firstFrameUploadAbortRef.current?.abort();
      referenceUploadAbortRef.current?.abort();
      revokeUnusedReferenceObjectUrls(
        previousReferenceMediaRef.current,
        [],
      );
    },
    [],
  );

  const availableModels = useMemo(
    () => options?.models.filter((item) => item.actions.includes(action)) ?? [],
    [action, options?.models],
  );
  const selectedModel =
    availableModels.find((item) => item.model === model)?.model ??
    availableModels[0]?.model ??
    "";
  const referenceLimits = useMemo(
    () => referenceLimitsForModel(selectedModel),
    [selectedModel],
  );
  useEffect(() => {
    referenceLimitsRef.current = referenceLimits;
  }, [referenceLimits]);
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

  const abortPromptEnhancement = useCallback(() => {
    promptEnhanceEpochRef.current += 1;
    const controller = promptEnhanceAbortRef.current;
    promptEnhanceAbortRef.current = null;
    controller?.abort();
    setIsEnhancingPrompt(false);
  }, []);

  const cancelFirstFrameUpload = useCallback(() => {
    firstFrameUploadEpochRef.current += 1;
    const controller = firstFrameUploadAbortRef.current;
    firstFrameUploadAbortRef.current = null;
    controller?.abort();
  }, []);

  const cancelReferenceUpload = useCallback(() => {
    referenceUploadEpochRef.current += 1;
    const controller = referenceUploadAbortRef.current;
    referenceUploadAbortRef.current = null;
    controller?.abort();
  }, []);

  const commitReferenceMedia = useCallback(
    (update: (current: ReferenceDraft[]) => ReferenceDraft[]): boolean => {
      const current = referenceMediaRef.current;
      const next = update(current);
      if (next === current) return false;
      referenceMediaRef.current = next;
      setReferenceMedia(next);
      return true;
    },
    [],
  );

  const switchDraftContext = useCallback(
    (taskId: string, nextAction: VideoAction) => {
      draftFenceRef.current = nextVideoRequestFence(
        draftFenceRef.current,
        taskId,
      );
      actionRef.current = nextAction;
      abortPromptEnhancement();
      cancelFirstFrameUpload();
      cancelReferenceUpload();
      clearPromptEnhanceChoices();
      setReferencePreviewItem(null);
    },
    [
      abortPromptEnhancement,
      cancelFirstFrameUpload,
      cancelReferenceUpload,
      clearPromptEnhanceChoices,
    ],
  );

  const isCurrentFirstFrameUpload = useCallback(
    (request: DraftUploadRequest): boolean =>
      firstFrameUploadAbortRef.current === request.controller &&
      firstFrameUploadEpochRef.current === request.epoch &&
      !request.controller.signal.aborted &&
      actionRef.current === request.expectedAction &&
      isVideoRequestFenceCurrent(draftFenceRef.current, request.draftFence),
    [],
  );

  const isCurrentReferenceUpload = useCallback(
    (request: ReferenceUploadRequest): boolean =>
      referenceUploadAbortRef.current === request.controller &&
      referenceUploadEpochRef.current === request.epoch &&
      !request.controller.signal.aborted &&
      actionRef.current === request.expectedAction &&
      isVideoRequestFenceCurrent(draftFenceRef.current, request.draftFence),
    [],
  );

  const focusPromptTarget = useCallback(
    (target: HTMLTextAreaElement, options?: FocusOptions): boolean =>
      focusVideoWorkbenchElement(
        target,
        options,
        Boolean(
          promptEnhanceAbortRef.current ||
            firstFrameUploadAbortRef.current ||
            referenceUploadAbortRef.current,
        ),
      ),
    [],
  );

  const insertPromptText = useCallback((text: string) => {
    abortPromptEnhancement();
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
      if (!focusPromptTarget(target)) return;
      target.setSelectionRange(pos, pos);
    });
  }, [
    abortPromptEnhancement,
    clearPromptEnhanceSelection,
    focusPromptTarget,
    prompt,
  ]);

  const insertReferenceTag = useCallback((item: ReferenceDraft) => {
    insertPromptText(referenceDisplayToken(item));
  }, [insertPromptText]);

  const uploadMut = useMutation({
    mutationFn: (request: DraftUploadRequest) =>
      uploadImage(request.file, { signal: request.controller.signal }),
    onSuccess: (img, request) => {
      if (!isCurrentFirstFrameUpload(request)) return;
      abortPromptEnhancement();
      clearPromptEnhanceChoices();
      setInputImageId(img.id);
      setUploadedLabel(`${img.width}x${img.height}`);
      toast.success("首帧已上传");
    },
    onError: (err, request) => {
      if (isAbortError(err) || !isCurrentFirstFrameUpload(request)) return;
      toast.error("上传失败", {
        description: err instanceof Error ? err.message : undefined,
      });
    },
    onSettled: (_data, _error, request) => {
      if (firstFrameUploadAbortRef.current === request.controller) {
        firstFrameUploadAbortRef.current = null;
      }
    },
  });

  const referenceUploadMut = useMutation({
    mutationFn: async (
      request: ReferenceUploadRequest,
    ): Promise<ReferenceUploadResult> => {
      if (
        referenceMediaRef.current.filter(
          (item) => item.kind === request.kind,
        ).length >= request.limit
      ) {
        throw new Error(referenceLimitMessage(request.kind, request.limit));
      }
      if (request.kind === "image") {
        const img = await uploadImage(request.file, {
          signal: request.controller.signal,
        });
        return {
          kind: "image" as const,
          image_id: img.id,
          display: `${img.width}x${img.height}`,
          previewUrl: imageReferencePreviewUrl(img),
        };
      }
      const video = await uploadReferenceVideo(
        request.file,
        request.controller.signal,
      );
      return {
        kind: "video" as const,
        video_id: video.id,
        display: video.size_bytes
          ? `${Math.round(video.size_bytes / 1024 / 1024)}MB`
          : "视频",
        previewUrl:
          cleanReferencePreviewUrl(video.poster_url) ??
          videoPosterUrl(video.id),
      };
    },
    onSuccess: (ref, request) => {
      if (!isCurrentReferenceUpload(request)) {
        revokeReferenceObjectUrl(ref.previewUrl);
        return;
      }
      abortPromptEnhancement();
      clearPromptEnhanceChoices();
      const limit = referenceLimitsRef.current[ref.kind];
      const accepted = commitReferenceMedia((current) => {
        const currentCount = current.filter(
          (item) => item.kind === ref.kind,
        ).length;
        if (currentCount >= limit) {
          return current;
        }
        const identity = nextReferenceIdentity(ref.kind, current);
        return [
          ...current,
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
      if (!accepted) {
        revokeReferenceObjectUrl(ref.previewUrl);
        toast.error(referenceLimitMessage(ref.kind, limit));
        return;
      }
      toast.success("参考素材已上传");
    },
    onError: (err, request) => {
      if (isAbortError(err) || !isCurrentReferenceUpload(request)) return;
      toast.error("上传失败", {
        description: err instanceof Error ? err.message : undefined,
      });
    },
    onSettled: (_data, _error, request) => {
      if (referenceUploadAbortRef.current === request.controller) {
        referenceUploadAbortRef.current = null;
      }
    },
  });

  const startFirstFrameUpload = useCallback(
    (file: File) => {
      if (actionRef.current !== "i2v") return;
      abortPromptEnhancement();
      clearPromptEnhanceChoices();
      cancelFirstFrameUpload();
      const controller = new AbortController();
      const request: DraftUploadRequest = {
        controller,
        draftFence: { ...draftFenceRef.current },
        epoch: firstFrameUploadEpochRef.current + 1,
        expectedAction: "i2v",
        file,
      };
      firstFrameUploadEpochRef.current = request.epoch;
      firstFrameUploadAbortRef.current = controller;
      uploadMut.mutate(request);
    },
    [
      abortPromptEnhancement,
      cancelFirstFrameUpload,
      clearPromptEnhanceChoices,
      uploadMut,
    ],
  );

  const startReferenceUpload = useCallback(
    (file: File) => {
      if (actionRef.current !== "reference") return;
      const kind = file.type.startsWith("image/")
        ? "image"
        : file.type.startsWith("video/")
          ? "video"
          : null;
      if (!kind) {
        toast.error("上传失败", { description: "只支持图片或视频" });
        return;
      }
      const limit = referenceLimitsRef.current[kind];
      if (
        referenceMediaRef.current.filter((item) => item.kind === kind).length >=
        limit
      ) {
        toast.error(referenceLimitMessage(kind, limit));
        return;
      }
      abortPromptEnhancement();
      clearPromptEnhanceChoices();
      cancelReferenceUpload();
      const controller = new AbortController();
      const request: ReferenceUploadRequest = {
        controller,
        draftFence: { ...draftFenceRef.current },
        epoch: referenceUploadEpochRef.current + 1,
        expectedAction: "reference",
        file,
        kind,
        limit,
      };
      referenceUploadEpochRef.current = request.epoch;
      referenceUploadAbortRef.current = controller;
      referenceUploadMut.mutate(request);
    },
    [
      abortPromptEnhancement,
      cancelReferenceUpload,
      clearPromptEnhanceChoices,
      referenceUploadMut,
    ],
  );

  const addAssetReference = useCallback(() => {
    if (referenceUploadAbortRef.current) return;
    const url = normalizeAssetUrl(assetUrlInput);
    const kind = selectedAssetReferenceKind;
    if (!url) {
      if (assetUrlInput.trim()) {
        toast.error("请输入 asset-* 或 asset://asset-* 官方素材 ID");
      }
      return;
    }
    const current = referenceMediaRef.current;
    const limit = referenceLimitsRef.current[kind];
    if (
      current.filter((item) => item.kind === kind).length >=
      limit
    ) {
      toast.error(referenceLimitMessage(kind, limit));
      return;
    }
    abortPromptEnhancement();
    clearPromptEnhanceChoices();
    commitReferenceMedia((references) => [
      ...references,
      (() => {
        const identity = nextReferenceIdentity(kind, references);
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
    abortPromptEnhancement,
    clearPromptEnhanceChoices,
    commitReferenceMedia,
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
    onSuccess: (gen, requestedId) => {
      if (gen.id !== requestedId) return;
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
    mutationFn: (request: VideoRequestFence) =>
      retryVideoGeneration(request.taskId),
    onSuccess: (gen, request) => {
      if (
        !isVideoRequestFenceCurrent(
          retryRequestFenceRef.current,
          request,
        )
      ) {
        return;
      }
      terminalHistorySyncedRef.current.delete(gen.id);
      setItems((prev) => mergeById(prev, [gen]));
      setIsTaskPanelOpen(true);
      const createdNewTask = gen.id !== request.taskId;
      toast.success(createdNewTask ? "已创建新的重试任务" : "已重新生成", {
        description: createdNewTask
          ? `正在跟踪新任务 ${gen.id.slice(0, 8)}`
          : undefined,
      });
      scheduleGenerationRefresh(gen.id, { delayMs: 800 });
      void qc.invalidateQueries({ queryKey: ["video", "generations"] });
    },
    onError: (err, request) => {
      if (
        !isVideoRequestFenceCurrent(
          retryRequestFenceRef.current,
          request,
        )
      ) {
        return;
      }
      toast.error("重试失败", {
        description: err instanceof Error ? err.message : undefined,
      });
    },
  });
  const requestVideoRetry = (generationId: string) => {
    const request = nextVideoRequestFence(
      retryRequestFenceRef.current,
      generationId,
    );
    retryRequestFenceRef.current = request;
    retryMut.mutate(request);
  };
  const deleteMut = useMutation({
    mutationFn: deleteVideo,
    onSuccess: async (_data, videoId) => {
      for (const item of effectiveItems) {
        if (item.video?.id === videoId) abortGenerationRefresh(item.id);
      }
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
    switchDraftContext(item.id, item.action);
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
    commitReferenceMedia(() => draftReferenceMedia);
    setPrompt(displayPromptReferenceMentions(item.prompt, draftReferenceMedia));
    requestAnimationFrame(() => {
      const target = promptRef.current;
      if (target) focusPromptTarget(target);
    });
    toast.success("已套用参数");
  }, [commitReferenceMedia, focusPromptTarget, switchDraftContext]);

  const canEnhancePrompt = Boolean(
    !uploadMut.isPending &&
      !referenceUploadMut.isPending &&
      (prompt.trim() ||
        (action === "i2v" && inputImageId.trim()) ||
        (action === "reference" && referenceMedia.length > 0)),
  );

  const enhancePromptAction = useCallback(async () => {
    if (
      isEnhancingPrompt ||
      !canEnhancePrompt ||
      firstFrameUploadAbortRef.current ||
      referenceUploadAbortRef.current
    ) {
      return;
    }
    const original = prompt;
    const activeReferenceMedia = referencesForVideoAction(action, referenceMedia);
    const current = promptForVideoAction(action, prompt, activeReferenceMedia);
    const ctl = new AbortController();
    promptEnhanceAbortRef.current?.abort();
    const requestEpoch = promptEnhanceEpochRef.current + 1;
    const requestDraftFence = { ...draftFenceRef.current };
    promptEnhanceEpochRef.current = requestEpoch;
    promptEnhanceAbortRef.current = ctl;
    clearPromptEnhanceChoices();
    setIsEnhancingPrompt(true);
    let accumulated = "";
    const isCurrentRequest = () =>
      !ctl.signal.aborted &&
      promptEnhanceAbortRef.current === ctl &&
      promptEnhanceEpochRef.current === requestEpoch &&
      isVideoRequestFenceCurrent(draftFenceRef.current, requestDraftFence);
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
          if (!isCurrentRequest()) return;
          accumulated += delta;
          setPromptEnhancePreview(
            displayPromptReferenceMentions(accumulated, activeReferenceMedia),
          );
        },
        ctl.signal,
      );
      if (!isCurrentRequest()) return;
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
      if (isCurrentRequest()) {
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
      if (
        promptEnhanceAbortRef.current === ctl &&
        promptEnhanceEpochRef.current === requestEpoch
      ) {
        promptEnhanceAbortRef.current = null;
        setIsEnhancingPrompt(false);
      }
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
    requestAnimationFrame(() => focusPromptTarget(target));
  }, [focusPromptTarget]);

  const applyPromptEnhanceCandidate = useCallback(
    (candidate: PromptEnhanceCandidate) => {
      if (!canApplyPromptEnhanceCandidate(candidate)) return;
      setPrompt(candidate.prompt);
      setSelectedPromptEnhanceCandidateId(candidate.id);
      requestAnimationFrame(() => {
        const target = promptRef.current;
        if (target) focusPromptTarget(target, { preventScroll: true });
      });
    },
    [focusPromptTarget],
  );

  const handlePromptChange = useCallback(
    (value: string) => {
      abortPromptEnhancement();
      clearPromptEnhanceSelection();
      setPrompt(
        action === "reference"
          ? displayPromptReferenceMentions(value, referenceMedia)
          : value,
      );
    },
    [
      abortPromptEnhancement,
      action,
      clearPromptEnhanceSelection,
      referenceMedia,
    ],
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

  const uploadsPending =
    uploadMut.isPending || referenceUploadMut.isPending;
  const submitDisabledReason = useMemo(() => {
    return videoSubmitDisabledReason({
      createPending: createMut.isPending,
      uploadPending: uploadsPending,
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
    uploadsPending,
  ]);

  const canSubmit = submitDisabledReason === "可以提交";
  const submitVideo = useCallback(() => {
    if (
      !canSubmit ||
      firstFrameUploadAbortRef.current ||
      referenceUploadAbortRef.current
    ) {
      return;
    }
    createMut.mutate();
  }, [canSubmit, createMut]);
  const serviceEnabled = Boolean(options?.enabled);
  const serviceSummary = optionsQ.isLoading
    ? "读取视频服务配置"
    : serviceEnabled
      ? `${availableModels.length} 个模型可用`
      : videoUnavailableReasonMessage(options?.unavailable_reason);
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
      <main className="lumen-studio-bg mx-auto flex h-[calc(100dvh-var(--mobile-tabbar-height))] w-full max-w-[1600px] flex-col gap-3 overflow-x-clip overflow-y-auto overscroll-contain px-3 pb-[max(1rem,env(safe-area-inset-bottom,0px))] pt-2 [scroll-padding-bottom:calc(var(--mobile-tabbar-height)+6rem)] md:h-[calc(100dvh-3rem)] md:px-5 md:pb-4 md:[scroll-padding-bottom:1rem]">
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
                      copy={MODE_COPY[key]}
                      selected={action === key}
                      onSelect={() => {
                        switchDraftContext(`draft:${key}`, key);
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

              <div className="space-y-3 p-3 sm:p-4 md:pb-5 lg:pb-6">
                {action === "i2v" && (
                  <section className="overflow-hidden rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)]/66">
                    <input
                      ref={fileRef}
                      type="file"
                      accept="image/png,image/jpeg,image/webp,image/mpo"
                      className="hidden"
                      onChange={(event) => {
                        const file = event.target.files?.[0];
                        if (file) startFirstFrameUpload(file);
                        event.target.value = "";
                      }}
                    />
                    <div className="flex flex-col items-start gap-1.5 border-b border-[var(--border-subtle)] px-3 py-2.5 min-[390px]:flex-row min-[390px]:items-center min-[390px]:justify-between">
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
                            cancelFirstFrameUpload();
                            abortPromptEnhancement();
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
                        if (file) startReferenceUpload(file);
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
                      <div className="flex min-w-0 flex-col items-stretch gap-2 min-[390px]:flex-row min-[390px]:items-center">
                        <Button
                          variant="outline"
                          size="sm"
                          loading={referenceUploadMut.isPending}
                          disabled={referenceUploadMut.isPending}
                          onClick={() => referenceFileRef.current?.click()}
                          leftIcon={<Upload className="h-3.5 w-3.5" />}
                        >
                          上传参考
                        </Button>
                        <p className="min-w-0 flex-1 text-xs leading-5 text-[var(--fg-2)]">
                          点击素材可预览，点击文字可插入引用。
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
                              abortPromptEnhancement();
                              clearPromptEnhanceChoices();
                              setReferencePreviewItem((current) =>
                                current?._key === item._key ? null : current,
                              );
                              commitReferenceMedia((current) =>
                                current.filter((ref) => ref._key !== item._key),
                              );
                            }}
                          />
                        ))}
                        {referenceMedia.length === 0 && (
                          <button
                            type="button"
                            disabled={referenceUploadMut.isPending}
                            onClick={() => referenceFileRef.current?.click()}
                            className="flex min-h-24 min-w-[min(240px,calc(100vw-3rem))] flex-col items-center justify-center gap-2 rounded-[var(--radius-control)] border border-dashed border-[var(--border)] bg-[var(--bg-1)]/50 px-5 text-center text-xs text-[var(--fg-2)] transition-colors hover:border-[var(--border-strong)] hover:bg-[var(--bg-2)] disabled:pointer-events-none disabled:opacity-60"
                          >
                            <Upload className="h-4 w-4" />
                            添加图片或视频参考
                          </button>
                        )}
                      </div>
                    </div>
                    <details className="group border-t border-[var(--border-subtle)]">
                      <summary className="flex min-h-11 cursor-pointer list-none items-center justify-between gap-3 px-3 py-2.5 text-xs font-medium text-[var(--fg-1)] transition-colors hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)]">
                        <span className="inline-flex items-center gap-2">
                          <Tags className="h-3.5 w-3.5 text-[var(--fg-2)]" />
                          添加官方素材 ID
                        </span>
                        <ChevronDown className="h-3.5 w-3.5 transition-transform group-open:rotate-180" />
                      </summary>
                      <div className="grid grid-cols-1 gap-2 border-t border-[var(--border-subtle)] bg-[var(--bg-1)]/56 p-3 min-[390px]:grid-cols-[auto_minmax(0,1fr)_auto] min-[390px]:items-center">
                        <div className="inline-flex h-11 w-full shrink-0 overflow-hidden rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] p-0.5 min-[390px]:w-auto">
                          {assetReferenceKindOptions.map((kind) => {
                            const active = selectedAssetReferenceKind === kind;
                            return (
                              <button
                                key={kind}
                                type="button"
                                aria-pressed={active}
                                disabled={referenceUploadMut.isPending}
                                onClick={() => setAssetReferenceKind(kind)}
                                className={cn(
                                  "inline-flex min-w-12 flex-1 items-center justify-center rounded-[calc(var(--radius-control)-2px)] px-2.5 text-xs font-semibold transition-colors",
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
                        <div className="relative min-w-0 flex-1">
                          <Tags className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-[var(--fg-2)]" />
                          <input
                            value={assetUrlInput}
                            disabled={referenceUploadMut.isPending}
                            onChange={(event) => setAssetUrlInput(event.target.value)}
                            onKeyDown={(event) => {
                              if (
                                event.key === "Enter" &&
                                !event.nativeEvent.isComposing &&
                                !referenceUploadMut.isPending
                              ) {
                                event.preventDefault();
                                addAssetReference();
                              }
                            }}
                            placeholder="asset://asset-..."
                            className="h-11 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] pl-9 pr-3 font-mono text-base text-[var(--fg-0)] outline-none transition-colors focus:border-[var(--accent)]/60 sm:h-10 sm:text-xs"
                          />
                        </div>
                        <Button
                          variant="secondary"
                          size="sm"
                          disabled={
                            referenceUploadMut.isPending ||
                            !assetUrlInput.trim()
                          }
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
                      "min-h-[200px] w-full resize-none overflow-y-hidden bg-transparent px-3 py-3 text-base leading-7 text-[var(--fg-0)] outline-none placeholder:text-[var(--fg-2)] sm:min-h-[320px] sm:px-4 sm:py-4 sm:text-sm lg:min-h-[360px] landscape:max-md:min-h-[150px]",
                      isEnhancingPrompt && "cursor-wait",
                    )}
                  />
                  <div className="border-t border-[var(--border-subtle)] bg-[var(--bg-1)]/62 px-3 py-2.5 sm:px-4">
                    <div className="flex gap-2 overflow-x-auto pb-0.5">
                      {PROMPT_CHIPS.map((chip) => (
                        <button
                          key={chip}
                          type="button"
                          disabled={isEnhancingPrompt || uploadsPending}
                          onClick={() => insertPromptText(chip)}
                          className="min-h-11 shrink-0 rounded-full border border-[var(--border)] bg-[var(--bg-0)] px-3 text-xs text-[var(--fg-1)] transition-colors hover:border-[var(--border-strong)] hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)] disabled:pointer-events-none disabled:opacity-50 sm:min-h-0 sm:py-1.5"
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
            className="scroll-mt-20 pb-[calc(var(--mobile-tabbar-height)+1rem)] md:sticky md:top-[76px] md:pb-0"
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
            onSubmit={submitVideo}
            onModelChange={(value) => {
              abortPromptEnhancement();
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
              setResolution(nextResolution);
              setDurationS((prev) => durationOrPreferred(prev, nextDurations));
            }}
            onDurationChange={(value) => {
              abortPromptEnhancement();
              clearPromptEnhanceChoices();
              setDurationS(Number(value));
            }}
            onResolutionChange={(value) => {
              abortPromptEnhancement();
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
              abortPromptEnhancement();
              clearPromptEnhanceChoices();
              setAspectRatio(value);
            }}
            onSeedChange={setSeed}
            onGenerateAudioChange={(value) => {
              abortPromptEnhancement();
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
        onRetry={(item) => requestVideoRetry(item.id)}
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
          onRetry={() => requestVideoRetry(playbackVideoItem.id)}
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
