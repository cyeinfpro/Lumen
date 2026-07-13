"use client";

import { Handle, Position, type Node, type NodeProps } from "@xyflow/react";
import {
  AlertCircle,
  CheckCircle2,
  GripVertical,
  Loader2,
  Play,
  RotateCcw,
} from "lucide-react";
import { memo, useCallback, useEffect, useRef, useState } from "react";

import { imageVariantUrl, videoPosterUrl } from "@/lib/apiClient";
import { CANVAS_NODE_SPECS, type CanvasPortSpec } from "@/lib/canvas/registry";
import type {
  CanvasDataType,
  CanvasNodeDefinition,
  CanvasNodeExecution,
  CanvasNodeType,
  CanvasOutput,
} from "@/lib/canvas/types";
import { MAX_PROMPT_CHARS } from "@/lib/promptLimits";
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
  onUpdateConfig?: (
    nodeId: string,
    config: Record<string, unknown>,
  ) => void;
  onUpdateTitle?: (nodeId: string, title: string) => void;
  onEditFocus?: (nodeId: string) => void;
  onEditBlur?: (nodeId: string) => void;
  onConfigEditStart?: (nodeId: string) => void;
  onConfigEditEnd?: (nodeId: string) => void;
  onStartConnection?: (
    nodeId: string,
    handleId: string,
    dataType: CanvasDataType,
  ) => void;
  editingEnabled?: boolean;
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
        "relative overflow-visible rounded-[var(--radius-card)] border bg-[var(--bg-1)]/96 text-[var(--fg-0)] shadow-[var(--shadow-2)] backdrop-blur-xl",
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
      <header
        className="canvas-node-drag-handle flex min-h-11 cursor-grab items-center gap-2 border-b border-[var(--border-subtle)] px-2 active:cursor-grabbing"
        title="拖动节点"
      >
        <GripVertical className="h-4 w-4 shrink-0 text-[var(--fg-3)]" aria-hidden />
        <Icon className="h-4 w-4 shrink-0 text-[var(--accent)]" />
        <div className="min-w-0 flex-1">
          <InlineNodeTitle
            key={`${definition.id}:${definition.title}`}
            data={data}
          />
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
        onStartConnection={
          data.onStartConnection
            ? (port) =>
                data.onStartConnection?.(
                  definition.id,
                  port.id,
                  port.dataType,
                )
            : undefined
        }
      />
    </article>
  );
}

function FrameCanvasNode({ data, selected }: NodeProps<CanvasFlowNode>) {
  const Icon = CANVAS_NODE_SPECS.frame.icon;
  return (
    <div
      className={cn(
        "h-full min-h-[220px] w-full border border-dashed bg-[var(--bg-1)]/24 p-3",
        selected ? "border-[var(--accent)]" : "border-[var(--border-strong)]",
      )}
    >
      <div
        className="canvas-node-drag-handle inline-flex min-h-9 max-w-full cursor-grab items-center gap-1.5 bg-[var(--bg-0)]/86 px-2 py-1 text-[var(--fg-1)] active:cursor-grabbing"
        title="拖动画框"
      >
        <GripVertical className="h-4 w-4 shrink-0 text-[var(--fg-3)]" aria-hidden />
        <Icon className="h-4 w-4 text-[var(--accent)]" />
        <InlineNodeTitle
          key={`${data.definition.id}:${data.definition.title}`}
          data={data}
          compact
        />
      </div>
    </div>
  );
}

function InlineNodeTitle({
  data,
  compact = false,
}: {
  data: CanvasFlowNodeData;
  compact?: boolean;
}) {
  const { definition } = data;
  const [draft, setDraft] = useState(definition.title);
  const cancelBlurRef = useRef(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const editingDisabled = data.editingEnabled === false;

  const commit = () => {
    if (cancelBlurRef.current) {
      cancelBlurRef.current = false;
      return;
    }
    const title = draft.trim().slice(0, 80);
    if (!title) {
      setDraft(definition.title);
      return;
    }
    setDraft(title);
    data.onUpdateTitle?.(definition.id, title);
  };

  useEffect(() => {
    if (
      editingDisabled &&
      inputRef.current &&
      document.activeElement === inputRef.current
    ) {
      inputRef.current.blur();
    }
  }, [editingDisabled]);

  return (
    <input
      ref={inputRef}
      type="text"
      value={draft}
      maxLength={80}
      readOnly={editingDisabled}
      tabIndex={editingDisabled ? -1 : undefined}
      data-canvas-inline-editor
      aria-label={`编辑${CANVAS_NODE_SPECS[definition.type].label}节点名称`}
      onChange={(event) => {
        if (!editingDisabled) setDraft(event.currentTarget.value);
      }}
      onFocus={(event) => {
        if (editingDisabled) {
          event.currentTarget.blur();
          return;
        }
        cancelBlurRef.current = false;
        data.onEditFocus?.(definition.id);
      }}
      onBlur={() => {
        commit();
        data.onEditBlur?.(definition.id);
      }}
      onPointerDown={(event) => {
        if (!editingDisabled) event.stopPropagation();
      }}
      onClick={(event) => {
        if (!editingDisabled) event.stopPropagation();
      }}
      onDoubleClick={(event) => {
        if (!editingDisabled) event.stopPropagation();
      }}
      onKeyDown={(event) => {
        if (event.nativeEvent.isComposing) {
          event.stopPropagation();
          return;
        }
        if (event.key === "Enter") {
          event.preventDefault();
          event.currentTarget.blur();
        }
        if (event.key === "Escape") {
          event.preventDefault();
          cancelBlurRef.current = true;
          setDraft(definition.title);
          event.currentTarget.blur();
        }
        event.stopPropagation();
      }}
      className={cn(
        "nodrag nopan nokey block min-w-0 max-w-full cursor-text rounded-[var(--radius-control)] border border-transparent bg-transparent px-1 py-0.5 font-medium text-[var(--fg-0)] outline-none hover:border-[var(--border)] focus:border-[var(--accent)] focus:bg-[var(--bg-1)] focus:ring-2 focus:ring-[var(--accent-soft)] max-[1199px]:text-base",
        editingDisabled &&
          "pointer-events-none cursor-default truncate hover:border-transparent",
        compact ? "w-[min(260px,calc(100%-2px))] type-body-sm" : "w-full type-body-sm",
      )}
    />
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
      className="nodrag nopan inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-control)] bg-[var(--accent)] text-[var(--accent-on)] transition-opacity hover:opacity-[var(--op-hover)] disabled:opacity-50 max-[1199px]:h-11 max-[1199px]:w-11"
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
    return <TextNodeContent data={data} />;
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

function TextNodeContent({ data }: { data: CanvasFlowNodeData }) {
  const { definition } = data;
  const isPrompt = definition.type === "prompt";
  const text = String(definition.config.text ?? "");
  const [draft, setDraft] = useState(text);
  const draftRef = useRef(text);
  const dataRef = useRef(data);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const timerRef = useRef<number | null>(null);
  const composingRef = useRef(false);
  const editingDisabled = data.editingEnabled === false;
  const placeholder = isPrompt ? "描述要生成的画面" : "添加画布说明";

  const flush = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    const currentData = dataRef.current;
    const currentDefinition = currentData.definition;
    const value = draftRef.current;
    if (value === String(currentDefinition.config.text ?? "")) return;
    currentData.onUpdateConfig?.(currentDefinition.id, {
      ...currentDefinition.config,
      text: value,
    });
  }, []);

  useEffect(() => {
    dataRef.current = data;
  }, [data]);

  const scheduleFlush = useCallback(() => {
    if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    timerRef.current = window.setTimeout(flush, 180);
  }, [flush]);

  useEffect(() => {
    if (document.activeElement === textareaRef.current) return;
    if (draftRef.current === text) return;
    draftRef.current = text;
    setDraft(text);
  }, [text]);

  useEffect(() => {
    if (!editingDisabled) return;
    if (document.activeElement === textareaRef.current) {
      textareaRef.current?.blur();
      return;
    }
    flush();
  }, [editingDisabled, flush]);

  useEffect(
    () => () => {
      if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    },
    [],
  );

  return (
    <textarea
      ref={textareaRef}
      value={draft}
      rows={4}
      data-canvas-inline-editor
      readOnly={editingDisabled}
      tabIndex={editingDisabled ? -1 : undefined}
      maxLength={isPrompt ? MAX_PROMPT_CHARS : 2000}
      aria-label={isPrompt ? "编辑提示词内容" : "编辑备注内容"}
      placeholder={placeholder}
      onFocus={(event) => {
        if (editingDisabled) {
          event.currentTarget.blur();
          return;
        }
        data.onConfigEditStart?.(definition.id);
        data.onEditFocus?.(definition.id);
      }}
      onBlur={() => {
        flush();
        data.onConfigEditEnd?.(definition.id);
        data.onEditBlur?.(definition.id);
      }}
      onPointerDown={(event) => {
        if (!editingDisabled) event.stopPropagation();
      }}
      onClick={(event) => {
        if (!editingDisabled) event.stopPropagation();
      }}
      onDoubleClick={(event) => {
        if (!editingDisabled) event.stopPropagation();
      }}
      onChange={(event) => {
        if (editingDisabled) return;
        const value = event.currentTarget.value;
        draftRef.current = value;
        setDraft(value);
        if (!composingRef.current) scheduleFlush();
      }}
      onCompositionStart={() => {
        composingRef.current = true;
      }}
      onCompositionEnd={(event) => {
        composingRef.current = false;
        const value = event.currentTarget.value;
        draftRef.current = value;
        setDraft(value);
        scheduleFlush();
      }}
      onKeyDown={(event) => {
        if (event.nativeEvent.isComposing) {
          event.stopPropagation();
          return;
        }
        if (event.key === "Escape") {
          event.preventDefault();
          event.currentTarget.blur();
        }
        event.stopPropagation();
      }}
      className={cn(
        "nodrag nopan nowheel nokey block h-24 w-full cursor-text resize-none overflow-y-auto border-0 bg-[var(--bg-2)]/38 p-3 type-body-sm leading-5 text-[var(--fg-1)] outline-none placeholder:text-[var(--fg-3)] focus:bg-[var(--bg-2)]/62 focus:ring-2 focus:ring-inset focus:ring-[var(--accent-soft)] max-[1199px]:text-base max-[1199px]:leading-6",
        editingDisabled &&
          "pointer-events-none cursor-default overflow-hidden bg-transparent",
      )}
    />
  );
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
  onStartConnection,
}: {
  ports: CanvasPortSpec[];
  direction: "input" | "output";
  connectionType?: CanvasDataType | null;
  compatibleHandles?: string[];
  onStartConnection?: (port: CanvasPortSpec) => void;
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
        isConnectableStart={direction === "output"}
        isConnectableEnd={direction === "input"}
        style={{ top }}
        data-port-type={port.dataType}
        aria-label={`${direction === "input" ? "输入" : "输出"}端口 ${port.label} ${port.dataType}`}
        title={`${port.label} · ${port.dataType}`}
        role={onStartConnection ? "button" : undefined}
        tabIndex={onStartConnection ? 0 : undefined}
        onClick={
          onStartConnection
            ? (event) => {
                event.stopPropagation();
                onStartConnection(port);
              }
            : undefined
        }
        onKeyDown={
          onStartConnection
            ? (event) => {
                if (event.key !== "Enter" && event.key !== " ") return;
                event.preventDefault();
                event.stopPropagation();
                onStartConnection(port);
              }
            : undefined
        }
        className={cn(
          styles.handle,
          onStartConnection && "nokey",
          compatible && styles.handleCompatible,
        )}
      />
    );
  });
}

const NODE_SUMMARY: Record<
  CanvasNodeType,
  (node: CanvasNodeDefinition) => string
> = {
  prompt: (node) => `${String(node.config.text ?? "").length} 字`,
  note: (node) => `${String(node.config.text ?? "").length} 字`,
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
