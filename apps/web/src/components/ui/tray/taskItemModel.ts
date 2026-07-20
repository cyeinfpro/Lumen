import { recommendedActionsForError } from "@/lib/errors";
import type {
  Generation,
  GenerationStage,
  RecommendedErrorAction,
} from "@/lib/types";

const STAGE_LABEL: Record<GenerationStage, string> = {
  queued: "排队中",
  understanding: "理解中",
  rendering: "渲染中",
  finalizing: "收尾",
};

const STAGE_RATIO: Record<GenerationStage, number> = {
  queued: 0.12,
  understanding: 0.35,
  rendering: 0.7,
  finalizing: 0.92,
};

const SUBSTAGE_LABEL: Record<string, string> = {
  waiting_queue: "排队中",
  waiting_provider: "等待可用通道",
  preparing_refs: "准备参考图",
  upstream_started: "模型生成中",
  upstream_retrying: "上游重试中",
  postprocessing: "图片后处理中",
  processing: "图片后处理中",
  storing: "保存图片中",
  display_ready: "图片已完成",
  retryable: "失败，可重试",
  terminal: "失败",
  cancelled: "已取消",
  provider_selected: "通道已就绪",
  stream_started: "模型生成中",
  partial_received: "生成预览中",
  final_received: "生成完成，处理中",
};

export interface TaskItemPresentation {
  running: boolean;
  queued: boolean;
  failed: boolean;
  succeeded: boolean;
  canceled: boolean;
  ratio: number;
  statusText: string;
  title: string;
  actions: RecommendedErrorAction[];
  showRecoveryActions: boolean;
}

function truncate(value: string, length: number): string {
  if (!value) return "";
  return value.length > length ? `${value.slice(0, length)}…` : value;
}

function taskProgressRatio(generation: Generation): number {
  if (
    generation.status === "succeeded" ||
    generation.status === "failed" ||
    generation.status === "canceled"
  ) {
    return 1;
  }
  return STAGE_RATIO[generation.stage] ?? 0.2;
}

function taskStatusText(generation: Generation): string {
  if (generation.status === "failed") {
    return (
      generation.diagnostics?.safe_error_summary ??
      generation.error_message ??
      "生成失败"
    );
  }
  if (generation.status === "canceled") return "已取消";
  if (generation.status === "succeeded") return "已完成";

  const substage = generation.substage
    ? SUBSTAGE_LABEL[generation.substage]
    : undefined;
  if (generation.status === "queued") {
    const queuePosition =
      generation.queue_position != null && generation.queue_position > 0
        ? ` · 第 ${generation.queue_position} 位`
        : "";
    return `${substage ?? "排队中"}${queuePosition}`;
  }

  const stage = substage ?? STAGE_LABEL[generation.stage];
  return generation.attempt > 1
    ? `${stage} (第${generation.attempt}次)`
    : stage;
}

function recoveryActions(generation: Generation): RecommendedErrorAction[] {
  return generation.recommended_actions?.length
    ? generation.recommended_actions
    : recommendedActionsForError(generation.error_code, {
        retryable: generation.retryable,
        status: generation.status,
      });
}

export function deriveTaskItemPresentation(
  generation: Generation,
): TaskItemPresentation {
  const running =
    generation.status === "queued" || generation.status === "running";
  const queued = generation.status === "queued";
  const failed = generation.status === "failed";
  const succeeded = generation.status === "succeeded";
  const canceled = generation.status === "canceled";
  const actions = recoveryActions(generation);

  return {
    running,
    queued,
    failed,
    succeeded,
    canceled,
    ratio: taskProgressRatio(generation),
    statusText: taskStatusText(generation),
    title: truncate(generation.prompt || "图像生成", 40),
    actions,
    showRecoveryActions: (failed || canceled) && actions.length > 0,
  };
}
