import type { VideoAction, VideoGenerationOut } from "@/lib/types";

export type VideoGenerationWithVideo = VideoGenerationOut & {
  video: NonNullable<VideoGenerationOut["video"]>;
};

export type VideoHistoryFilter = "all" | "succeeded" | "failed";
export const VIDEO_SETTLING_TIMEOUT_MS = 60_000;

export type VideoSettlingPhase = "settling" | "expired";

export type VideoSettlingCheckpoint = {
  phase: VideoSettlingPhase;
  startedAtMs: number;
  deadlineAtMs: number;
};

export function isVideoMaterializationPending(
  item: Pick<VideoGenerationOut, "status" | "video">,
): boolean {
  return item.status === "succeeded" && item.video == null;
}

export function createVideoSettlingCheckpoint(
  nowMs = Date.now(),
  timeoutMs = VIDEO_SETTLING_TIMEOUT_MS,
): VideoSettlingCheckpoint {
  const startedAtMs = Number.isFinite(nowMs) ? nowMs : Date.now();
  const durationMs = Number.isFinite(timeoutMs)
    ? Math.max(0, timeoutMs)
    : VIDEO_SETTLING_TIMEOUT_MS;
  return {
    phase: "settling",
    startedAtMs,
    deadlineAtMs: startedAtMs + durationMs,
  };
}

export function ensureVideoSettlingCheckpoint(
  checkpoint: VideoSettlingCheckpoint | undefined,
  nowMs = 0,
): VideoSettlingCheckpoint {
  if (!checkpoint) return createVideoSettlingCheckpoint(nowMs);
  if (checkpoint.phase === "expired") return checkpoint;
  if (nowMs >= checkpoint.deadlineAtMs) {
    return { ...checkpoint, phase: "expired" };
  }
  return checkpoint;
}

export function isVideoSettlingActive(
  item: Pick<VideoGenerationOut, "status" | "video">,
  checkpoint: VideoSettlingCheckpoint | undefined,
  nowMs?: number,
): boolean {
  if (!isVideoMaterializationPending(item)) return false;
  if (!checkpoint) return false;
  return (
    checkpoint.phase === "settling" &&
    (nowMs === undefined || nowMs < checkpoint.deadlineAtMs)
  );
}

export const MODE_COPY: Record<
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

const SMART_VIDEO_DURATION = -1;
const ACTIVE_VIDEO_STATUSES = [
  "queued",
  "submitting",
  "submit_unknown",
  "submitted",
  "running",
] as const;
const TERMINAL_VIDEO_STATUSES = [
  "succeeded",
  "failed",
  "canceled",
  "expired",
] as const;
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

export function formatDurationLabel(durationS: number): string {
  return durationS === SMART_VIDEO_DURATION ? "自动时长" : `${durationS}s`;
}

export function formatTaskElapsed(ms?: number | null): string | null {
  if (typeof ms !== "number" || !Number.isFinite(ms) || ms < 0) return null;
  const totalSeconds = Math.max(0, Math.round(ms / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m ${seconds}s`;
  return `${seconds}s`;
}

export function taskElapsedLabel(item: VideoGenerationOut): string | null {
  const elapsed = formatTaskElapsed(item.elapsed_ms);
  if (!elapsed) return null;
  return `${isTerminalVideo(item) ? "耗时" : "已耗时"} ${elapsed}`;
}

export function isActiveVideo(
  item: VideoGenerationOut,
  settling?: VideoSettlingCheckpoint,
  nowMs?: number,
): boolean {
  if (isVideoMaterializationPending(item)) {
    return isVideoSettlingActive(item, settling, nowMs);
  }
  if (
    ACTIVE_VIDEO_STATUSES.includes(
      item.status as (typeof ACTIVE_VIDEO_STATUSES)[number],
    )
  ) {
    return true;
  }
  return false;
}

export function isTerminalVideo(item: VideoGenerationOut): boolean {
  return TERMINAL_VIDEO_STATUSES.includes(
    item.status as (typeof TERMINAL_VIDEO_STATUSES)[number],
  );
}

export function isFailedHistoryVideo(item: VideoGenerationOut): boolean {
  return ["failed", "canceled", "expired"].includes(item.status);
}

export function hasVideo(
  item: VideoGenerationOut,
): item is VideoGenerationWithVideo {
  return item.video != null;
}

export function actionLabel(action: VideoAction): string {
  return MODE_COPY[action]?.title ?? action.toUpperCase();
}

export function stageCopy(
  item: VideoGenerationOut,
): { label: string; detail: string } {
  if (isVideoMaterializationPending(item)) {
    return {
      label: "整理中",
      detail: "任务已完成，正在等待视频文件保存。",
    };
  }
  return (
    STAGE_COPY[item.progress_stage] ??
    STAGE_COPY[item.status] ?? {
      label: item.status,
      detail: item.progress_stage,
    }
  );
}

export function progressForItem(item: VideoGenerationOut): number {
  if (item.status === "succeeded") return 100;
  if (["failed", "canceled", "expired"].includes(item.status)) {
    return Math.max(0, Math.min(100, item.progress_pct || 0));
  }
  return Math.max(4, Math.min(98, item.progress_pct || 0));
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

export function taskErrorSummary(raw: string): string {
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

export function activeVideoTaskSummary(
  activeCount: number,
  historyCount: number,
): string {
  return activeCount > 0
    ? `${activeCount} 个任务正在处理`
    : `${historyCount} 条历史记录`;
}

export function videoHistoryCountText({
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

export function videoHistoryEmptyCopy(
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
