"use client";

import { Handle, Position, type Node, type NodeProps } from "@xyflow/react";
import { AlertCircle, CheckCircle2, Loader2, Play, RotateCcw } from "lucide-react";
import { memo } from "react";

import { imageVariantUrl, videoPosterUrl } from "@/lib/apiClient";
import { CANVAS_NODE_SPECS, type CanvasPortSpec } from "@/lib/canvas/registry";
import type {
  CanvasDataType,
  CanvasNodeDefinition,
  CanvasNodeExecution,
  CanvasNodeType,
  CanvasOutput,
} from "@/lib/canvas/types";
import { cn } from "@/lib/utils";
import styles from "../canvas.module.css";

export interface CanvasFlowNodeData extends Record<string, unknown> {
  definition: CanvasNodeDefinition;
  execution?: CanvasNodeExecution | null;
  activeOutput?: CanvasOutput | null;
  deliveryOutputs?: CanvasOutput[];
  connectionType?: CanvasDataType | null;
  compatibleInputHandles?: string[];
  onRun?: (nodeId: string) => void;
}

export type CanvasFlowNode = Node<CanvasFlowNodeData, CanvasNodeDefinition["type"]>;

const TERMINAL_OK = new Set(["succeeded", "reused"]);
const ACTIVE = new Set(["pending", "ready", "queued", "running", "reconciling", "canceling"]);
const RUNNABLE_TYPES = new Set<CanvasNodeType>([
  "image_generate",
  "video_generate",
]);

function CanvasNodeComponent({ data, selected }: NodeProps<CanvasFlowNode>) {
  const { definition, execution } = data;
  const spec = CANVAS_NODE_SPECS[definition.type];
  const Icon = spec.icon;

  return (
    <article
      className={cn(
        "relative overflow-hidden rounded-[var(--radius-card)] border bg-[var(--bg-1)]/96 text-[var(--fg-0)] shadow-[var(--shadow-2)] backdrop-blur-xl",
        selected ? "border-[var(--accent)] shadow-[var(--shadow-amber)]" : "border-[var(--border)]",
      )}
      style={{ width: definition.size?.width ?? spec.width }}
      aria-label={`${spec.label}节点 ${definition.title}`}
    >
      <NodePorts
        ports={spec.inputs}
        direction="input"
        connectionType={data.connectionType}
        compatibleHandles={data.compatibleInputHandles}
      />
      <header className="flex min-h-11 items-center gap-2 border-b border-[var(--border-subtle)] px-3">
        <Icon className="h-4 w-4 shrink-0 text-[var(--accent)]" />
        <div className="min-w-0 flex-1">
          <h3 className="truncate type-body-sm font-medium">{definition.title}</h3>
          <p className="truncate type-mono-meta text-[var(--fg-3)]">{spec.label}</p>
        </div>
        <NodeStatus execution={execution} />
      </header>

      <div className="min-h-[96px]">
        <NodeContent data={data} />
      </div>

      <footer className="flex min-h-10 items-center justify-between gap-2 border-t border-[var(--border-subtle)] px-3">
        <span className="type-caption truncate text-[var(--fg-2)]">
          {nodeSummary(definition)}
        </span>
        <NodeFooterAction data={data} />
      </footer>
      <NodePorts
        ports={spec.outputs}
        direction="output"
        connectionType={data.connectionType}
      />
    </article>
  );
}

function FrameCanvasNode({ data, selected }: NodeProps<CanvasFlowNode>) {
  const { definition } = data;
  const Icon = CANVAS_NODE_SPECS.frame.icon;
  return (
    <div
      className={cn(
        "h-full min-h-[220px] w-full border border-dashed bg-[var(--bg-1)]/24 p-3",
        selected ? "border-[var(--accent)]" : "border-[var(--border-strong)]",
      )}
    >
      <div className="inline-flex items-center gap-2 bg-[var(--bg-0)]/86 px-2 py-1 type-caption text-[var(--fg-1)]">
        <Icon className="h-4 w-4 text-[var(--accent)]" />
        {definition.title}
      </div>
    </div>
  );
}

function NodeFooterAction({ data }: { data: CanvasFlowNodeData }) {
  const { definition, execution, activeOutput } = data;
  const running = Boolean(execution && ACTIVE.has(execution.status));
  const failed =
    execution?.status === "failed" || execution?.status === "blocked";
  if (!RUNNABLE_TYPES.has(definition.type)) {
    return execution && TERMINAL_OK.has(execution.status) || activeOutput ? (
      <CheckCircle2 className="h-4 w-4 shrink-0 text-[var(--success-fg)]" />
    ) : null;
  }
  return (
    <button
      type="button"
      aria-label={running ? "节点运行中" : failed ? "重试节点" : "运行节点"}
      title={running ? "运行中" : failed ? "重试" : "运行"}
      disabled={running}
      onClick={(event) => {
        event.stopPropagation();
        data.onRun?.(definition.id);
      }}
      className="nodrag nopan inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-control)] bg-[var(--accent)] text-[var(--accent-on)] transition-opacity hover:opacity-[var(--op-hover)] disabled:opacity-50"
    >
      {running ? (
        <Loader2 className="h-4 w-4 animate-spin" />
      ) : failed ? (
        <RotateCcw className="h-4 w-4" />
      ) : (
        <Play className="h-4 w-4" />
      )}
    </button>
  );
}

function NodeContent({ data }: { data: CanvasFlowNodeData }) {
  const { definition, activeOutput, deliveryOutputs = [] } = data;
  if (definition.type === "prompt" || definition.type === "note") {
    const text = String(definition.config.text ?? "").trim();
    return (
      <p className="line-clamp-4 p-3 type-body-sm leading-5 text-[var(--fg-1)]">
        {text || (definition.type === "prompt" ? "输入画面描述" : "添加备注")}
      </p>
    );
  }
  if (definition.type === "delivery") {
    return deliveryOutputs.length > 0 ? (
      <div className="grid grid-cols-3 gap-1 p-2">
        {deliveryOutputs.slice(0, 6).map((output, index) => (
          <OutputPreview key={`${output.image_id ?? output.video_id}-${index}`} output={output} />
        ))}
      </div>
    ) : (
      <div className="grid min-h-[96px] place-items-center p-3 type-caption text-[var(--fg-2)]">
        连接最终图片或视频
      </div>
    );
  }
  if (
    definition.type === "image_asset" ||
    definition.type === "video_asset" ||
    definition.type === "image_generate" ||
    definition.type === "video_generate"
  ) {
    return activeOutput ? (
      <OutputPreview output={activeOutput} large />
    ) : (
      <div className="grid min-h-[112px] place-items-center bg-[var(--surface-media)] p-3 type-caption text-[var(--fg-2)]">
        {definition.type.endsWith("_asset") ? "选择素材" : "暂无输出"}
      </div>
    );
  }
  return <div className="min-h-[96px]" />;
}

function OutputPreview({ output, large = false }: { output: CanvasOutput; large?: boolean }) {
  const src =
    output.preview_url ??
    output.url ??
    (output.type === "image" && output.image_id
      ? imageVariantUrl(output.image_id, "thumb256")
      : output.video_id
        ? output.poster_url ?? videoPosterUrl(output.video_id)
        : null);
  return (
    <div
      className={cn(
        "relative overflow-hidden bg-[var(--surface-media)]",
        large ? "aspect-video w-full" : "aspect-square",
      )}
    >
      {src ? (
        // eslint-disable-next-line @next/next/no-img-element -- API-backed signed media and canvas thumbnails.
        <img src={src} alt="" className="h-full w-full object-cover" draggable={false} />
      ) : (
        <div className="grid h-full min-h-16 place-items-center type-caption text-[var(--fg-3)]">
          无预览
        </div>
      )}
      {output.type === "video" ? (
        <span className="absolute bottom-1 right-1 rounded-[var(--radius-control)] bg-[var(--media-control-bg)] px-1.5 py-0.5 type-mono-meta text-[var(--media-control-fg)]">
          VIDEO
        </span>
      ) : null}
    </div>
  );
}

function NodeStatus({ execution }: { execution?: CanvasNodeExecution | null }) {
  if (!execution) return null;
  if (ACTIVE.has(execution.status)) {
    return <Loader2 className="h-4 w-4 shrink-0 animate-spin text-[var(--accent)]" />;
  }
  if (execution.status === "failed" || execution.status === "blocked") {
    return <AlertCircle className="h-4 w-4 shrink-0 text-[var(--danger-fg)]" />;
  }
  if (TERMINAL_OK.has(execution.status)) {
    return <CheckCircle2 className="h-4 w-4 shrink-0 text-[var(--success-fg)]" />;
  }
  return null;
}

function NodePorts({
  ports,
  direction,
  connectionType,
  compatibleHandles = [],
}: {
  ports: CanvasPortSpec[];
  direction: "input" | "output";
  connectionType?: CanvasDataType | null;
  compatibleHandles?: string[];
}) {
  return ports.map((port, index) => {
    const compatible =
      direction === "input" &&
      Boolean(connectionType) &&
      compatibleHandles.includes(port.id);
    const top = `${((index + 1) / (ports.length + 1)) * 100}%`;
    return (
      <Handle
        key={port.id}
        id={port.id}
        type={direction === "input" ? "target" : "source"}
        position={direction === "input" ? Position.Left : Position.Right}
        style={{ top }}
        data-port-type={port.dataType}
        aria-label={`${direction === "input" ? "输入" : "输出"}端口 ${port.label} ${port.dataType}`}
        title={`${port.label} · ${port.dataType}`}
        className={cn(styles.handle, compatible && styles.handleCompatible)}
      />
    );
  });
}

const NODE_SUMMARY: Record<
  CanvasNodeType,
  (node: CanvasNodeDefinition) => string
> = {
  prompt: (node) => String(node.config.text ?? "").trim() || "未填写",
  note: (node) => String(node.config.text ?? "").trim() || "未填写",
  image_asset: (node) =>
    String(node.config.display_name || node.config.image_id || "未选择"),
  video_asset: (node) =>
    String(node.config.display_name || node.config.video_id || "未选择"),
  image_generate: (node) =>
    `${String(node.config.aspect_ratio ?? "1:1")} · ${String(node.config.quality ?? "2k")} · ${Number(node.config.count ?? 1)} 张`,
  video_generate: (node) =>
    `${String(node.config.mode ?? "t2v")} · ${Number(node.config.duration_s ?? 5)} 秒`,
  delivery: () => "最终交付",
  frame: (node) => node.title,
};

function nodeSummary(node: CanvasNodeDefinition): string {
  return NODE_SUMMARY[node.type](node);
}

const MemoCanvasNode = memo(CanvasNodeComponent);
const MemoFrameNode = memo(FrameCanvasNode);

export const canvasNodeTypes = {
  prompt: MemoCanvasNode,
  image_asset: MemoCanvasNode,
  video_asset: MemoCanvasNode,
  image_generate: MemoCanvasNode,
  video_generate: MemoCanvasNode,
  note: MemoCanvasNode,
  frame: MemoFrameNode,
  delivery: MemoCanvasNode,
};
