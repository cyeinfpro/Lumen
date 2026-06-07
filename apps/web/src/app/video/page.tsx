"use client";

/* eslint-disable @next/next/no-img-element -- Video posters are authenticated API media URLs. */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  Clapperboard,
  CircleCheck,
  Copy,
  Download,
  Film,
  Gauge,
  ImageIcon,
  Layers3,
  PencilLine,
  Play,
  RefreshCw,
  RotateCcw,
  Send,
  Settings2,
  Sparkles,
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
  listVideoGenerations,
  retryVideoGeneration,
  uploadImage,
  uploadVideo,
  videoBinaryUrl,
  videoDownloadUrl,
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
  display: string;
};

type PromptEnhanceCandidate = {
  id: string;
  title: string;
  prompt: string;
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
]);
const ACTIVE_VIDEO_STATUSES = ["queued", "submitting", "submitted", "running"] as const;
const TERMINAL_VIDEO_STATUSES = ["succeeded", "failed", "canceled", "expired"] as const;
const SETTLING_VIDEO_STAGES = ["fetching", "storing", "billing"] as const;
const VIDEO_ACTIVE_POLL_MS = 2500;
const VIDEO_REFRESH_MIN_INTERVAL_MS = 900;
const VIDEO_REFRESH_RETRY_BASE_MS = 1500;
const VIDEO_REFRESH_RETRY_MAX_MS = 15000;
const VIDEO_PROMPT_VARIANT_COUNT = 3;
const VIDEO_PROMPT_VARIANT_TITLES = [
  "推荐镜头版",
  "动作节奏版",
  "参考一致版",
];

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
  storing: {
    label: "保存中",
    detail: "正在保存。",
  },
  billing: {
    label: "结算中",
    detail: "正在结算。",
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

function isActiveVideo(item: VideoGenerationOut): boolean {
  if (ACTIVE_VIDEO_STATUSES.includes(
    item.status as (typeof ACTIVE_VIDEO_STATUSES)[number],
  )) {
    return true;
  }
  if (item.status === "succeeded" && !item.video) return true;
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
  return Number.isSafeInteger(parsed) ? parsed : null;
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
  if (!price) return { tokens, micro: 0 };
  return { tokens, micro: Math.round((tokens * price.price.micro) / 1_000_000) };
}

function videoSrc(video: VideoGenerationWithVideo["video"]): string {
  return video.url?.trim() || videoBinaryUrl(video.id);
}

function videoDownloadSrc(id: string): string {
  return videoDownloadUrl(id);
}

function posterSrc(video: VideoGenerationWithVideo["video"]): string | undefined {
  return video.poster_url?.trim() || undefined;
}

function prewarmVideoItem(item: VideoGenerationWithVideo | null | undefined): void {
  if (!item) return;
  prewarmImage(posterSrc(item.video));
  prewarmVideoMetadata(videoSrc(item.video));
}

function hasVideo(item: VideoGenerationOut): item is VideoGenerationWithVideo {
  return item.video != null;
}

function videoDownloadName(item: VideoGenerationWithVideo): string {
  const ext = item.video.mime === "video/quicktime" ? "mov" : "mp4";
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

function parsePromptEnhanceCandidates(raw: string): PromptEnhanceCandidate[] {
  const normalized = raw.replace(/\r\n/g, "\n").trim();
  if (!normalized) return [];
  const candidates: PromptEnhanceCandidate[] = [];
  const variantPattern =
    /<variant(?:\s+title=(?:"([^"]+)"|'([^']+)'))?\s*>([\s\S]*?)<\/variant>/gi;
  for (const match of normalized.matchAll(variantPattern)) {
    const promptText = cleanPromptEnhanceText(match[3] ?? "");
    if (!promptText) continue;
    const title =
      cleanPromptEnhanceText(match[1] ?? match[2] ?? "") ||
      VIDEO_PROMPT_VARIANT_TITLES[candidates.length] ||
      `方案 ${candidates.length + 1}`;
    candidates.push({
      id: `variant-${candidates.length + 1}`,
      title,
      prompt: promptText,
    });
  }
  if (candidates.length > 0) return candidates.slice(0, VIDEO_PROMPT_VARIANT_COUNT);
  const fallback = cleanPromptEnhanceText(normalized);
  return fallback ? [{ id: "variant-1", title: "优化结果", prompt: fallback }] : [];
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
  const [items, setItems] = useState<VideoGenerationOut[]>([]);
  const [selectedVideoId, setSelectedVideoId] = useState("");
  const [isEnhancingPrompt, setIsEnhancingPrompt] = useState(false);
  const [promptEnhancePreview, setPromptEnhancePreview] = useState("");
  const [promptEnhanceCandidates, setPromptEnhanceCandidates] = useState<
    PromptEnhanceCandidate[]
  >([]);
  const [selectedPromptEnhanceCandidateId, setSelectedPromptEnhanceCandidateId] =
    useState("");

  const optionsQ = useQuery({
    queryKey: ["video", "options"],
    queryFn: getVideoOptions,
    retry: false,
    staleTime: 60_000,
    gcTime: 5 * 60_000,
  });
  const historyQ = useQuery({
    queryKey: ["video", "generations"],
    queryFn: () => listVideoGenerations({ limit: 40 }),
    retry: false,
    placeholderData: (previousData) => previousData,
    staleTime: 20_000,
    gcTime: 5 * 60_000,
  });

  const options = optionsQ.data;
  const effectiveItems = useMemo(
    () => mergeById(historyQ.data?.items ?? [], items),
    [historyQ.data?.items, items],
  );
  const activeItems = useMemo(
    () => effectiveItems.filter(isActiveVideo),
    [effectiveItems],
  );
  const completedVideoItems = useMemo(
    () => effectiveItems.filter(hasVideo),
    [effectiveItems],
  );
  const selectedVideoItem = useMemo(
    () =>
      selectedVideoId
        ? completedVideoItems.find((item) => item.video.id === selectedVideoId)
        : undefined,
    [completedVideoItems, selectedVideoId],
  );
  const primaryVideoItem = selectedVideoItem ?? completedVideoItems[0] ?? null;
  const previewStripItems = completedVideoItems.slice(0, 6);
  const settledHistoryItems = useMemo(
    () => effectiveItems.filter((item) => !isActiveVideo(item)),
    [effectiveItems],
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
    prewarmVideoItem(primaryVideoItem);
  }, [primaryVideoItem]);

  const refreshGeneration = useCallback(
    async (id: string, opts: { forceHistorySync?: boolean } = {}) => {
      const next = await getVideoGeneration(id);
      setItems((prev) => mergeById(prev, [next]));
      if (next.video) {
        setSelectedVideoId(next.video.id);
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
  const estimate = estimateHoldMicro(options, {
    model: selectedModel,
    billingModel: selectedBillingModel,
    action,
    resolution: effectiveResolution,
    durationS,
    referenceHasVideo: referenceMedia.some((item) => item.kind === "video"),
  });
  const nextReferenceLabel = useCallback(
    (kind: "image" | "video") => {
      const count = referenceMedia.filter((item) => item.kind === kind).length + 1;
      return `${kind === "image" ? "图片" : "视频"} ${count}`;
    },
    [referenceMedia],
  );
  const clearPromptEnhanceChoices = useCallback(() => {
    setPromptEnhancePreview("");
    setPromptEnhanceCandidates([]);
    setSelectedPromptEnhanceCandidateId("");
  }, []);

  const insertPromptText = useCallback((text: string) => {
    clearPromptEnhanceChoices();
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
  }, [clearPromptEnhanceChoices, prompt]);

  const insertReferenceTag = useCallback((label: string) => {
    insertPromptText(`[${label}]`);
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
        if (referenceMedia.filter((item) => item.kind === "image").length >= 9) {
          throw new Error("参考图片最多 9 张");
        }
        const img = await uploadImage(file);
        return {
          kind: "image" as const,
          image_id: img.id,
          display: `${img.width}x${img.height}`,
        };
      }
      if (file.type.startsWith("video/")) {
        if (referenceMedia.filter((item) => item.kind === "video").length >= 3) {
          throw new Error("参考视频最多 3 个");
        }
        const video = await uploadVideo(file);
        return {
          kind: "video" as const,
          video_id: video.id,
          display: video.size_bytes ? `${Math.round(video.size_bytes / 1024 / 1024)}MB` : "视频",
        };
      }
      throw new Error("只支持图片或视频");
    },
    onSuccess: (ref) => {
      clearPromptEnhanceChoices();
      const label = nextReferenceLabel(ref.kind);
      setReferenceMedia((prev) => [
        ...prev,
        {
          _key: uuid(),
          kind: ref.kind,
          image_id: ref.kind === "image" ? ref.image_id : null,
          video_id: ref.kind === "video" ? ref.video_id : null,
          label,
          display: ref.display,
        },
      ]);
      toast.success("参考素材已上传");
    },
    onError: (err) => toast.error("上传失败", { description: err instanceof Error ? err.message : undefined }),
  });

  const createMut = useMutation({
    mutationFn: () =>
      createVideoGeneration({
        action,
        model: selectedModel,
        prompt: prompt.trim(),
        input_image_id: action === "i2v" ? inputImageId.trim() : null,
        reference_media:
          action === "reference"
            ? referenceMedia.map((item) => ({
                kind: item.kind,
                image_id: item.kind === "image" ? item.image_id ?? null : null,
                video_id: item.kind === "video" ? item.video_id ?? null : null,
                label: item.label,
              }))
            : [],
        duration_s: durationS,
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
      toast.success("已请求取消");
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
    setPrompt(item.prompt);
    setModel(item.model);
    setDurationS(item.duration_s);
    setResolution(item.resolution);
    setAspectRatio(item.aspect_ratio);
    setGenerateAudio(item.generate_audio);
    setSeed(item.seed != null ? String(item.seed) : "");
    setInputImageId(item.input_image_id ?? "");
    setUploadedLabel(item.input_image_id ? "已从历史任务载入" : "");
    setReferenceMedia(
      item.reference_media.map((ref, index) => {
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
          label,
          display:
            ref.kind === "image"
              ? ref.image_id?.slice(0, 8) ?? "图片"
              : ref.video_id?.slice(0, 8) ?? "视频",
        };
      }),
    );
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
    const current = prompt.trim();
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
          duration_s: durationS,
          resolution: effectiveResolution,
          aspect_ratio: aspectRatio,
          generate_audio: generateAudio,
          input_image_id: action === "i2v" ? inputImageId.trim() || null : null,
          variant_count: VIDEO_PROMPT_VARIANT_COUNT,
          reference_media:
            action === "reference"
              ? referenceMedia.map((item) => ({
                  kind: item.kind,
                  image_id: item.kind === "image" ? item.image_id ?? null : null,
                  video_id: item.kind === "video" ? item.video_id ?? null : null,
                  label: item.label,
                }))
              : [],
        },
        (delta) => {
          if (ctl.signal.aborted || promptEnhanceAbortRef.current !== ctl) return;
          accumulated += delta;
          setPromptEnhancePreview(accumulated);
        },
        ctl.signal,
      );
      const candidates = parsePromptEnhanceCandidates(accumulated);
      const recommended = candidates[0];
      if (recommended) {
        setPrompt(recommended.prompt);
        setPromptEnhanceCandidates(candidates);
        setSelectedPromptEnhanceCandidateId(recommended.id);
        setPromptEnhancePreview("");
        toast.success(
          candidates.length > 1
            ? `已生成 ${candidates.length} 个优化方案`
            : "提示词已优化",
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
          const candidates = parsePromptEnhanceCandidates(accumulated);
          const recommended = candidates[0];
          if (recommended) {
            setPrompt(recommended.prompt);
            setPromptEnhanceCandidates(candidates);
            setSelectedPromptEnhanceCandidateId(recommended.id);
          } else {
            setPrompt(cleanPromptEnhanceText(accumulated));
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
    durationS,
    effectiveResolution,
    generateAudio,
    inputImageId,
    isEnhancingPrompt,
    prompt,
    referenceMedia,
    selectedModel,
  ]);

  const applyPromptEnhanceCandidate = useCallback(
    (candidate: PromptEnhanceCandidate) => {
      setPrompt(candidate.prompt);
      setSelectedPromptEnhanceCandidateId(candidate.id);
      requestAnimationFrame(() => promptRef.current?.focus());
    },
    [],
  );

  const handlePromptChange = useCallback(
    (value: string) => {
      clearPromptEnhanceChoices();
      setPrompt(value);
    },
    [clearPromptEnhanceChoices],
  );

  const submitDisabledReason = useMemo(() => {
    if (createMut.isPending) return "正在提交";
    if (optionsQ.isLoading) return "正在读取配置";
    if (!options?.enabled) return options?.unavailable_reason ?? "功能未启用";
    if (!selectedModel) return "没有可用模型";
    if (!availableResolutions.includes(effectiveResolution)) return "当前模型不支持该分辨率";
    if (!prompt.trim()) return "先填写描述";
    if (action === "i2v" && !inputImageId.trim()) return "需要上传首帧或填写图片 ID";
    if (action === "reference" && referenceMedia.length === 0) {
      return "先添加参考素材";
    }
    if (estimate === null) return "缺少预扣估算";
    return "可以提交";
  }, [
    action,
    availableResolutions,
    createMut.isPending,
    estimate,
    inputImageId,
    options?.enabled,
    options?.unavailable_reason,
    optionsQ.isLoading,
    prompt,
    referenceMedia.length,
    effectiveResolution,
    selectedModel,
  ]);

  const canSubmit =
    Boolean(options?.enabled) &&
    Boolean(selectedModel) &&
    prompt.trim().length > 0 &&
    availableResolutions.includes(effectiveResolution) &&
    (action === "t2v" ||
      (action === "i2v" && inputImageId.trim().length > 0) ||
      (action === "reference" && referenceMedia.length > 0)) &&
    estimate !== null &&
    !createMut.isPending;
  const serviceEnabled = Boolean(options?.enabled);
  const serviceSummary = optionsQ.isLoading
    ? "读取视频服务配置"
    : serviceEnabled
      ? `${availableModels.length} 个模型可用`
      : options?.unavailable_reason ?? "需要先配置可用的视频供应商";

  return (
    <div className="min-h-[100dvh] bg-[var(--bg-0)] text-[var(--fg-0)]">
      <div className="hidden md:block">
        <DesktopTopNav active="video" />
      </div>
      <main className="lumen-studio-bg mx-auto flex w-full max-w-[1440px] flex-col gap-4 px-4 pb-36 pt-3 md:px-6 md:pb-10">
        <VideoWorkbenchHeader
          mode={actionLabel(action)}
          profile={`${effectiveResolution} · ${formatDurationLabel(durationS)}`}
          audio={generateAudio}
          enabled={serviceEnabled}
          loading={optionsQ.isLoading}
          activeCount={activeItems.length}
          completedCount={completedVideoItems.length}
          serviceSummary={serviceSummary}
          submitState={submitDisabledReason}
        />

        <div className="grid gap-4 xl:grid-cols-[minmax(340px,420px)_minmax(0,1fr)] xl:items-start">
          <section className="min-w-0 space-y-4 xl:sticky xl:top-20">
            <Card variant="subtle" elevation={2} padding="none" className="overflow-hidden border-[var(--border)]">
              <div className="relative flex items-start justify-between gap-3 border-b border-[var(--border-subtle)] bg-[var(--bg-1)]/74 p-4 sm:p-5">
                <span aria-hidden="true" className="absolute left-0 top-4 h-8 w-1 rounded-r-full bg-[var(--accent)]" />
                <div>
                  <p className="type-card-title">新建视频</p>
                  <p className="mt-1 text-sm text-[var(--fg-2)]">
                    {serviceEnabled
                      ? `${availableModels.length} 个模型 · ${actionLabel(action)}`
                      : serviceSummary}
                  </p>
                </div>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => {
                    void optionsQ.refetch();
                    void historyQ.refetch();
                  }}
                  leftIcon={<RefreshCw className="h-3.5 w-3.5" />}
                >
                  刷新
                </Button>
              </div>

              <div className="space-y-5 p-4 sm:p-5">
                <ReadinessBanner
                  enabled={serviceEnabled}
                  loading={optionsQ.isLoading}
                  reason={submitDisabledReason}
                  model={selectedModel}
                  profile={`${effectiveResolution} · ${formatDurationLabel(durationS)}`}
                />
                <WorkflowRail
                  action={action}
                  hasPrompt={prompt.trim().length > 0}
                  hasSource={
                    action === "t2v" ||
                    (action === "i2v" && inputImageId.trim().length > 0) ||
                    (action === "reference" && referenceMedia.length > 0)
                  }
                  profile={`${effectiveResolution} · ${formatDurationLabel(durationS)}`}
                />
                <div className="space-y-2">
                  <div className="grid grid-cols-3 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] p-1">
                    {(Object.keys(MODE_COPY) as VideoAction[]).map((key) => (
                      <ModeCard
                        key={key}
                        actionKey={key}
                        selected={action === key}
                        onSelect={() => {
                          clearPromptEnhanceChoices();
                          setAction(key);
                          setModel(firstModelForAction(options, key));
                        }}
                      />
                    ))}
                  </div>
                  <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-[var(--fg-2)]">
                    <span>{MODE_COPY[action].description}</span>
                    <span className="font-medium text-[var(--fg-1)]">{MODE_COPY[action].requirement}</span>
                  </div>
                </div>

                <div className="space-y-3">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="type-caption text-[var(--fg-2)]">描述</span>
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
                    rows={7}
                    maxLength={10000}
                    placeholder="写清主体、动作、画面比例和不要出现的内容。"
                    className={cn(
                      "min-h-[176px] w-full resize-none rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)]/80 p-3 text-sm leading-6 text-[var(--fg-0)] outline-none transition-[border-color,box-shadow] focus:border-[var(--accent)]/60 focus:shadow-[var(--ring)] placeholder:text-[var(--fg-2)]",
                      isEnhancingPrompt && "cursor-wait border-[var(--accent)]/50",
                    )}
                  />
                  {(isEnhancingPrompt ||
                    promptEnhancePreview.trim() ||
                    promptEnhanceCandidates.length > 0) && (
                    <PromptEnhanceChooser
                      loading={isEnhancingPrompt}
                      preview={promptEnhancePreview}
                      candidates={promptEnhanceCandidates}
                      selectedId={selectedPromptEnhanceCandidateId}
                      onSelect={applyPromptEnhanceCandidate}
                      onDismiss={clearPromptEnhanceChoices}
                    />
                  )}
                  <div className="flex flex-wrap gap-2">
                    {PROMPT_CHIPS.map((chip) => (
                      <button
                        key={chip}
                        type="button"
                        disabled={isEnhancingPrompt}
                        onClick={() => insertPromptText(chip)}
                        className="rounded-full border border-[var(--border)] bg-[var(--bg-0)] px-3 py-1.5 text-xs text-[var(--fg-1)] transition-colors hover:border-[var(--border-strong)] hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)] disabled:pointer-events-none disabled:opacity-50"
                      >
                        {chip}
                      </button>
                    ))}
                  </div>
                  <div className="grid gap-2 text-xs text-[var(--fg-2)] sm:grid-cols-3 xl:grid-cols-1 2xl:grid-cols-3">
                    <PromptMeta icon={<Film className="h-3.5 w-3.5" />} label={actionLabel(action)} />
                    <PromptMeta icon={<Layers3 className="h-3.5 w-3.5" />} label={`${referenceMedia.length} 个参考素材`} />
                    <PromptMeta icon={<Settings2 className="h-3.5 w-3.5" />} label={`${effectiveResolution} · ${formatDurationLabel(durationS)}`} />
                  </div>
                </div>

                {action === "i2v" && (
                  <div className="space-y-3 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-1)]/78 p-3 shadow-[var(--shadow-1)]">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div>
                        <p className="text-sm font-medium text-[var(--fg-0)]">首帧图片</p>
                        <p className="text-xs text-[var(--fg-2)]">上传首帧，或粘贴已有图片 ID。</p>
                      </div>
                      <input
                        ref={fileRef}
                        type="file"
                        accept="image/png,image/jpeg,image/webp"
                        className="hidden"
                        onChange={(event) => {
                          const file = event.target.files?.[0];
                          if (file) uploadMut.mutate(file);
                          event.target.value = "";
                        }}
                      />
                      <Button
                        variant="outline"
                        size="sm"
                        loading={uploadMut.isPending}
                        onClick={() => fileRef.current?.click()}
                        leftIcon={<Upload className="h-3.5 w-3.5" />}
                      >
                        上传
                      </Button>
                    </div>
                    <input
                      value={inputImageId}
                      onChange={(event) => {
                        clearPromptEnhanceChoices();
                        setInputImageId(event.target.value);
                        setUploadedLabel("");
                      }}
                      placeholder="image_id"
                      className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-1)] px-3 font-mono text-xs text-[var(--fg-0)] outline-none focus:border-[var(--accent)]/50"
                    />
                    <div className="rounded-[var(--radius-control)] border border-dashed border-[var(--border)] bg-[var(--bg-1)]/70 px-3 py-2 text-xs text-[var(--fg-2)]">
                      {uploadedLabel || inputImageId ? uploadedLabel || "已填写图片 ID" : "用于确定第一帧构图。"}
                    </div>
                  </div>
                )}

                {action === "reference" && (
                  <div className="space-y-3 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-1)]/78 p-3 shadow-[var(--shadow-1)]">
                    <input
                      ref={referenceFileRef}
                      type="file"
                      accept="image/png,image/jpeg,image/webp,video/mp4,video/quicktime"
                      className="hidden"
                      onChange={(event) => {
                        const file = event.target.files?.[0];
                        if (file) referenceUploadMut.mutate(file);
                        event.target.value = "";
                      }}
                    />
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div>
                        <p className="text-sm font-medium text-[var(--fg-0)]">参考素材</p>
                        <p className="text-xs text-[var(--fg-2)]">点击素材标签可插入描述。</p>
                      </div>
                      <Button
                        variant="outline"
                        size="sm"
                        loading={referenceUploadMut.isPending}
                        onClick={() => referenceFileRef.current?.click()}
                        leftIcon={<Upload className="h-3.5 w-3.5" />}
                      >
                        上传
                      </Button>
                    </div>
                    <div className="flex flex-wrap gap-2">
                      {referenceMedia.map((item) => (
                        <ReferenceChip
                          key={item._key}
                          item={item}
                          onInsert={() => insertReferenceTag(item.label)}
                          onRemove={() => {
                            clearPromptEnhanceChoices();
                            setReferenceMedia((prev) =>
                              prev.filter((ref) => ref._key !== item._key),
                            );
                          }}
                        />
                      ))}
                      {referenceMedia.length === 0 && (
                        <span className="rounded-[var(--radius-control)] border border-dashed border-[var(--border)] bg-[var(--bg-1)]/70 px-3 py-2 text-xs text-[var(--fg-2)]">
                          未添加参考素材
                        </span>
                      )}
                    </div>
                  </div>
                )}

                <div className="space-y-3 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-1)]/78 p-3 shadow-[var(--shadow-1)]">
                  <div className="flex items-center gap-2">
                    <Settings2 className="h-4 w-4 text-[var(--fg-2)]" />
                    <p className="text-sm font-medium text-[var(--fg-0)]">参数</p>
                  </div>
                  <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-1 2xl:grid-cols-2">
                    <SelectField
                      label="模型"
                      value={selectedModel}
                      onChange={(value) => {
                        clearPromptEnhanceChoices();
                        setModel(value);
                      }}
                      options={availableModels.map((item) => item.model)}
                    />
                    <SelectField
                      label="时长"
                      value={String(durationS)}
                      onChange={(value) => {
                        clearPromptEnhanceChoices();
                        setDurationS(Number(value));
                      }}
                      options={(options?.durations_s ?? VIDEO_DURATION_OPTIONS).map(String)}
                      renderOption={(value) => formatDurationLabel(Number(value))}
                    />
                    <SelectField
                      label="分辨率"
                      value={effectiveResolution}
                      onChange={(value) => {
                        clearPromptEnhanceChoices();
                        setResolution(value);
                      }}
                      options={availableResolutions}
                    />
                    <SelectField
                      label="比例"
                      value={aspectRatio}
                      onChange={(value) => {
                        clearPromptEnhanceChoices();
                        setAspectRatio(value);
                      }}
                      options={options?.aspect_ratios ?? ["adaptive", "16:9", "9:16", "1:1"]}
                    />
                  </div>
                  <div className="grid gap-3 sm:grid-cols-[1fr_auto] xl:grid-cols-1 2xl:grid-cols-[1fr_auto]">
                    <label className="space-y-1.5">
                      <span className="type-caption text-[var(--fg-2)]">种子</span>
                      <input
                        value={seed}
                        onChange={(event) => setSeed(event.target.value)}
                        inputMode="numeric"
                        placeholder="随机"
                        className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-1)] px-3 font-mono text-xs text-[var(--fg-0)] outline-none focus:border-[var(--accent)]/50"
                      />
                    </label>
                    <label className="flex min-h-10 items-center justify-between gap-4 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-1)] px-3 text-sm">
                      <span>生成音频</span>
                      <input
                        type="checkbox"
                        checked={generateAudio}
                        onChange={(event) => {
                          clearPromptEnhanceChoices();
                          setGenerateAudio(event.target.checked);
                        }}
                      />
                    </label>
                  </div>
                </div>

                <div className="hidden md:block">
                  <SubmitPanel
                    estimate={estimate}
                    canSubmit={canSubmit}
                    reason={submitDisabledReason}
                    loading={createMut.isPending}
                    onSubmit={() => createMut.mutate()}
                  />
                </div>
                <div className="sticky bottom-16 z-20 md:hidden">
                  <SubmitPanel
                    estimate={estimate}
                    canSubmit={canSubmit}
                    reason={submitDisabledReason}
                    loading={createMut.isPending}
                    onSubmit={() => createMut.mutate()}
                    compact
                  />
                </div>
              </div>
            </Card>
          </section>

          <section className="min-w-0 space-y-4">
            <Card variant="subtle" elevation={2} padding="none" className="overflow-hidden border-[var(--border)]">
              <div className="relative flex flex-wrap items-center justify-between gap-3 border-b border-[var(--border-subtle)] bg-[var(--bg-1)]/74 p-4 sm:p-5">
                <span aria-hidden="true" className="absolute left-0 top-4 h-8 w-1 rounded-r-full bg-[var(--accent)]" />
                <div>
                  <p className="type-card-title">预览</p>
                  <p className="mt-1 text-sm text-[var(--fg-2)]">
                    {primaryVideoItem
                      ? `${actionLabel(primaryVideoItem.action)} · ${primaryVideoItem.resolution} · ${formatDurationLabel(primaryVideoItem.duration_s)}`
                      : "等待结果"}
                  </p>
                </div>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => void historyQ.refetch()}
                  leftIcon={<RefreshCw className="h-3.5 w-3.5" />}
                >
                  刷新
                </Button>
              </div>
              <div className="space-y-4 p-4 sm:p-5 lg:p-6">
                <PrimaryPreview
                  item={primaryVideoItem}
                  onUseDraft={primaryVideoItem ? () => loadAsDraft(primaryVideoItem) : undefined}
                  onRetry={primaryVideoItem ? () => retryMut.mutate(primaryVideoItem.id) : undefined}
                  onCopy={primaryVideoItem ? () => {
                    void navigator.clipboard?.writeText(primaryVideoItem.prompt);
                    toast.success("描述已复制");
                  } : undefined}
                  onDelete={primaryVideoItem?.video ? () => deleteMut.mutate(primaryVideoItem.video.id) : undefined}
                />
                {previewStripItems.length > 1 && (
                  <div className="space-y-2 border-t border-[var(--border-subtle)] pt-4">
                    <div className="flex items-center justify-between gap-3">
                      <p className="type-caption text-[var(--fg-2)]">最近</p>
                      <span className="text-xs tabular-nums text-[var(--fg-2)]">
                        {completedVideoItems.length} 个
                      </span>
                    </div>
                    <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4">
                      {previewStripItems.map((item) => (
                        <VideoStripItem
                          key={item.id}
                          item={item}
                          selected={primaryVideoItem?.video.id === item.video.id}
                          onPreview={() => setSelectedVideoId(item.video.id)}
                          onUseDraft={() => loadAsDraft(item)}
                        />
                      ))}
                    </div>
                  </div>
                )}
              </div>
            </Card>

            <Card variant="subtle" elevation={2} padding="none" className="overflow-hidden border-[var(--border)]">
              <div className="relative flex flex-wrap items-center justify-between gap-3 border-b border-[var(--border-subtle)] bg-[var(--bg-1)]/74 p-4 sm:p-5">
                <span aria-hidden="true" className="absolute left-0 top-4 h-8 w-1 rounded-r-full bg-[var(--accent)]" />
                <div className="flex items-center gap-2">
                  <Clapperboard className="h-4 w-4 text-[var(--fg-2)]" />
                  <p className="type-card-title">记录</p>
                </div>
                <div className="flex flex-wrap items-center gap-2 text-xs text-[var(--fg-2)]">
                  <span className="rounded-full border border-[var(--border)] bg-[var(--bg-0)] px-2 py-1 tabular-nums">
                    {activeItems.length} 活跃
                  </span>
                  <span className="rounded-full border border-[var(--border)] bg-[var(--bg-0)] px-2 py-1 tabular-nums">
                    {historyQ.isLoading ? "读取中" : `${settledHistoryItems.length} 历史`}
                  </span>
                </div>
              </div>
              <div className="space-y-5 p-4 sm:p-5">
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
                      {historyQ.isLoading ? "读取中" : `${settledHistoryItems.length} 条`}
                    </span>
                  </div>
                  <div className="grid gap-3">
                    {settledHistoryItems.map((item) => (
                      <TaskRow
                        key={item.id}
                        item={item}
                        onCancel={() => cancelMut.mutate(item.id)}
                        onRetry={() => retryMut.mutate(item.id)}
                        onCopy={() => {
                          void navigator.clipboard?.writeText(item.prompt);
                          toast.success("描述已复制");
                        }}
                        onUseDraft={() => loadAsDraft(item)}
                        onDelete={() => item.video && deleteMut.mutate(item.video.id)}
                        onPreview={hasVideo(item) ? () => setSelectedVideoId(item.video.id) : undefined}
                        selected={primaryVideoItem?.video.id === item.video?.id}
                        showPreview={false}
                      />
                    ))}
                    {settledHistoryItems.length === 0 && (
                      <EmptyPanel
                        icon={<Film className="h-5 w-5" />}
                        title={historyQ.isLoading ? "读取中" : "暂无历史"}
                        description={
                          activeItems.length > 0
                            ? "当前任务完成后会进入历史。"
                            : "提交记录会保留状态、参数和结果。"
                        }
                      />
                    )}
                  </div>
                </section>
              </div>
            </Card>
          </section>
        </div>
      </main>
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
    <label className="space-y-1.5">
      <span className="type-caption text-[var(--fg-2)]">{label}</span>
      <select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm outline-none focus:border-[var(--accent)]/50"
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

function WorkflowRail({
  action,
  hasPrompt,
  hasSource,
  profile,
}: {
  action: VideoAction;
  hasPrompt: boolean;
  hasSource: boolean;
  profile: string;
}) {
  return (
    <div className="grid grid-cols-3 gap-2 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-1)]/72 p-2 shadow-[var(--shadow-1)] xl:grid-cols-1 2xl:grid-cols-3">
      <WorkflowStep
        label="描述"
        value={hasPrompt ? "已填写" : "待输入"}
        active={!hasPrompt}
        done={hasPrompt}
        icon={<PencilLine className="h-3.5 w-3.5" />}
      />
      <WorkflowStep
        label="素材"
        value={action === "t2v" ? "纯文本" : hasSource ? "已连接素材" : MODE_COPY[action].requirement}
        active={hasPrompt && !hasSource}
        done={hasSource}
        icon={<Layers3 className="h-3.5 w-3.5" />}
      />
      <WorkflowStep
        label="参数"
        value={profile}
        active={hasPrompt && hasSource}
        done={hasPrompt && hasSource}
        icon={<Settings2 className="h-3.5 w-3.5" />}
      />
    </div>
  );
}

function WorkflowStep({
  label,
  value,
  icon,
  active,
  done,
}: {
  label: string;
  value: string;
  icon: React.ReactNode;
  active: boolean;
  done: boolean;
}) {
  return (
    <div
      className={cn(
        "relative min-w-0 overflow-hidden rounded-[var(--radius-control)] border px-2 py-2 transition-colors sm:px-3",
        done || active
          ? "border-[var(--border-strong)] bg-[var(--bg-0)] shadow-[var(--shadow-1)]"
          : "border-[var(--border-subtle)] bg-[var(--bg-1)]/70",
      )}
    >
      {(done || active) && (
        <span
          aria-hidden="true"
          className={cn(
            "absolute inset-y-2 left-0 w-0.5 rounded-r-full",
            active ? "bg-[var(--accent)]" : "bg-[var(--border-strong)]",
          )}
        />
      )}
      <div className="flex min-w-0 items-center gap-2">
        <span
          className={cn(
            "shrink-0",
            active ? "text-[var(--accent)]" : done ? "text-[var(--fg-0)]" : "text-[var(--fg-2)]",
          )}
        >
          {icon}
        </span>
        <span className="type-caption truncate text-[var(--fg-2)]">{label}</span>
      </div>
      <p className="mt-1 truncate text-xs font-medium text-[var(--fg-0)]">{value}</p>
    </div>
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
    <section className="grid gap-4 border-b border-[var(--border)] pb-4 lg:grid-cols-[minmax(0,1fr)_minmax(560px,0.9fr)] lg:items-end">
      <div className="min-w-0">
        <div className="inline-flex max-w-full items-center gap-2 rounded-full border border-[var(--border)] bg-[var(--bg-1)]/72 px-3 py-1.5 text-xs font-medium text-[var(--fg-1)] shadow-[var(--shadow-1)]">
          <Sparkles className="h-3.5 w-3.5 shrink-0 text-[var(--accent)]" />
          <span className="truncate">Lumen 视频工作台</span>
        </div>
        <div className="mt-3 flex flex-wrap items-end gap-x-3 gap-y-2">
          <h1 className="type-page-title">视频工作台</h1>
          <span className="rounded-full border border-[var(--border)] bg-[var(--bg-1)]/72 px-2.5 py-1 text-xs text-[var(--fg-2)]">
            {submitState}
          </span>
        </div>
        <p className="mt-2 max-w-2xl text-sm leading-6 text-[var(--fg-1)]">
          用同一个工作台完成文字生成、首帧生成和多参考生成；左侧控制描述与参数，右侧保留预览、队列和历史。
        </p>
      </div>
      <div className="grid gap-2 sm:grid-cols-3">
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

function ReadinessBanner({
  enabled,
  loading,
  reason,
  model,
  profile,
}: {
  enabled: boolean;
  loading: boolean;
  reason: string;
  model: string;
  profile: string;
}) {
  const ready = enabled && reason === "可以提交";
  const icon = loading ? (
    <RefreshCw className="h-4 w-4 animate-spin" />
  ) : ready ? (
    <CircleCheck className="h-4 w-4" />
  ) : (
    <AlertCircle className="h-4 w-4" />
  );
  return (
    <div
      className={cn(
        "grid gap-3 rounded-[var(--radius-card)] border p-3 shadow-[var(--shadow-1)] sm:grid-cols-[auto_minmax(0,1fr)_auto] sm:items-center",
        ready
          ? "border-success-border bg-success-soft"
          : enabled
            ? "border-[var(--border)] bg-[var(--bg-1)]/78"
            : "border-warning-border bg-warning-soft",
      )}
    >
      <span
        className={cn(
          "flex h-9 w-9 items-center justify-center rounded-[var(--radius-control)] border bg-[var(--bg-0)]",
          ready
            ? "border-success-border text-[var(--success-fg)]"
            : enabled
              ? "border-[var(--border)] text-[var(--fg-1)]"
              : "border-warning-border text-[var(--warning-fg)]",
        )}
      >
        {icon}
      </span>
      <div className="min-w-0">
        <p className="text-sm font-semibold text-[var(--fg-0)]">
          {loading ? "正在读取配置" : ready ? "任务已准备好" : reason}
        </p>
        <p className="mt-0.5 truncate text-xs text-[var(--fg-2)]">
          {enabled
            ? `${model || "未选择模型"} · ${profile}`
            : "配置视频供应商后，提交按钮会自动进入可用状态。"}
        </p>
      </div>
      <span className="hidden rounded-full border border-[var(--border)] bg-[var(--bg-0)] px-2.5 py-1 text-xs text-[var(--fg-2)] sm:inline-flex">
        {ready ? "就绪" : enabled ? "草稿" : "配置"}
      </span>
    </div>
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
        "relative min-w-0 overflow-hidden rounded-[var(--radius-control)] border px-3 py-2.5",
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
      <div className="flex min-w-0 items-start gap-2.5">
        <span
          className={cn(
            "mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-[var(--radius-control)] border",
            active
              ? "border-[var(--accent-border)] bg-[var(--bg-0)] text-[var(--accent)]"
              : "border-[var(--border-subtle)] bg-[var(--bg-1)] text-[var(--fg-2)]",
          )}
        >
          {icon}
        </span>
        <span className="min-w-0">
          <span className="type-caption block text-[var(--fg-2)]">{label}</span>
          <span className="mt-0.5 block truncate text-sm font-semibold text-[var(--fg-0)]">
            {value}
          </span>
          <span className="mt-0.5 block truncate text-xs text-[var(--fg-2)]">
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
        "group relative min-h-[88px] overflow-hidden rounded-[var(--radius-control)] border px-3 py-2.5 text-left transition-[background-color,border-color,color,transform] duration-200",
        selected
          ? "border-[var(--accent-border)] bg-[var(--accent-soft)] text-[var(--fg-0)] shadow-[var(--shadow-1)]"
          : "border-transparent text-[var(--fg-1)] hover:border-[var(--border)] hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)]",
      )}
    >
      <span
        aria-hidden="true"
        className={cn(
          "absolute inset-x-3 bottom-0 h-0.5 rounded-t-full transition-colors",
          selected ? "bg-[var(--accent)]" : "bg-transparent",
        )}
      />
      <div className="flex items-start justify-between gap-2">
        <span
          className={cn(
            "flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-control)] border",
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
      <p className="mt-3 text-sm font-semibold text-[var(--fg-0)]">
        {copy.title}
      </p>
      <p className="mt-1 truncate text-[11px] font-medium text-[var(--fg-2)]">
        {copy.eyebrow}
      </p>
    </button>
  );
}

function PromptMeta({ icon, label }: { icon: React.ReactNode; label: string }) {
  return (
    <span className="inline-flex min-h-8 items-center gap-2 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-2.5">
      <span className="text-[var(--fg-2)]">{icon}</span>
      <span className="truncate">{label}</span>
    </span>
  );
}

function PromptEnhanceChooser({
  loading,
  preview,
  candidates,
  selectedId,
  onSelect,
  onDismiss,
}: {
  loading: boolean;
  preview: string;
  candidates: PromptEnhanceCandidate[];
  selectedId: string;
  onSelect: (candidate: PromptEnhanceCandidate) => void;
  onDismiss: () => void;
}) {
  const cleanPreview = cleanPromptEnhanceText(preview);
  const visibleCandidates = candidates.length > 0 ? candidates : [];

  const copyCandidate = async (candidate: PromptEnhanceCandidate) => {
    try {
      await navigator.clipboard.writeText(candidate.prompt);
      toast.success("已复制提示词");
    } catch {
      toast.error("复制失败");
    }
  };

  return (
    <div className="space-y-2 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-1)]/78 p-3 shadow-[var(--shadow-1)]">
      <div className="flex items-center justify-between gap-2">
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
                ? `${visibleCandidates.length} 个候选，已应用推荐版`
                : loading
                  ? "优先补运动、运镜和时间推进"
                  : "已应用到描述"}
            </span>
          </span>
        </div>
        {!loading && (
          <Button
            variant="ghost"
            size="sm"
            onClick={onDismiss}
            leftIcon={<XCircle className="h-3.5 w-3.5" />}
          >
            清除
          </Button>
        )}
      </div>

      {loading && (
        <div className="min-h-20 rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/72 p-3 text-sm leading-6 text-[var(--fg-1)]">
          {cleanPreview || "等待上游返回..."}
        </div>
      )}

      {visibleCandidates.length > 0 && (
        <div className="grid gap-2">
          {visibleCandidates.map((candidate) => {
            const selected = candidate.id === selectedId;
            return (
              <div
                key={candidate.id}
                className={cn(
                  "rounded-[var(--radius-control)] border bg-[var(--bg-0)] p-3 transition-colors",
                  selected
                    ? "border-[var(--accent-border)] shadow-[var(--shadow-1)]"
                    : "border-[var(--border-subtle)]",
                )}
              >
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
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
                      <p className="truncate text-sm font-semibold text-[var(--fg-0)]">
                        {candidate.title}
                      </p>
                    </div>
                    <p className="mt-2 max-h-28 overflow-y-auto text-sm leading-6 text-[var(--fg-1)]">
                      {candidate.prompt}
                    </p>
                  </div>
                  <div className="flex shrink-0 items-center gap-1">
                    <Button
                      variant={selected ? "secondary" : "outline"}
                      size="sm"
                      disabled={selected}
                      onClick={() => onSelect(candidate)}
                    >
                      {selected ? "已用" : "使用"}
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-9 w-9 px-0"
                      onClick={() => void copyCandidate(candidate)}
                      aria-label="复制优化提示词"
                    >
                      <Copy className="h-3.5 w-3.5" />
                    </Button>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function ReferenceChip({
  item,
  onInsert,
  onRemove,
}: {
  item: ReferenceDraft;
  onInsert: () => void;
  onRemove: () => void;
}) {
  return (
    <div className="inline-flex min-h-10 max-w-full items-center gap-2 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-1)] px-2 text-xs text-[var(--fg-1)]">
      <button
        type="button"
        onClick={onInsert}
        className="inline-flex min-w-0 items-center gap-2 rounded-[var(--radius-control)] px-1 py-1 text-left transition-colors hover:bg-[var(--bg-2)]"
      >
        {item.kind === "image" ? (
          <ImageIcon className="h-3.5 w-3.5 shrink-0" />
        ) : (
          <VideoIcon className="h-3.5 w-3.5 shrink-0" />
        )}
        <span className="shrink-0">[{item.label}]</span>
        <span className="truncate text-[var(--fg-2)]">{item.display}</span>
      </button>
      <button
        type="button"
        aria-label="移除参考素材"
        onClick={onRemove}
        className="shrink-0 rounded-full p-0.5 text-[var(--fg-2)] transition-colors hover:bg-[var(--bg-3)] hover:text-[var(--fg-0)]"
      >
        <XCircle className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}

function SubmitPanel({
  estimate,
  canSubmit,
  reason,
  loading,
  onSubmit,
  compact = false,
}: {
  estimate: { tokens: number; micro: number } | null;
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
        compact ? "p-3" : "p-4",
      )}
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="grid min-w-0 flex-1 grid-cols-2 gap-3">
          <div>
            <p className="type-caption text-[var(--fg-2)]">预扣</p>
            <p className="text-base font-semibold tabular-nums text-[var(--fg-0)]">
              {estimate ? formatRmb(estimate.micro / 1_000_000) : "-"}
            </p>
          </div>
          <div>
            <p className="type-caption text-[var(--fg-2)]">Token 上限</p>
            <p className="text-base font-semibold tabular-nums text-[var(--fg-0)]">
              {estimate ? estimate.tokens.toLocaleString() : "-"}
            </p>
          </div>
        </div>
        <Button
          variant="primary"
          size={compact ? "sm" : "md"}
          disabled={!canSubmit}
          loading={loading}
          onClick={onSubmit}
          leftIcon={<Send className="h-4 w-4" />}
        >
          提交
        </Button>
      </div>
      <p
        className={cn(
          "mt-2 text-xs",
          canSubmit ? "text-success" : "text-[var(--fg-2)]",
        )}
      >
        {reason}
      </p>
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
  item: VideoGenerationWithVideo;
  fullWidth?: boolean;
}) {
  return (
    <a
      href={videoDownloadSrc(item.video.id)}
      download={videoDownloadName(item)}
      className={cn(
        "inline-flex h-9 items-center justify-center gap-1.5 rounded-[var(--radius-control)] border border-[var(--border)] bg-transparent px-3 text-xs font-medium leading-tight text-[var(--fg-0)] transition-[background-color,border-color,color] hover:border-[var(--border-strong)] hover:bg-[var(--bg-2)]",
        fullWidth && "w-full",
      )}
    >
      <Download className="h-3.5 w-3.5 shrink-0" />
      下载
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

function PrimaryVideoPlayer({ item }: { item: VideoGenerationWithVideo }) {
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
    <div className="relative overflow-hidden rounded-[var(--radius-panel)] border border-[var(--border-strong)] bg-[var(--bg-2)] shadow-[var(--shadow-2)]">
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
        className="aspect-video w-full bg-[var(--bg-2)] object-contain"
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

function PrimaryPreview({
  item,
  onUseDraft,
  onRetry,
  onCopy,
  onDelete,
}: {
  item: VideoGenerationWithVideo | null;
  onUseDraft?: () => void;
  onRetry?: () => void;
  onCopy?: () => void;
  onDelete?: () => void;
}) {
  if (!item) {
    return (
      <div className="overflow-hidden rounded-[var(--radius-panel)] border border-[var(--border-strong)] bg-[var(--bg-1)] shadow-[var(--shadow-2)]">
        <div className="flex items-center justify-between gap-3 border-b border-[var(--border-subtle)] bg-[var(--bg-1)] px-3 py-2">
          <div className="flex min-w-0 items-center gap-2">
            <span className="h-2.5 w-2.5 rounded-full bg-[var(--accent)]" />
            <span className="truncate text-xs font-medium text-[var(--fg-1)]">预览</span>
          </div>
          <span className="text-[11px] tabular-nums text-[var(--fg-2)]">16:9</span>
        </div>
        <div className="relative grid min-h-[380px] place-items-center overflow-hidden bg-[var(--bg-2)]/60 p-4 text-[var(--fg-0)] sm:p-6">
          <div aria-hidden="true" className="absolute inset-4 rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/32" />
          <div aria-hidden="true" className="absolute inset-x-8 top-8 h-px bg-[var(--border-subtle)]" />
          <div aria-hidden="true" className="absolute inset-x-8 bottom-8 h-px bg-[var(--border-subtle)]" />
          <div className="relative w-full max-w-xl text-center">
            <div className="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-[var(--radius-card)] border border-[var(--accent-border)] bg-[var(--bg-0)] text-[var(--accent)] shadow-[var(--shadow-1)]">
              <Clapperboard className="h-6 w-6" />
            </div>
            <p className="text-base font-semibold">等待第一条视频任务</p>
            <p className="mx-auto mt-2 max-w-md text-sm leading-6 text-[var(--fg-2)]">
              填写描述并提交后，这里会先显示任务状态，再切换为可播放预览，历史记录会保留同一组参数。
            </p>
            <div className="mt-5 grid gap-2 text-left sm:grid-cols-3">
              <PreviewStep
                label="描述"
                detail="主体、镜头、运动"
                icon={<PencilLine className="h-3.5 w-3.5" />}
              />
              <PreviewStep
                label="提交"
                detail="预扣与队列状态"
                icon={<Send className="h-3.5 w-3.5" />}
              />
              <PreviewStep
                label="复用"
                detail="下载或套用参数"
                icon={<RotateCcw className="h-3.5 w-3.5" />}
              />
            </div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <PrimaryVideoPlayer item={item} />
      <div className="grid gap-4 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-1)]/78 p-3 shadow-[var(--shadow-1)] xl:grid-cols-[minmax(0,1fr)_auto]">
        <div className="min-w-0">
          <div className="mb-2 flex flex-wrap gap-2">
            <StatusPill item={item} />
            <span className="rounded-full border border-[var(--border)] bg-[var(--bg-0)] px-2 py-1 text-xs text-[var(--fg-2)]">
              {actionLabel(item.action)} · {item.resolution} · {formatDurationLabel(item.duration_s)}
            </span>
          </div>
          <p className="line-clamp-3 text-sm leading-6 text-[var(--fg-0)]">{item.prompt}</p>
        </div>
        <div className="flex flex-wrap items-start gap-2 xl:justify-end">
          <VideoDownloadLink item={item} />
          {onUseDraft && (
            <Button
              variant="secondary"
              size="sm"
              onClick={onUseDraft}
              leftIcon={<RotateCcw className="h-3.5 w-3.5" />}
            >
              套用参数
            </Button>
          )}
          {onRetry && (
            <Button
              variant="outline"
              size="sm"
              onClick={onRetry}
              leftIcon={<Play className="h-3.5 w-3.5" />}
            >
              重新生成
            </Button>
          )}
          {onCopy && (
            <Button
              variant="outline"
              size="sm"
              onClick={onCopy}
              leftIcon={<Copy className="h-3.5 w-3.5" />}
            >
              复制
            </Button>
          )}
          {onDelete && (
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
      </div>
    </div>
  );
}

function PreviewStep({
  label,
  detail,
  icon,
}: {
  label: string;
  detail: string;
  icon: React.ReactNode;
}) {
  return (
    <div className="rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-1)]/78 p-3 shadow-[var(--shadow-1)]">
      <div className="flex items-center gap-2 text-[var(--fg-1)]">
        <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] text-[var(--accent)]">
          {icon}
        </span>
        <span className="text-sm font-semibold text-[var(--fg-0)]">{label}</span>
      </div>
      <p className="mt-2 text-xs leading-5 text-[var(--fg-2)]">{detail}</p>
    </div>
  );
}

function VideoStripItem({
  item,
  selected,
  onPreview,
  onUseDraft,
}: {
  item: VideoGenerationWithVideo;
  selected: boolean;
  onPreview: () => void;
  onUseDraft: () => void;
}) {
  return (
    <article
      className={cn(
        "relative grid grid-cols-[112px_minmax(0,1fr)] gap-3 overflow-hidden rounded-[var(--radius-card)] border p-2 transition-colors hover:border-[var(--border)] max-sm:grid-cols-[96px_minmax(0,1fr)]",
        selected
          ? "border-[var(--accent-border)] bg-[var(--accent-soft)] shadow-[var(--shadow-1)]"
          : "border-[var(--border-subtle)] bg-[var(--bg-0)]/60",
      )}
    >
      {selected && (
        <span aria-hidden="true" className="absolute inset-y-3 left-0 w-1 rounded-r-full bg-[var(--accent)]" />
      )}
      <VideoPosterButton
        item={item}
        selected={selected}
        onPreview={onPreview}
        compact
      />
      <div className="flex min-w-0 flex-col justify-between gap-2 py-0.5">
        <div className="min-w-0">
          <div className="mb-1 flex flex-wrap items-center gap-1.5">
            <StatusPill item={item} />
            <span className="rounded-full border border-[var(--border)] bg-[var(--bg-1)] px-2 py-1 text-[11px] text-[var(--fg-2)]">
              {formatDurationLabel(item.duration_s)}
            </span>
          </div>
          <p className="line-clamp-2 text-xs leading-5 text-[var(--fg-1)]">{item.prompt}</p>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={onUseDraft}
          leftIcon={<RotateCcw className="h-3.5 w-3.5" />}
          className="self-start"
        >
          套用
        </Button>
      </div>
    </article>
  );
}

function TaskRow({
  item,
  onCancel,
  onRetry,
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
        <Button
          variant="outline"
          size="sm"
          onClick={onRetry}
          leftIcon={<Play className="h-3.5 w-3.5" />}
        >
          重新生成
        </Button>
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
        {videoItem && <VideoDownloadLink item={videoItem} />}
        {onUseDraft && (
          <Button
            variant="outline"
            size="sm"
            onClick={onUseDraft}
            leftIcon={<RotateCcw className="h-3.5 w-3.5" />}
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
