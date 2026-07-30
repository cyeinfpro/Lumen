import { AlertCircle, CheckCircle2 } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { CANVAS_NOTE_MAX_CHARS } from "@/lib/canvas/constants";
import { CANVAS_NODE_SPECS } from "@/lib/canvas/registry";
import type { CanvasNodeType } from "@/lib/canvas/types";
import { MAX_PROMPT_CHARS } from "@/lib/promptLimits";
import { cn } from "@/lib/utils";
import { CanvasImageAssetDropZone } from "./CanvasImageAssetDropZone";
import {
  normalizedCanvasCrop,
  OutputPreview,
} from "./CanvasNodesPreview";
import type { CanvasFlowNodeData } from "./CanvasNodesTypes";

export function CanvasNodesContent({ data }: { data: CanvasFlowNodeData }) {
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
            {selected
              ? "可连接到兼容的下游节点"
              : "在右侧检查器中上传或填写素材 ID"}
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
      maxLength={isPrompt ? MAX_PROMPT_CHARS : CANVAS_NOTE_MAX_CHARS}
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
