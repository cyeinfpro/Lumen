"use client";

import { NodeResizer, type NodeProps } from "@xyflow/react";
import { GripVertical } from "lucide-react";
import { memo, useEffect, useRef, useState } from "react";

import { normalizeCanvasNodeTitle } from "@/lib/canvas/constants";
import {
  CANVAS_NODE_SPECS,
  findMatchingCanvasNodeCatalogItem,
  type CanvasPortSpec,
} from "@/lib/canvas/registry";
import { cn } from "@/lib/utils";
import { CanvasNodeExecutionProgress } from "./CanvasNodeExecutionProgress";
import { CanvasNodesContent } from "./CanvasNodesContent";
import { NodePorts } from "./CanvasNodesPorts";
import {
  canvasNodeExecutionState,
  canvasNodeStateClass,
  NodeActivityBar,
  nodeColorTag,
  NodeFooterAction,
  nodeSummary,
} from "./CanvasNodesPresentation";
import { CanvasNodeStatus } from "./CanvasNodeStatus";
import type {
  CanvasFlowNode,
  CanvasFlowNodeData,
} from "./CanvasNodesTypes";

export type { CanvasFlowNode, CanvasFlowNodeData } from "./CanvasNodesTypes";

function CanvasNodeComponent({ data, selected }: NodeProps<CanvasFlowNode>) {
  const { definition, execution } = data;
  const spec = CANVAS_NODE_SPECS[definition.type];
  const preset = findMatchingCanvasNodeCatalogItem(definition);
  const displayLabel = preset?.label ?? spec.label;
  const Icon = spec.icon;
  const collapsed = definition.ui?.collapsed === true;
  const colorTag = nodeColorTag(definition);
  const { running, failed, warning } = canvasNodeExecutionState(execution);

  return (
    <article
      className={cn(
        "relative overflow-visible rounded-[var(--radius-card)] border bg-[var(--bg-1)]/96 text-[var(--fg-0)] backdrop-blur-xl transition-[border-color,box-shadow]",
        canvasNodeStateClass(failed, running, warning),
        !running && "hover:shadow-[var(--shadow-2)]",
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
        <Icon
          className={cn(
            "h-4 w-4 shrink-0 transition-colors",
            running ? "text-[var(--accent)]" : "text-[var(--fg-2)]",
          )}
          aria-hidden
        />
        <div className="min-w-0 flex-1">
          <InlineNodeTitle
            key={`${definition.id}:${definition.title}`}
            data={data}
          />
          <p className="mt-0.5 truncate type-caption text-[var(--fg-3)]">
            {displayLabel}
          </p>
        </div>
        <CanvasNodeStatus execution={execution} />
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
        <CanvasNodesContent data={data} />
      </div>
      <CanvasNodeExecutionProgress execution={data.execution} />
      <footer className="flex min-h-11 items-center justify-between gap-2 border-t border-[var(--border-subtle)] px-3">
        <span className="type-caption truncate text-[var(--fg-3)]">
          {nodeSummary(data)}
        </span>
        <NodeFooterAction data={data} />
      </footer>
    </>
  );
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
        "relative w-full rounded-[var(--radius-card)] border border-dashed bg-[var(--bg-1)]/24 transition-[border-color,box-shadow] hover:shadow-[var(--shadow-2)]",
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
        className="canvas-node-drag-handle inline-flex min-h-11 max-w-full cursor-grab items-center gap-1.5 rounded-[var(--radius-control)] bg-[var(--bg-0)]/86 px-2 py-1 text-[var(--fg-1)] active:cursor-grabbing"
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
        <Icon className="h-4 w-4 text-[var(--fg-2)]" aria-hidden />
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
    const title = normalizeCanvasNodeTitle(draft, definition.title);
    setDraft(title);
    if (title !== definition.title) {
      data.onUpdateTitle?.(definition.id, title);
    }
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
        "nodrag nopan nokey block min-w-0 max-w-full cursor-text rounded-[var(--radius-control)] border border-transparent bg-transparent px-1 py-0.5 font-medium text-[var(--fg-0)] outline-none hover:border-[var(--border)] focus:border-[var(--accent)] focus:bg-[var(--bg-1)] focus:ring-2 focus:ring-[var(--accent-soft)] type-body",
        editingDisabled &&
          "pointer-events-none cursor-default truncate hover:border-transparent",
        compact
          ? "w-[min(260px,calc(100%-2px))] type-body-sm"
          : "w-full type-body-sm",
      )}
    />
  );
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
