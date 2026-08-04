import { CheckCircle2, Loader2, Play, RotateCcw } from "lucide-react";

import { isCanvasExecutableNodeType } from "@/lib/canvas/registry";
import type {
  CanvasNodeDefinition,
  CanvasNodeExecution,
  CanvasNodeType,
  CanvasOutput,
} from "@/lib/canvas/types";
import type { CanvasFlowNodeData } from "./CanvasNodesTypes";

const TERMINAL_OK = new Set(["succeeded", "reused"]);
const ACTIVE = new Set([
  "pending",
  "ready",
  "queued",
  "running",
  "reconciling",
  "canceling",
]);
const FAILED = new Set(["failed", "blocked"]);
const WARNING = new Set(["partial_failed"]);

export function canvasNodeExecutionState(
  execution?: CanvasNodeExecution | null,
) {
  return {
    running: Boolean(execution && ACTIVE.has(execution.status)),
    failed: Boolean(execution && FAILED.has(execution.status)),
    warning: Boolean(execution && WARNING.has(execution.status)),
  };
}

export function NodeActivityBar({
  failed,
  running,
  warning,
}: {
  failed: boolean;
  running: boolean;
  warning: boolean;
}) {
  if (failed) {
    return (
      <span
        aria-hidden
        className="absolute inset-x-0 top-0 z-10 h-1 rounded-t-[var(--radius-card)] bg-[var(--danger)]"
      />
    );
  }
  if (warning) {
    return (
      <span
        aria-hidden
        className="absolute inset-x-0 top-0 z-10 h-1 rounded-t-[var(--radius-card)] bg-[var(--warning)]"
      />
    );
  }
  if (!running) return null;
  return (
    <span
      aria-hidden
      className="absolute inset-x-0 top-0 z-10 h-1 animate-pulse rounded-t-[var(--radius-card)] bg-[var(--accent)] motion-reduce:animate-none"
    />
  );
}

export function canvasNodeStateClass(
  failed: boolean,
  running: boolean,
  warning: boolean,
): string {
  if (failed) return "border-[var(--danger)] shadow-[var(--shadow-1)]";
  if (warning) return "border-[var(--warning)] shadow-[var(--shadow-1)]";
  if (running) {
    return "border-[var(--accent-border)] shadow-[var(--shadow-amber)]";
  }
  return "border-[var(--border)] shadow-[var(--shadow-1)]";
}

export function NodeFooterAction({ data }: { data: CanvasFlowNodeData }) {
  const { definition, execution, activeOutput } = data;
  const { running } = canvasNodeExecutionState(execution);
  const failed =
    execution?.status === "partial_failed" ||
    execution?.status === "failed" ||
    execution?.status === "blocked";
  if (!isCanvasExecutableNodeType(definition.type)) {
    return (
      <PassiveNodeCompletion execution={execution} activeOutput={activeOutput} />
    );
  }
  return <RunnableNodeAction data={data} failed={failed} running={running} />;
}

function PassiveNodeCompletion({
  execution,
  activeOutput,
}: {
  execution?: CanvasNodeExecution | null;
  activeOutput?: CanvasOutput | null;
}) {
  const complete =
    Boolean(execution && TERMINAL_OK.has(execution.status)) ||
    Boolean(activeOutput);
  return complete ? (
    <CheckCircle2 className="h-4 w-4 shrink-0 text-[var(--success-fg)]" />
  ) : null;
}

function RunnableNodeAction({
  data,
  failed,
  running,
}: {
  data: CanvasFlowNodeData;
  failed: boolean;
  running: boolean;
}) {
  const disabledReason = data.runDisabledReason ?? null;
  const label = runnableNodeActionLabel(running, failed, disabledReason);
  return (
    <button
      type="button"
      aria-label={label.aria}
      title={label.title}
      disabled={running || Boolean(disabledReason)}
      onClick={(event) => {
        event.stopPropagation();
        data.onRun?.(data.definition.id);
      }}
      className="nodrag nopan inline-flex h-8 min-h-11 w-8 min-w-11 shrink-0 items-center justify-center rounded-[var(--radius-control)] bg-[var(--accent)] text-[var(--accent-on)] transition-opacity hover:opacity-[var(--op-hover)] disabled:opacity-50"
    >
      <RunnableNodeActionIcon failed={failed} running={running} />
    </button>
  );
}

function RunnableNodeActionIcon({
  failed,
  running,
}: {
  failed: boolean;
  running: boolean;
}) {
  if (running) {
    return (
      <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" />
    );
  }
  if (failed) return <RotateCcw className="h-4 w-4" />;
  return <Play className="h-4 w-4" />;
}

function runnableNodeActionLabel(
  running: boolean,
  failed: boolean,
  disabledReason: string | null,
) {
  if (running) return { aria: "节点运行中", title: "运行中" };
  if (disabledReason) {
    return {
      aria: `节点不可运行：${disabledReason}`,
      title: disabledReason,
    };
  }
  if (failed) return { aria: "重试节点", title: "重试" };
  return { aria: "运行节点", title: "运行" };
}

const NODE_SUMMARY: Record<
  CanvasNodeType,
  (data: CanvasFlowNodeData) => string
> = {
  prompt: ({ definition }) =>
    `${String(definition.config.text ?? "").length} 字${definition.config.locked === true ? " · 已锁定" : ""}`,
  prompt_merge: ({ resolvedText, inputCounts }) =>
    `${inputCounts?.texts ?? 0} 路 · ${(resolvedText ?? "").length} 字`,
  note: ({ definition }) => {
    const tags = Array.isArray(definition.config.tags)
      ? definition.config.tags.length
      : 0;
    return `${String(definition.config.text ?? "").length} 字${tags ? ` · ${tags} 标签` : ""}`;
  },
  image_asset: ({ definition }) => assetSummary(definition),
  mask_asset: ({ definition }) => assetSummary(definition),
  video_asset: ({ definition }) => assetSummary(definition),
  image_generate: ({ definition }) => imageSummary(definition),
  image_edit: ({ definition }) => imageSummary(definition),
  image_inpaint: ({ definition }) => imageSummary(definition),
  image_upscale: ({ definition }) => imageSummary(definition),
  video_generate: ({ definition }) => videoSummary(definition),
  video_text_generate: ({ definition }) => videoSummary(definition),
  video_image_generate: ({ definition }) => videoSummary(definition),
  video_reference_generate: ({ definition }) => videoSummary(definition),
  delivery: ({ deliveryOutputs }) =>
    deliveryOutputs?.length ? `${deliveryOutputs.length} 个结果` : "最终交付",
  frame: ({ definition }) =>
    definition.config.hidden_in_run === true
      ? "运行视图隐藏"
      : definition.title,
};

export function nodeSummary(data: CanvasFlowNodeData): string {
  return NODE_SUMMARY[data.definition.type](data);
}

function assetSummary(node: CanvasNodeDefinition): string {
  if (node.type === "video_asset") {
    return String(node.config.display_name || node.config.video_id || "未选择");
  }
  return String(node.config.display_name || node.config.image_id || "未选择");
}

function imageSummary(node: CanvasNodeDefinition): string {
  return `${String(node.config.aspect_ratio ?? "1:1")} · ${String(node.config.quality ?? "2k").toUpperCase()} · ${Number(node.config.count ?? 1)} 张`;
}

function videoSummary(node: CanvasNodeDefinition): string {
  const duration = Number(node.config.duration_s ?? 5);
  return `${videoModeLabel(String(node.config.mode ?? "t2v"))} · ${duration === -1 ? "智能时长" : `${duration} 秒`} · ${String(node.config.resolution ?? "720p").toUpperCase()}`;
}

function videoModeLabel(mode: string): string {
  return (
    {
      t2v: "文生视频",
      i2v: "图生视频",
      reference: "参考生成",
    }[mode] ?? mode
  );
}

export function nodeColorTag(node: CanvasNodeDefinition): string | null {
  const colorTag = node.ui?.color_tag;
  if (typeof colorTag !== "string" || !colorTag.trim()) return null;
  const value = colorTag.trim();
  return NODE_COLOR_TAG_VALUES[value] ?? value;
}

const NODE_COLOR_TAG_VALUES: Record<string, string> = {
  accent: "var(--accent)",
  success: "var(--success)",
  info: "var(--info)",
  danger: "var(--danger)",
};
