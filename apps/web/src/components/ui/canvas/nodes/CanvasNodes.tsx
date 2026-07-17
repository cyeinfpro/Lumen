"use client";

import {
  Handle,
  NodeResizer,
  Position,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import {
  AlertCircle,
  AlertTriangle,
  CheckCircle2,
  GripVertical,
  Loader2,
  Maximize2,
  Play,
  PlayCircle,
  RotateCcw,
} from "lucide-react";
import { memo, useCallback, useEffect, useRef, useState } from "react";

import type { LightboxItem } from "@/components/ui/lightbox/types";
import {
  imageBinaryUrl,
  imageVariantUrl,
  videoBinaryUrl,
} from "@/lib/apiClient";
import { canvasExecutionStatusLabel } from "@/lib/canvas/executionPresentation";
import {
  CANVAS_NODE_SPECS,
  findMatchingCanvasNodeCatalogItem,
  isCanvasExecutableNodeType,
  type CanvasPortSpec,
} from "@/lib/canvas/registry";
import type {
  CanvasDataType,
  CanvasNodeDefinition,
  CanvasNodeExecution,
  CanvasNodeType,
  CanvasOutput,
  CanvasPosition,
  CanvasSize,
} from "@/lib/canvas/types";
import { MAX_PROMPT_CHARS } from "@/lib/promptLimits";
import { cn } from "@/lib/utils";
import { useUiStore } from "@/store/useUiStore";
import { CanvasOutputDownloadButton } from "../CanvasOutputDownloadButton";
import { CanvasVideoPreviewDialog } from "../CanvasVideoPreviewDialog";
import { CanvasImageAssetDropZone } from "./CanvasImageAssetDropZone";
import { CanvasNodeExecutionProgress } from "./CanvasNodeExecutionProgress";
import styles from "../canvas.module.css";

export interface CanvasFlowNodeData extends Record<string, unknown> {
  definition: CanvasNodeDefinition;
  execution?: CanvasNodeExecution | null;
  activeOutput?: CanvasOutput | null;
  deliveryOutputs?: CanvasOutput[];
  resolvedText?: string;
  inputCounts?: Record<string, number>;
  runDisabledReason?: string | null;
  connectionType?: CanvasDataType | null;
  compatibleInputHandles?: string[];
  onRun?: (nodeId: string) => void;
  onUpdateConfig?: (nodeId: string, config: Record<string, unknown>) => void;
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
  onCompleteConnection?: (nodeId: string, handleId: string) => void;
  onResizeStart?: (nodeId: string) => void;
  onResizeEnd?: (
    nodeId: string,
    geometry: { position: CanvasPosition; size: CanvasSize },
  ) => void;
  editingEnabled?: boolean;
}

export type CanvasFlowNode = Node<
  CanvasFlowNodeData,
  CanvasNodeDefinition["type"]
>;

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
function CanvasNodeComponent({ data, selected }: NodeProps<CanvasFlowNode>) {
  const { definition, execution } = data;
  const spec = CANVAS_NODE_SPECS[definition.type];
  const preset = findMatchingCanvasNodeCatalogItem(definition);
  const displayLabel = preset?.label ?? spec.label;
  const Icon = spec.icon;
  const collapsed = definition.ui?.collapsed === true;
  const colorTag = nodeColorTag(definition);
  const running = Boolean(execution && ACTIVE.has(execution.status));
  const failed = Boolean(execution && FAILED.has(execution.status));
  const warning = Boolean(execution && WARNING.has(execution.status));

  return (
    <article
      className={cn(
        "relative overflow-visible rounded-[var(--radius-card)] border bg-[var(--bg-1)]/96 text-[var(--fg-0)] backdrop-blur-xl transition-[border-color,box-shadow]",
        canvasNodeStateClass(failed, running, warning),
        selected &&
          "ring-2 ring-[var(--accent)] ring-offset-2 ring-offset-[var(--surface-canvas)]",
      )}
      style={{ width: definition.size?.width ?? spec.width }}
      aria-busy={running || undefined}
      aria-label={canvasNodeAriaLabel(
        displayLabel,
        definition.title,
        collapsed,
      )}
    >
      <NodeActivityBar failed={failed} running={running} warning={warning} />
      <NodePorts
        ports={spec.inputs}
        direction="input"
        connectionType={data.connectionType}
        compatibleHandles={data.compatibleInputHandles}
        onStartConnection={nodeInputConnectionHandler(data, definition.id)}
      />
      <header
        className={cn(
          "canvas-node-drag-handle flex min-h-11 cursor-grab items-center gap-2 px-2 active:cursor-grabbing",
          !collapsed && "border-b border-[var(--border-subtle)]",
        )}
        title="拖动节点"
      >
        <GripVertical
          className="h-4 w-4 shrink-0 text-[var(--fg-3)]"
          aria-hidden
        />
        {colorTag ? (
          <span
            className="h-5 w-1 shrink-0 rounded-full border border-[var(--border-subtle)]"
            style={{ backgroundColor: colorTag }}
            title="颜色标签"
            aria-label="颜色标签"
          />
        ) : null}
        <Icon className="h-4 w-4 shrink-0 text-[var(--accent)]" aria-hidden />
        <div className="min-w-0 flex-1">
          <InlineNodeTitle
            key={`${definition.id}:${definition.title}`}
            data={data}
          />
          <p className="truncate type-mono-meta text-[var(--fg-3)]">
            {displayLabel}
          </p>
        </div>
        <NodeStatus execution={execution} />
      </header>

      <CanvasNodeBody collapsed={collapsed} data={data} />
      <NodePorts
        ports={spec.outputs}
        direction="output"
        connectionType={data.connectionType}
        onStartConnection={nodeOutputConnectionHandler(data, definition.id)}
      />
    </article>
  );
}

function CanvasNodeBody({
  collapsed,
  data,
}: {
  collapsed: boolean;
  data: CanvasFlowNodeData;
}) {
  if (collapsed) return <span className="sr-only">节点内容已折叠</span>;
  return (
    <>
      <div className="min-h-[96px]">
        <NodeContent data={data} />
      </div>
      <CanvasNodeExecutionProgress execution={data.execution} />
      <footer className="flex min-h-10 items-center justify-between gap-2 border-t border-[var(--border-subtle)] px-3">
        <span className="type-caption truncate text-[var(--fg-2)]">
          {nodeSummary(data)}
        </span>
        <NodeFooterAction data={data} />
      </footer>
    </>
  );
}

function NodeActivityBar({
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

function canvasNodeStateClass(
  failed: boolean,
  running: boolean,
  warning: boolean,
): string {
  if (failed) return "border-[var(--danger)] shadow-[var(--shadow-2)]";
  if (warning) return "border-[var(--warning)] shadow-[var(--shadow-2)]";
  if (running) {
    return "border-[var(--accent-border)] shadow-[var(--shadow-amber)]";
  }
  return "border-[var(--border)] shadow-[var(--shadow-2)]";
}

function canvasNodeAriaLabel(
  typeLabel: string,
  title: string,
  collapsed: boolean,
): string {
  return `${typeLabel}节点 ${title}${collapsed ? "，已折叠" : ""}`;
}

function nodeOutputConnectionHandler(
  data: CanvasFlowNodeData,
  nodeId: string,
) {
  if (!data.onStartConnection) return undefined;
  return (port: CanvasPortSpec) =>
    data.onStartConnection?.(nodeId, port.id, port.dataType);
}

function nodeInputConnectionHandler(
  data: CanvasFlowNodeData,
  nodeId: string,
) {
  if (!data.connectionType || !data.onCompleteConnection) return undefined;
  return (port: CanvasPortSpec) => {
    if (!data.compatibleInputHandles?.includes(port.id)) return;
    data.onCompleteConnection?.(nodeId, port.id);
  };
}

function FrameCanvasNode({ data, selected }: NodeProps<CanvasFlowNode>) {
  const Icon = CANVAS_NODE_SPECS.frame.icon;
  const { definition } = data;
  const collapsed = definition.ui?.collapsed === true;
  const colorTag = nodeColorTag(definition);
  return (
    <div
      className={cn(
        "relative w-full border border-dashed bg-[var(--bg-1)]/24",
        collapsed ? "h-11 min-h-11" : "h-full min-h-[220px] p-3",
        selected
          ? "border-[var(--accent)] ring-2 ring-[var(--accent)] ring-offset-2 ring-offset-[var(--surface-canvas)]"
          : "border-[var(--border-strong)]",
      )}
      aria-label={`画框节点 ${definition.title}${collapsed ? "，已折叠" : ""}`}
    >
      <NodeResizer
        isVisible={
          selected &&
          !collapsed &&
          data.editingEnabled !== false &&
          Boolean(data.onResizeEnd)
        }
        minWidth={240}
        minHeight={160}
        color="var(--accent)"
        lineClassName="!border-[var(--accent)]"
        handleClassName="!h-3 !w-3 !border-2 !border-[var(--bg-1)] !bg-[var(--accent)] after:absolute after:-inset-4 after:content-['']"
        onResize={() => data.onResizeStart?.(definition.id)}
        onResizeEnd={(_, params) => {
          data.onResizeEnd?.(definition.id, {
            position: {
              x: Math.round(params.x),
              y: Math.round(params.y),
            },
            size: {
              width: Math.round(params.width),
              height: Math.round(params.height),
            },
          });
        }}
      />
      <div
        className="canvas-node-drag-handle inline-flex min-h-11 max-w-full cursor-grab items-center gap-1.5 bg-[var(--bg-0)]/86 px-2 py-1 text-[var(--fg-1)] active:cursor-grabbing"
        title="拖动画框"
      >
        <GripVertical
          className="h-4 w-4 shrink-0 text-[var(--fg-3)]"
          aria-hidden
        />
        {colorTag ? (
          <span
            className="h-5 w-1 shrink-0 rounded-full border border-[var(--border-subtle)]"
            style={{ backgroundColor: colorTag }}
            title="颜色标签"
            aria-label="颜色标签"
          />
        ) : null}
        <Icon className="h-4 w-4 text-[var(--accent)]" aria-hidden />
        <InlineNodeTitle
          key={`${definition.id}:${definition.title}`}
          data={data}
          compact
        />
      </div>
      {collapsed ? <span className="sr-only">画框内容已折叠</span> : null}
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
        compact
          ? "w-[min(260px,calc(100%-2px))] type-body-sm"
          : "w-full type-body-sm",
      )}
    />
  );
}

function NodeFooterAction({ data }: { data: CanvasFlowNodeData }) {
  const { definition, execution, activeOutput } = data;
  const running = Boolean(execution && ACTIVE.has(execution.status));
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
      className="nodrag nopan inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-control)] bg-[var(--accent)] text-[var(--accent-on)] transition-opacity hover:opacity-[var(--op-hover)] disabled:opacity-50 max-[1199px]:h-11 max-[1199px]:w-11"
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

function NodeContent({ data }: { data: CanvasFlowNodeData }) {
  const { definition, activeOutput, deliveryOutputs = [] } = data;
  if (definition.type === "prompt" || definition.type === "note") {
    return <TextNodeContent data={data} />;
  }
  if (definition.type === "prompt_merge") {
    return <PromptMergeNodeContent data={data} />;
  }
  if (definition.type === "delivery") {
    return deliveryOutputs.length > 0 ? (
      <div className="grid grid-cols-3 gap-1 p-2">
        {deliveryOutputs.slice(0, 6).map((output, index) => (
          <OutputPreview
            key={`${output.image_id ?? output.video_id}-${index}`}
            output={output}
            alt={`交付${output.type === "image" ? "图片" : "视频"} ${index + 1}`}
          />
        ))}
      </div>
    ) : (
      <div className="grid min-h-[96px] place-items-center p-3 type-caption text-[var(--fg-2)]">
        连接最终图片或视频
      </div>
    );
  }
  if (definition.type === "image_asset") {
    return (
      <CanvasImageAssetDropZone
        nodeId={definition.id}
        config={definition.config}
        editingEnabled={data.editingEnabled !== false}
        onUpdateConfig={data.onUpdateConfig}
      >
        {activeOutput ? (
          <OutputPreview
            output={activeOutput}
            alt={`${definition.title}图片预览`}
            crop={normalizedCanvasCrop(definition.config.crop)}
            large
          />
        ) : null}
      </CanvasImageAssetDropZone>
    );
  }
  const spec = CANVAS_NODE_SPECS[definition.type];
  if (
    spec.family === "asset" ||
    spec.family === "image" ||
    spec.family === "video"
  ) {
    return activeOutput ? (
      <OutputPreview
        output={activeOutput}
        alt={`${definition.title}${activeOutput.type === "image" ? "图片" : "视频"}预览`}
        crop={
          definition.type === "mask_asset"
            ? normalizedCanvasCrop(definition.config.crop)
            : null
        }
        large
      />
    ) : (
      <NodeInputOverview data={data} />
    );
  }
  return <div className="min-h-[96px]" />;
}

function PromptMergeNodeContent({ data }: { data: CanvasFlowNodeData }) {
  const resolved = data.resolvedText ?? "";
  const inputCount = data.inputCounts?.texts ?? 0;
  return (
    <div className="grid min-h-[112px] content-start gap-2 bg-[var(--bg-2)]/32 p-3">
      <div className="flex items-center justify-between gap-3 type-caption">
        <span className="text-[var(--fg-2)]">组合预览</span>
        <span className="shrink-0 tabular-nums text-[var(--fg-1)]">
          {inputCount} 路 · {resolved.length} 字
        </span>
      </div>
      <p className="line-clamp-4 whitespace-pre-wrap type-body-sm leading-5 text-[var(--fg-1)]">
        {resolved || "连接多个提示词后在此预览组合结果"}
      </p>
    </div>
  );
}

function NodeInputOverview({ data }: { data: CanvasFlowNodeData }) {
  const { definition } = data;
  const spec = CANVAS_NODE_SPECS[definition.type];
  const isAsset = spec.family === "asset";
  if (isAsset) {
    const selected =
      definition.type === "video_asset"
        ? Boolean(definition.config.video_id)
        : Boolean(definition.config.image_id);
    return (
      <div className="grid min-h-[112px] place-items-center bg-[var(--surface-media)] p-3 text-center">
        <div>
          <p className="type-body-sm font-medium text-[var(--fg-1)]">
            {selected ? "素材已就绪" : assetEmptyLabel(definition.type)}
          </p>
          <p className="mt-1 type-caption text-[var(--fg-3)]">
            {selected ? "可连接到兼容的下游节点" : "在右侧检查器中上传或填写素材 ID"}
          </p>
        </div>
      </div>
    );
  }
  return (
    <div className="grid min-h-[112px] content-start gap-2 bg-[var(--surface-media)] p-3">
      <div className="flex items-center justify-between gap-3">
        <span className="type-caption font-medium text-[var(--fg-1)]">
          输入状态
        </span>
        <span className="type-mono-meta text-[var(--fg-3)]">等待运行</span>
      </div>
      <div className="grid gap-1.5">
        {spec.inputs.map((port) => {
          const count = data.inputCounts?.[port.id] ?? 0;
          const missing = port.required === true && count === 0;
          return (
            <div
              key={port.id}
              className="flex min-h-6 items-center justify-between gap-2"
            >
              <span className="min-w-0 truncate type-caption text-[var(--fg-2)]">
                {port.label}
                {port.required ? " *" : ""}
              </span>
              <span
                className={cn(
                  "inline-flex shrink-0 items-center gap-1 type-mono-meta tabular-nums",
                  missing
                    ? "text-[var(--danger-fg)]"
                    : count > 0
                      ? "text-[var(--success-fg)]"
                      : "text-[var(--fg-3)]",
                )}
              >
                {missing ? (
                  <AlertCircle className="h-3 w-3" aria-hidden />
                ) : count > 0 ? (
                  <CheckCircle2 className="h-3 w-3" aria-hidden />
                ) : null}
                {count > 0 ? count : missing ? "缺失" : "可选"}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function assetEmptyLabel(type: CanvasNodeType): string {
  if (type === "video_asset") return "选择视频素材";
  if (type === "mask_asset") return "选择遮罩素材";
  return "选择图片素材";
}

function TextNodeContent({ data }: { data: CanvasFlowNodeData }) {
  const { definition } = data;
  const isPrompt = definition.type === "prompt";
  const locked = isPrompt && definition.config.locked === true;
  const text = String(definition.config.text ?? "");
  const [draft, setDraft] = useState(text);
  const draftRef = useRef(text);
  const dataRef = useRef(data);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const timerRef = useRef<number | null>(null);
  const composingRef = useRef(false);
  const editingDisabled = data.editingEnabled === false || locked;
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
      flush();
    },
    [flush],
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
      aria-label={
        locked
          ? "提示词内容已锁定"
          : isPrompt
            ? "编辑提示词内容"
            : "编辑备注内容"
      }
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

function OutputPreview({
  output,
  alt,
  crop = null,
  large = false,
}: {
  output: CanvasOutput;
  alt: string;
  crop?: NormalizedCanvasCrop | null;
  large?: boolean;
}) {
  const media = useOutputPreviewMedia(output);
  const [videoPreviewOpen, setVideoPreviewOpen] = useState(false);
  const width = outputDimension(output.width);
  const height = outputDimension(output.height);
  const [naturalSize, setNaturalSize] = useState<{
    src: string;
    width: number;
    height: number;
  } | null>(null);
  const natural = matchingNaturalSize(media.visibleSrc, naturalSize);
  const previewWidth = width ?? natural?.width;
  const previewHeight = height ?? natural?.height;
  const cropStyle = outputCropStyle(
    output.type,
    crop,
    previewWidth,
    previewHeight,
  );
  return (
    <>
      <div
        className={cn(
          "relative w-full overflow-hidden bg-[var(--surface-media)]",
          large ? "min-h-[112px]" : "min-h-16",
        )}
        style={{
          aspectRatio: outputAspectRatio(
            output,
            crop,
            previewWidth,
            previewHeight,
          ),
        }}
      >
        <OutputPreviewButton
          output={output}
          alt={alt}
          media={media}
          width={width}
          height={height}
          cropStyle={cropStyle}
          onNaturalSize={setNaturalSize}
          onOpenVideo={() => setVideoPreviewOpen(true)}
        />
        <OutputTypeBadge type={output.type} />
        <CanvasOutputDownloadButton
          output={output}
          title={alt}
          className="absolute bottom-2 left-2 z-10"
        />
      </div>
      {media.videoSrc ? (
        <CanvasVideoPreviewDialog
          key={media.videoSrc}
          open={videoPreviewOpen}
          output={output}
          src={media.videoSrc}
          poster={media.poster}
          title={alt}
          onClose={() => setVideoPreviewOpen(false)}
        />
      ) : null}
    </>
  );
}

interface OutputPreviewMediaState {
  visibleSrc: string | null;
  videoSrc: string | null;
  poster: string | null;
  onError: () => void;
}

function useOutputPreviewMedia(output: CanvasOutput): OutputPreviewMediaState {
  const imageSources =
    output.type === "image" ? imagePreviewSources(output) : [];
  const imageSourceKey = imageSources.join("\n");
  const [imageSourceState, setImageSourceState] = useState({
    key: imageSourceKey,
    index: 0,
  });
  const imageSourceIndex =
    imageSourceState.key === imageSourceKey ? imageSourceState.index : 0;
  const videoSrc = output.type === "video" ? videoPlaybackSource(output) : null;
  const poster = output.type === "video" ? videoPosterSource(output) : null;
  const [failedVideoSrc, setFailedVideoSrc] = useState<string | null>(null);
  const imageSrc = imageSources[imageSourceIndex] ?? null;
  const visibleSrc =
    output.type === "video"
      ? videoSrc === failedVideoSrc
        ? null
        : videoSrc
      : imageSrc;
  const onError = () => {
    if (!visibleSrc) return;
    if (output.type === "video") {
      setFailedVideoSrc(visibleSrc);
      return;
    }
    setImageSourceState({
      key: imageSourceKey,
      index: imageSourceIndex + 1,
    });
  };
  return { visibleSrc, videoSrc, poster, onError };
}

function OutputPreviewButton({
  output,
  alt,
  media,
  width,
  height,
  cropStyle,
  onNaturalSize,
  onOpenVideo,
}: {
  output: CanvasOutput;
  alt: string;
  media: OutputPreviewMediaState;
  width?: number;
  height?: number;
  cropStyle?: React.CSSProperties;
  onNaturalSize: (
    size: { src: string; width: number; height: number },
  ) => void;
  onOpenVideo: () => void;
}) {
  const video = output.type === "video";
  return (
    <button
      type="button"
      aria-label={video ? `播放${alt}` : `放大查看${alt}`}
      title={video ? "播放视频" : "查看大图"}
      className={cn(
        "nodrag nopan nowheel group block h-full w-full text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[var(--accent)]",
        video ? "cursor-pointer" : "cursor-zoom-in",
      )}
      onPointerDown={(event) => event.stopPropagation()}
      onDoubleClick={(event) => event.stopPropagation()}
      onClick={(event) => {
        event.stopPropagation();
        openCanvasOutputPreview(output, alt, media.videoSrc, onOpenVideo);
      }}
    >
      <OutputPreviewMedia
        type={output.type}
        src={media.visibleSrc}
        poster={media.poster}
        alt={alt}
        width={width}
        height={height}
        cropStyle={cropStyle}
        onNaturalSize={onNaturalSize}
        onError={media.onError}
      />
      <OutputPreviewAffordance type={output.type} />
    </button>
  );
}

function OutputPreviewAffordance({ type }: { type: CanvasOutput["type"] }) {
  if (type === "video") {
    return (
      <span
        aria-hidden
        className="pointer-events-none absolute left-1/2 top-1/2 grid h-11 w-11 -translate-x-1/2 -translate-y-1/2 place-items-center rounded-full bg-[var(--media-control-bg)] text-[var(--media-control-fg)] shadow-[var(--shadow-2)]"
      >
        <PlayCircle className="h-6 w-6" />
      </span>
    );
  }
  return (
    <span
      aria-hidden
      className="pointer-events-none absolute right-2 top-2 grid h-8 w-8 place-items-center rounded-full bg-[var(--media-control-bg)] text-[var(--media-control-fg)] opacity-0 shadow-[var(--shadow-2)] transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100"
    >
      <Maximize2 className="h-4 w-4" />
    </span>
  );
}

function openCanvasOutputPreview(
  output: CanvasOutput,
  alt: string,
  videoSrc: string | null,
  onOpenVideo: () => void,
) {
  if (output.type !== "video") {
    openCanvasImagePreview(output, alt);
    return;
  }
  if (videoSrc) onOpenVideo();
}

function matchingNaturalSize(
  src: string | null,
  naturalSize: { src: string; width: number; height: number } | null,
) {
  return src && naturalSize?.src === src ? naturalSize : null;
}

function outputCropStyle(
  type: CanvasOutput["type"],
  crop: NormalizedCanvasCrop | null,
  width?: number,
  height?: number,
): React.CSSProperties | undefined {
  if (!crop || type !== "image" || !width || !height) return undefined;
  return {
    height: `${100 / crop.height}%`,
    left: `${(-crop.x / crop.width) * 100}%`,
    maxWidth: "none",
    position: "absolute",
    top: `${(-crop.y / crop.height) * 100}%`,
    width: `${100 / crop.width}%`,
  };
}

function OutputPreviewMedia({
  type,
  src,
  poster,
  alt,
  width,
  height,
  cropStyle,
  onNaturalSize,
  onError,
}: {
  type: CanvasOutput["type"];
  src: string | null;
  poster?: string | null;
  alt: string;
  width?: number;
  height?: number;
  cropStyle?: React.CSSProperties;
  onNaturalSize: (
    size: { src: string; width: number; height: number },
  ) => void;
  onError: () => void;
}) {
  if (!src) {
    return (
      <div className="grid h-full min-h-16 place-items-center type-caption text-[var(--fg-3)]">
        无预览
      </div>
    );
  }
  if (type === "video") {
    return (
      <video
        src={src}
        poster={poster || undefined}
        muted
        playsInline
        preload={poster ? "metadata" : "auto"}
        aria-label={alt}
        className="pointer-events-none h-full w-full object-contain"
        onLoadedMetadata={(event) => {
          if (poster) return;
          const video = event.currentTarget;
          if (video.duration > 0 && video.currentTime === 0) {
            video.currentTime = Math.min(0.05, video.duration / 10);
          }
        }}
        onLoadedData={(event) => {
          const video = event.currentTarget;
          if (video.videoWidth <= 0 || video.videoHeight <= 0) return;
          onNaturalSize({
            src,
            width: video.videoWidth,
            height: video.videoHeight,
          });
        }}
        onError={onError}
      />
    );
  }
  return (
    // eslint-disable-next-line @next/next/no-img-element -- API-backed signed media and canvas thumbnails.
    <img
      src={src}
      alt={alt}
      width={width}
      height={height}
      loading="lazy"
      decoding="async"
      className={cn(!cropStyle && "h-full w-full object-contain")}
      style={cropStyle}
      onLoad={(event) => {
        if (width && height) return;
        const image = event.currentTarget;
        if (image.naturalWidth <= 0 || image.naturalHeight <= 0) return;
        onNaturalSize({
          src,
          width: image.naturalWidth,
          height: image.naturalHeight,
        });
      }}
      onError={onError}
      draggable={false}
    />
  );
}

function OutputTypeBadge({ type }: { type: CanvasOutput["type"] }) {
  if (type !== "video") return null;
  return (
    <span className="pointer-events-none absolute bottom-1 right-1 rounded-[var(--radius-control)] bg-[var(--media-control-bg)] px-1.5 py-0.5 type-mono-meta text-[var(--media-control-fg)]">
      VIDEO
    </span>
  );
}

function imagePreviewSources(output: CanvasOutput): string[] {
  return uniqueMediaSources([
    output.preview_url,
    output.image_id
      ? imageVariantUrl(output.image_id, "display2048")
      : null,
    output.url,
    output.image_id ? imageBinaryUrl(output.image_id) : null,
  ]);
}

function videoPlaybackSource(output: CanvasOutput): string | null {
  return (
    output.url?.trim() ||
    (output.video_id ? videoBinaryUrl(output.video_id) : null) ||
    null
  );
}

function videoPosterSource(output: CanvasOutput): string | null {
  return output.poster_url?.trim() || output.preview_url?.trim() || null;
}

function uniqueMediaSources(
  sources: Array<string | null | undefined>,
): string[] {
  return Array.from(
    new Set(
      sources
        .map((source) => source?.trim() ?? "")
        .filter((source) => source.length > 0),
    ),
  );
}

function openCanvasImagePreview(output: CanvasOutput, alt: string) {
  const item = canvasImageLightboxItem(output, alt);
  if (!item) return;
  useUiStore.getState().openLightboxFromItems([item], item.id);
}

function canvasImageLightboxItem(
  output: CanvasOutput,
  alt: string,
): LightboxItem | null {
  const imageId = mediaText(output.image_id);
  const originalUrl =
    mediaText(output.url) || imageBinarySource(imageId);
  if (!originalUrl) return null;
  const id =
    imageId ||
    mediaText(output.generation_id) ||
    `canvas-image-${originalUrl}`;
  const item: LightboxItem = {
    id,
    url: originalUrl,
    previewUrl:
      mediaText(output.preview_url) ||
      imageDisplaySource(imageId) ||
      originalUrl,
    thumbUrl: imageDisplaySource(imageId) || originalUrl,
    prompt: mediaText(output.label) || alt,
    width: outputDimension(output.width),
    height: outputDimension(output.height),
    generation_id: output.generation_id ?? null,
    source: "canvas",
    source_type: "canvas_output",
  };
  return item;
}

function mediaText(value: string | null | undefined): string | null {
  const text = value?.trim();
  return text ? text : null;
}

function imageBinarySource(imageId: string | null): string | null {
  return imageId ? imageBinaryUrl(imageId) : null;
}

function imageDisplaySource(imageId: string | null): string | null {
  return imageId ? imageVariantUrl(imageId, "display2048") : null;
}

function outputAspectRatio(
  output: CanvasOutput,
  crop?: NormalizedCanvasCrop | null,
  resolvedWidth?: number,
  resolvedHeight?: number,
): string {
  const width = resolvedWidth ?? outputDimension(output.width);
  const height = resolvedHeight ?? outputDimension(output.height);
  if (width && height) {
    return crop && output.type === "image"
      ? `${width * crop.width} / ${height * crop.height}`
      : `${width} / ${height}`;
  }
  return output.type === "video" ? "16 / 9" : "1 / 1";
}

function outputDimension(value: number | null | undefined): number | undefined {
  return typeof value === "number" && Number.isFinite(value) && value > 0
    ? Math.round(value)
    : undefined;
}

function NodeStatus({ execution }: { execution?: CanvasNodeExecution | null }) {
  if (!execution) return null;
  const label = canvasExecutionStatusLabel(execution.status);
  const title = executionStatusTitle(execution, label);
  if (ACTIVE.has(execution.status)) {
    return (
      <span role="status" title={title} className="inline-flex shrink-0">
        <Loader2
          className="h-4 w-4 animate-spin text-[var(--accent)] motion-reduce:animate-none"
          aria-hidden
        />
        <span className="sr-only">状态：{label}</span>
      </span>
    );
  }
  if (execution.status === "failed" || execution.status === "blocked") {
    return (
      <span role="alert" title={title} className="inline-flex shrink-0">
        <AlertCircle className="h-4 w-4 text-[var(--danger-fg)]" aria-hidden />
        <span className="sr-only">状态：{label}</span>
      </span>
    );
  }
  if (execution.status === "partial_failed") {
    return (
      <span role="status" title={title} className="inline-flex shrink-0">
        <AlertTriangle
          className="h-4 w-4 text-[var(--warning-fg)]"
          aria-hidden
        />
        <span className="sr-only">状态：{label}</span>
      </span>
    );
  }
  if (TERMINAL_OK.has(execution.status)) {
    return (
      <span role="status" title={label} className="inline-flex shrink-0">
        <CheckCircle2
          className="h-4 w-4 text-[var(--success-fg)]"
          aria-hidden
        />
        <span className="sr-only">状态：{label}</span>
      </span>
    );
  }
  return (
    <span
      role="status"
      title={title}
      className="inline-flex h-4 w-4 shrink-0 items-center justify-center"
    >
      <span className="h-2 w-2 rounded-full bg-[var(--fg-3)]" aria-hidden />
      <span className="sr-only">状态：{label}</span>
    </span>
  );
}

function executionStatusTitle(
  execution: CanvasNodeExecution,
  label: string,
): string {
  const reason =
    execution.error_message ??
    execution.tasks?.find((task) => task.error_message)?.error_message ??
    null;
  return reason ? `${label}：${reason}` : label;
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
        aria-keyshortcuts={onStartConnection ? "Enter Space" : undefined}
        title={`${port.label} · ${port.dataType}`}
        role={onStartConnection ? "button" : undefined}
        tabIndex={onStartConnection ? 0 : -1}
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
          "nokey touch-manipulation after:absolute after:-inset-4 after:content-[''] focus-visible:outline-none focus-visible:shadow-[var(--ring)]",
          compatible && styles.handleCompatible,
        )}
      />
    );
  });
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

function nodeSummary(data: CanvasFlowNodeData): string {
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

function nodeColorTag(node: CanvasNodeDefinition): string | null {
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

interface NormalizedCanvasCrop {
  x: number;
  y: number;
  width: number;
  height: number;
}

function normalizedCanvasCrop(value: unknown): NormalizedCanvasCrop | null {
  if (!value || typeof value !== "object") return null;
  const crop = value as Record<string, unknown>;
  const x = Number(crop.x);
  const y = Number(crop.y);
  const width = Number(crop.width);
  const height = Number(crop.height);
  if (
    ![x, y, width, height].every(Number.isFinite) ||
    x < 0 ||
    y < 0 ||
    width <= 0 ||
    height <= 0 ||
    x + width > 1 ||
    y + height > 1
  ) {
    return null;
  }
  return { x, y, width, height };
}

const MemoCanvasNode = memo(CanvasNodeComponent);
const MemoFrameNode = memo(FrameCanvasNode);

export const canvasNodeTypes = {
  prompt: MemoCanvasNode,
  prompt_merge: MemoCanvasNode,
  image_asset: MemoCanvasNode,
  mask_asset: MemoCanvasNode,
  video_asset: MemoCanvasNode,
  image_generate: MemoCanvasNode,
  image_edit: MemoCanvasNode,
  image_inpaint: MemoCanvasNode,
  image_upscale: MemoCanvasNode,
  video_generate: MemoCanvasNode,
  video_text_generate: MemoCanvasNode,
  video_image_generate: MemoCanvasNode,
  video_reference_generate: MemoCanvasNode,
  note: MemoCanvasNode,
  frame: MemoFrameNode,
  delivery: MemoCanvasNode,
};
