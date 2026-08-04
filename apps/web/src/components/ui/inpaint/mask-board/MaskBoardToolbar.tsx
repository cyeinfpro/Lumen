import { Eraser, Paintbrush, RotateCcw, Scan, Undo2 } from "lucide-react";

import { IconButton } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

import type { Tool } from "../types";
import { MAX_BRUSH, MIN_BRUSH } from "./geometry";
import type { ViewTransform } from "./types";

interface MaskBoardToolbarProps {
  tool: Tool;
  brushSize: number;
  disabled: boolean;
  isDarkBg: boolean;
  hasImage: boolean;
  view: ViewTransform;
  viewIsFit: boolean;
  hasStroke: boolean;
  liveCoverage: number;
  strokeCount: number;
  onToolChange: (tool: Tool) => void;
  onBrushSizeChange: (value: number) => void;
  onFitView: () => void;
  onUndo: () => void;
  onReset: () => void;
}

export function MaskBoardToolbar({
  tool,
  brushSize,
  disabled,
  isDarkBg,
  hasImage,
  view,
  viewIsFit,
  hasStroke,
  liveCoverage,
  strokeCount,
  onToolChange,
  onBrushSizeChange,
  onFitView,
  onUndo,
  onReset,
}: MaskBoardToolbarProps) {
  const editDisabled = !hasStroke || disabled;
  return (
    <div className="flex flex-wrap items-center gap-2 px-1">
      <ToolSegment
        value={tool}
        onChange={onToolChange}
        disabled={disabled}
      />

      <BrushSizeControl
        value={brushSize}
        onChange={onBrushSizeChange}
        disabled={disabled}
        isDarkBg={isDarkBg}
      />

      <IconButton
        variant="ghost"
        onClick={onFitView}
        disabled={!hasImage || viewIsFit}
        aria-label="适应画布"
        tooltip={`适应画布 (${Math.round(view.scale * 100)}%)`}
        className="rounded-full"
      >
        <Scan className="w-4 h-4" />
      </IconButton>

      <IconButton
        variant="ghost"
        onClick={onUndo}
        disabled={editDisabled}
        aria-label="撤销 (Z)"
        tooltip="撤销 (Z)"
        className="rounded-full"
      >
        <Undo2 className="w-4 h-4" />
      </IconButton>

      <IconButton
        variant="ghost"
        onClick={onReset}
        disabled={editDisabled}
        aria-label="清除全部"
        tooltip="清除全部"
        className="rounded-full"
      >
        <RotateCcw className="w-4 h-4" />
      </IconButton>

      <CoverageBadge coverage={liveCoverage} strokeCount={strokeCount} />
    </div>
  );
}

function ToolSegment({
  value,
  onChange,
  disabled,
}: {
  value: Tool;
  onChange: (value: Tool) => void;
  disabled?: boolean;
}) {
  return (
    <div
      role="group"
      aria-label="工具"
      className={cn(
        "inline-flex h-12 shrink-0 items-center rounded-full p-px",
        "bg-[var(--bg-2)] border border-[var(--border-subtle)]",
      )}
    >
      {[
        { value: "brush" as const, label: "画笔", hint: "B", Icon: Paintbrush },
        { value: "eraser" as const, label: "橡皮", hint: "E", Icon: Eraser },
      ].map(({ value: option, label, hint, Icon }) => {
        const active = value === option;
        return (
          <button
            key={option}
            type="button"
            onClick={() => onChange(option)}
            disabled={disabled}
            aria-pressed={active}
            aria-label={`${label} (${hint})`}
            title={`${label} (${hint})`}
            className={cn(
              "inline-flex h-8 min-h-11 items-center gap-1.5 rounded-full px-3",
              "text-[11px] transition-colors disabled:opacity-50",
              active
                ? "bg-[var(--bg-0)] text-[var(--fg-0)] shadow-[var(--shadow-1)]"
                : "text-[var(--fg-1)] hover:text-[var(--fg-0)]",
            )}
          >
            <Icon className="w-3.5 h-3.5" aria-hidden />
            <span>{label}</span>
          </button>
        );
      })}
    </div>
  );
}

function BrushSizeControl({
  value,
  onChange,
  disabled,
  isDarkBg,
}: {
  value: number;
  onChange: (value: number) => void;
  disabled?: boolean;
  isDarkBg: boolean;
}) {
  const previewDiameter = Math.min(30, Math.max(6, value / 3));
  return (
    <label
      className="flex items-center gap-2 ml-1"
      title="画笔大小（[ / ] 调节，鼠标滚轮调节）"
    >
      <span
        aria-hidden
        className="inline-flex items-center justify-center w-5 h-5"
      >
        <span
          style={{
            width: previewDiameter,
            height: previewDiameter,
            borderRadius: "50%",
            background: isDarkBg
              ? "rgba(64, 224, 208, 0.55)"
              : "rgba(255, 59, 48, 0.5)",
            transition: "width 80ms ease-out, height 80ms ease-out",
          }}
        />
      </span>
      <input
        type="range"
        min={MIN_BRUSH}
        max={MAX_BRUSH}
        step={2}
        value={value}
        onChange={(event) => onChange(Number(event.target.value))}
        disabled={disabled}
        aria-label="画笔大小"
        className="h-1.5 w-28 sm:w-32 cursor-pointer accent-[var(--amber-400)] disabled:cursor-not-allowed"
      />
      <span className="text-[11px] text-[var(--fg-1)] tabular-nums w-9">
        {value}px
      </span>
    </label>
  );
}

function CoverageBadge({
  coverage,
  strokeCount,
}: {
  coverage: number;
  strokeCount: number;
}) {
  if (strokeCount === 0) return null;
  const percentage = Math.round(coverage * 100);
  const tone =
    percentage >= 50
      ? "bg-warning-soft text-warning border-warning-border"
      : percentage >= 5
        ? "bg-success-soft text-success border-success-border"
        : "bg-[var(--bg-2)] text-[var(--fg-1)] border-[var(--border-subtle)]";
  return (
    <span
      className={cn(
        "ml-auto inline-flex items-center gap-1 h-7 px-2.5 rounded-full",
        "text-[11px] tabular-nums border",
        tone,
      )}
      aria-live="polite"
    >
      涂抹 {percentage}%
    </span>
  );
}
