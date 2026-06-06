"use client";

/* eslint-disable @next/next/no-img-element -- Video posters are authenticated API media URLs. */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Clapperboard,
  Copy,
  Download,
  Film,
  ImageIcon,
  Layers3,
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
  ...Array.from({ length: 12 }, (_, index) => index + 4),
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
  return durationS === SMART_VIDEO_DURATION ? "智能时长" : `${durationS}s`;
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
    action,
    resolution,
    durationS,
    referenceHasVideo,
  }: {
    model: string;
    action: VideoAction;
    resolution: string;
    durationS: number;
    referenceHasVideo?: boolean;
  },
): { tokens: number; micro: number } | null {
  const tokenMap = options?.hold_estimates?.[model];
  if (!tokenMap || typeof tokenMap !== "object") return null;
  const tokenRecord = tokenMap as Record<string, unknown>;
  const actionMap =
    tokenRecord[action] ??
    (action === "reference" ? tokenRecord.i2v ?? tokenRecord.t2v : undefined);
  if (!actionMap || typeof actionMap !== "object") return null;
  const tokensRaw = (actionMap as Record<string, unknown>)[
    `${resolution}:${holdEstimateDurationS(durationS)}`
  ];
  const tokens = Number(tokensRaw);
  if (!Number.isFinite(tokens) || tokens <= 0) return null;
  const pricingAction =
    action === "reference"
      ? referenceHasVideo
        ? "reference_video"
        : "reference_image"
      : action;
  const findPrice = (priceAction: VideoAction | "reference_image" | "reference_video") =>
    options?.pricing.find(
      (item) =>
        item.model === model &&
        item.action === priceAction &&
        item.resolution === resolution &&
        item.enabled,
    ) ??
    options?.pricing.find(
      (item) =>
        item.model === model &&
        item.action === priceAction &&
        (item.resolution == null || item.resolution === "") &&
        item.enabled,
    );
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
  const recentVideoItems = completedVideoItems.slice(0, 4);
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
  const availableResolutions = useMemo(
    () => resolutionOptionsForModel(options, selectedModel),
    [options, selectedModel],
  );
  const effectiveResolution = availableResolutions.includes(resolution)
    ? resolution
    : preferredResolution(availableResolutions);
  const estimate = estimateHoldMicro(options, {
    model: selectedModel,
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

  const insertPromptText = useCallback((text: string) => {
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
  }, [prompt]);

  const insertReferenceTag = useCallback((label: string) => {
    insertPromptText(`[${label}]`);
  }, [insertPromptText]);

  const uploadMut = useMutation({
    mutationFn: (file: File) => uploadImage(file),
    onSuccess: (img) => {
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
  }, []);

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
          setPrompt(accumulated);
        },
        ctl.signal,
      );
      toast.success("提示词已优化");
    } catch (err) {
      if (!ctl.signal.aborted) {
        const description = err instanceof Error ? err.message : undefined;
        if (accumulated.trim()) {
          setPrompt(accumulated);
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
    durationS,
    effectiveResolution,
    generateAudio,
    inputImageId,
    isEnhancingPrompt,
    prompt,
    referenceMedia,
    selectedModel,
  ]);

  const submitDisabledReason = useMemo(() => {
    if (createMut.isPending) return "正在提交";
    if (optionsQ.isLoading) return "正在读取配置";
    if (!options?.enabled) return options?.unavailable_reason ?? "视频生成未启用";
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

  return (
    <div className="min-h-[100dvh] bg-[var(--bg-0)] text-[var(--fg-0)]">
      <div className="hidden md:block">
        <DesktopTopNav active="video" />
      </div>
      <main className="mx-auto flex w-full max-w-[1440px] flex-col gap-3 px-4 pb-36 pt-3 md:px-6 md:pb-10">
        <section className="hidden min-w-0 items-center justify-between gap-3 border-b border-[var(--border)] pb-1.5 md:flex">
          <div className="flex min-w-0 items-baseline gap-2.5">
            <p className="type-page-kicker shrink-0">Video</p>
            <h1 className="type-page-title shrink-0">视频</h1>
            <p className="type-page-subtitle hidden min-w-0 truncate lg:block">
              文字、首帧和参考素材三种入口。
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-1.5">
            <HeaderStat
              label={options?.enabled ? "已启用" : "未启用"}
              value={optionsQ.isLoading ? "读取中" : options?.enabled ? "在线" : "离线"}
              tone={options?.enabled ? "success" : "muted"}
            />
            <HeaderStat
              label="活跃"
              value={String(activeItems.length)}
              tone={activeItems.length > 0 ? "accent" : "muted"}
            />
            <HeaderStat
              label="完成"
              value={String(completedVideoItems.length)}
              tone="muted"
            />
          </div>
        </section>
        <section className="grid gap-2 border-b border-[var(--border)] pb-3 md:hidden">
          <p className="type-page-kicker">Video</p>
          <div className="flex items-end justify-between gap-3">
            <h1 className="type-page-title">视频</h1>
            <span className="type-caption text-[var(--fg-2)]">
              {activeItems.length} 活跃 · {completedVideoItems.length} 完成
            </span>
          </div>
          <p className="text-[13px] leading-[1.6] text-[var(--fg-1)]">
            文字、首帧和参考素材三种入口。
          </p>
        </section>

        <div className="grid gap-4 lg:grid-cols-[minmax(360px,480px)_1fr]">
          <section className="space-y-4">
            <Card variant="subtle" padding="lg" className="space-y-4">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <p className="type-card-title">新建视频</p>
                  <p className="mt-1 text-sm text-[var(--fg-2)]">
                    {options?.enabled
                      ? `${availableModels.length} 个模型可用于${actionLabel(action)}`
                      : options?.unavailable_reason ?? "视频生成未启用"}
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

              <div className="space-y-2">
                <div className="grid grid-cols-3 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] p-1">
                  {(Object.keys(MODE_COPY) as VideoAction[]).map((key) => (
                    <ModeCard
                      key={key}
                      actionKey={key}
                      selected={action === key}
                      onSelect={() => {
                        setAction(key);
                        setModel(firstModelForAction(options, key));
                      }}
                    />
                  ))}
                </div>
                <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[var(--border-subtle)] pb-2 text-xs text-[var(--fg-2)]">
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
                      leftIcon={<Sparkles className="h-3.5 w-3.5" />}
                    >
                      一键优化
                    </Button>
                  </div>
                </div>
                <textarea
                  ref={promptRef}
                  value={prompt}
                  onChange={(event) => setPrompt(event.target.value)}
                  readOnly={isEnhancingPrompt}
                  rows={8}
                  maxLength={10000}
                  placeholder="写清主体、动作、画面比例和不要出现的内容。"
                  className={cn(
                    "min-h-[184px] w-full resize-none rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)]/80 p-3 text-sm leading-6 text-[var(--fg-0)] outline-none transition-[border-color,box-shadow] focus:border-[var(--accent)]/60 focus:shadow-[var(--ring)] placeholder:text-[var(--fg-2)]",
                    isEnhancingPrompt && "cursor-wait border-[var(--accent)]/50",
                  )}
                />
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
                <div className="grid gap-2 text-xs text-[var(--fg-2)] sm:grid-cols-3">
                  <PromptMeta icon={<Film className="h-3.5 w-3.5" />} label={actionLabel(action)} />
                  <PromptMeta icon={<Layers3 className="h-3.5 w-3.5" />} label={`${referenceMedia.length} 个参考素材`} />
                  <PromptMeta icon={<Settings2 className="h-3.5 w-3.5" />} label={`${effectiveResolution} · ${formatDurationLabel(durationS)}`} />
                </div>
              </div>

              {action === "i2v" && (
                <div className="space-y-3 rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/60 p-3">
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
                      上传首帧
                    </Button>
                  </div>
                  <input
                    value={inputImageId}
                    onChange={(event) => {
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
                <div className="space-y-3 rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/60 p-3">
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
                      上传图片/视频
                    </Button>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    {referenceMedia.map((item) => (
                      <ReferenceChip
                        key={item._key}
                        item={item}
                        onInsert={() => insertReferenceTag(item.label)}
                        onRemove={() =>
                          setReferenceMedia((prev) =>
                            prev.filter((ref) => ref._key !== item._key),
                          )
                        }
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

              <div className="space-y-3 rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/60 p-3">
                <div className="flex items-center gap-2">
                  <Settings2 className="h-4 w-4 text-[var(--fg-2)]" />
                  <p className="text-sm font-medium text-[var(--fg-0)]">参数</p>
                </div>
                <div className="grid gap-3 sm:grid-cols-2">
                  <SelectField
                    label="模型"
                    value={selectedModel}
                    onChange={setModel}
                    options={availableModels.map((item) => item.model)}
                  />
                  <SelectField
                    label="时长"
                    value={String(durationS)}
                    onChange={(value) => setDurationS(Number(value))}
                    options={(options?.durations_s ?? VIDEO_DURATION_OPTIONS).map(String)}
                    renderOption={(value) => formatDurationLabel(Number(value))}
                  />
                  <SelectField
                    label="分辨率"
                    value={effectiveResolution}
                    onChange={setResolution}
                    options={availableResolutions}
                  />
                  <SelectField
                    label="比例"
                    value={aspectRatio}
                    onChange={setAspectRatio}
                    options={options?.aspect_ratios ?? ["adaptive", "16:9", "9:16", "1:1"]}
                  />
                </div>
                <div className="grid gap-3 sm:grid-cols-[1fr_auto]">
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
                      onChange={(event) => setGenerateAudio(event.target.checked)}
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
            </Card>
          </section>

          <section className="space-y-4">
            <Card variant="subtle" padding="lg" className="space-y-4">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <p className="type-card-title">预览</p>
                  <p className="mt-1 text-sm text-[var(--fg-2)]">
                    最近完成的视频。
                  </p>
                </div>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => void historyQ.refetch()}
                  leftIcon={<RefreshCw className="h-3.5 w-3.5" />}
                >
                  刷新队列
                </Button>
              </div>
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
            </Card>

            <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_320px]">
              <Card variant="subtle" padding="lg" className="space-y-4">
                <div className="flex items-center justify-between gap-3">
                  <div className="flex items-center gap-2">
                    <Clapperboard className="h-4 w-4 text-[var(--fg-2)]" />
                    <p className="type-card-title">任务</p>
                  </div>
                  <span className="rounded-full border border-[var(--border)] bg-[var(--bg-0)] px-2 py-1 text-xs tabular-nums text-[var(--fg-2)]">
                    {activeItems.length}
                  </span>
                </div>
                <div className="space-y-3">
                  {activeItems.length === 0 && (
                    <EmptyPanel
                      icon={<Clapperboard className="h-5 w-5" />}
                      title="暂无任务"
                      description="提交后会显示排队、生成和取回状态。"
                    />
                  )}
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
                    />
                  ))}
                </div>
              </Card>

              <Card variant="subtle" padding="lg" className="space-y-4">
                <p className="type-card-title">最近</p>
                <div className="space-y-3">
                  {recentVideoItems.length === 0 && (
                    <EmptyPanel
                      icon={<Film className="h-5 w-5" />}
                      title="暂无视频"
                      description="完成后会出现在这里。"
                    />
                  )}
                  {recentVideoItems.map((item) => (
                    <RecentVideoCard
                      key={item.id}
                      item={item}
                      selected={primaryVideoItem?.video.id === item.video.id}
                      onPreview={() => setSelectedVideoId(item.video.id)}
                      onUseDraft={() => loadAsDraft(item)}
                    />
                  ))}
                </div>
              </Card>
            </div>

            <Card variant="subtle" padding="lg" className="space-y-4">
              <div className="flex items-center justify-between gap-3">
                <p className="type-card-title">历史</p>
                <span className="text-xs text-[var(--fg-2)]">
                  {historyQ.isLoading ? "读取中" : `${effectiveItems.length} 条`}
                </span>
              </div>
              <div className="grid gap-3">
                {effectiveItems.map((item) => (
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
                  />
                ))}
                {!historyQ.isLoading && effectiveItems.length === 0 && (
                  <EmptyPanel
                    icon={<Film className="h-5 w-5" />}
                    title="暂无历史"
                    description="提交记录会保留状态、参数和结果。"
                  />
                )}
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

function HeaderStat({
  label,
  value,
  tone = "muted",
}: {
  label: string;
  value: string;
  tone?: "muted" | "accent" | "success";
}) {
  return (
    <span
      className={cn(
        "inline-flex min-h-9 items-baseline gap-1.5 border px-3",
        tone === "success"
          ? "border-success-border bg-success-soft"
          : tone === "accent"
            ? "border-[var(--accent-border)] bg-[var(--accent-soft)]"
            : "border-[var(--border-subtle)] bg-[var(--bg-0)]/70",
      )}
    >
      <span
        className={cn(
          "text-[13px] font-semibold tabular-nums leading-[1.9]",
          tone === "success"
            ? "text-success"
            : tone === "accent"
              ? "text-[var(--accent)]"
              : "text-[var(--fg-0)]",
        )}
      >
        {value}
      </span>
      <span className="text-[10px] text-[var(--fg-2)]">{label}</span>
    </span>
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
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      className={cn(
        "group min-h-[74px] rounded-[var(--radius-control)] px-2 py-2 text-left transition-[background-color,color,transform] duration-200 sm:px-3",
        selected
          ? "bg-[var(--accent)] text-black shadow-[var(--shadow-amber)]"
          : "text-[var(--fg-1)] hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)]",
      )}
    >
      <div className="flex items-center justify-between gap-2">
        <span
          className={cn(
            "truncate text-[10px] font-medium",
            selected ? "text-black/60" : "text-[var(--fg-2)]",
          )}
        >
          {copy.eyebrow}
        </span>
        <span
          className={cn(
            "h-2 w-2 rounded-full",
            selected ? "bg-[var(--fg-0)]" : "bg-[var(--fg-3)]",
          )}
        />
      </div>
      <p className={cn("mt-2 text-sm font-semibold", selected ? "text-black" : "text-[var(--fg-0)]")}>
        {copy.title}
      </p>
      <p className={cn("mt-1 text-[11px] font-medium", selected ? "text-black/70" : "text-[var(--fg-2)]")}>
        {copy.requirement}
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
          ? "border-[var(--accent)]"
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
    <div className="relative overflow-hidden rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-0)] shadow-[var(--shadow-2)]">
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
        className="aspect-video w-full bg-[var(--bg-0)] object-contain"
      />
      {showState && (
        <div
          className={cn(
            "absolute inset-0 flex items-center justify-center bg-[var(--bg-0)]/55 text-[var(--fg-0)]",
            status !== "error" && "pointer-events-none",
          )}
        >
          <div
            role={status === "error" ? "alert" : "status"}
            aria-live={status === "error" ? "assertive" : "polite"}
            className="inline-flex items-center gap-2 rounded-full border border-[var(--border)] bg-[var(--bg-0)]/82 px-3 py-1.5 text-xs font-medium shadow-[var(--shadow-2)] backdrop-blur-md"
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
      <div className="grid min-h-[360px] place-items-center rounded-[var(--radius-panel)] border border-dashed border-[var(--border)] bg-[var(--bg-0)]/60 p-6">
        <div className="max-w-sm text-center">
          <div className="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)] text-[var(--fg-2)]">
            <Film className="h-6 w-6" />
          </div>
          <p className="text-base font-semibold text-[var(--fg-0)]">暂无视频</p>
          <p className="mt-2 text-sm leading-6 text-[var(--fg-2)]">
            生成完成后会显示在这里。
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <PrimaryVideoPlayer item={item} />
      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_auto]">
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

function RecentVideoCard({
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
    <article className="group space-y-2 rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/60 p-2.5 transition-colors hover:border-[var(--border)]">
      <VideoPosterButton
        item={item}
        selected={selected}
        onPreview={onPreview}
        compact
      />
      <p className="line-clamp-2 text-xs leading-5 text-[var(--fg-2)]">{item.prompt}</p>
      <div className="grid grid-cols-2 gap-2">
        <VideoDownloadLink item={item} fullWidth />
        <Button
          variant="outline"
          size="sm"
          fullWidth
          onClick={onUseDraft}
          leftIcon={<RotateCcw className="h-3.5 w-3.5" />}
        >
          套用参数
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
}: {
  item: VideoGenerationOut;
  onCancel: () => void;
  onRetry: () => void;
  onCopy: () => void;
  onUseDraft?: () => void;
  onDelete?: () => void;
  onPreview?: () => void;
  selected?: boolean;
}) {
  const active = isActiveVideo(item);
  const progress = progressForItem(item);
  const copy = stageCopy(item);
  const videoItem = hasVideo(item) ? item : null;
  return (
    <article className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/60 p-3 transition-colors hover:border-[var(--border)]">
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
      {videoItem && onPreview && (
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
