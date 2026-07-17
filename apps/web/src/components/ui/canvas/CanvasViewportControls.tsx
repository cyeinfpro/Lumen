"use client";

import { Grid3X3, Map, Minus, Plus, Scan } from "lucide-react";

import { Button, IconButton } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

export interface CanvasViewportControlsProps {
  /** Current zoom ratio where 1 equals 100%. */
  zoom: number;
  minZoom?: number;
  maxZoom?: number;
  onZoomOut: () => void;
  onZoomIn: () => void;
  onResetZoom: () => void;
  onFitView: () => void;
  showFitView?: boolean;
  gridVisible: boolean;
  onGridVisibleChange: (visible: boolean) => void;
  minimapVisible: boolean;
  onMinimapVisibleChange: (visible: boolean) => void;
  disabled?: boolean;
  className?: string;
}

function normalizedZoom(zoom: number) {
  return Number.isFinite(zoom) ? Math.max(0, zoom) : 1;
}

export function CanvasViewportControls({
  zoom,
  minZoom = 0.15,
  maxZoom = 2.4,
  onZoomOut,
  onZoomIn,
  onResetZoom,
  onFitView,
  showFitView = true,
  gridVisible,
  onGridVisibleChange,
  minimapVisible,
  onMinimapVisibleChange,
  disabled = false,
  className,
}: CanvasViewportControlsProps) {
  const safeZoom = normalizedZoom(zoom);
  const zoomPercent = Math.round(safeZoom * 100);
  const atMinimum = safeZoom <= minZoom + 0.001;
  const atMaximum = safeZoom >= maxZoom - 0.001;

  return (
    <div
      role="toolbar"
      aria-label="画布视口"
      className={cn(
        "surface-panel inline-flex min-h-11 max-w-full items-center gap-0.5 overflow-x-auto p-1",
        "text-[var(--fg-1)] motion-reduce:scroll-auto",
        className,
      )}
    >
      <IconButton
        aria-label="缩小画布"
        tooltip="缩小"
        size="sm"
        disabled={disabled || atMinimum}
        onClick={onZoomOut}
      >
        <Minus className="h-4 w-4" aria-hidden />
      </IconButton>

      <output
        aria-label="当前缩放比例"
        aria-live="polite"
        aria-atomic="true"
        className="inline-flex h-8 w-14 shrink-0 items-center justify-center type-caption font-medium tabular-nums text-[var(--fg-0)]"
      >
        {zoomPercent}%
      </output>

      <IconButton
        aria-label="放大画布"
        tooltip="放大"
        size="sm"
        disabled={disabled || atMaximum}
        onClick={onZoomIn}
      >
        <Plus className="h-4 w-4" aria-hidden />
      </IconButton>

      <span
        role="separator"
        aria-orientation="vertical"
        className="mx-1 h-5 w-px shrink-0 bg-[var(--border)]"
      />

      <Button
        variant="ghost"
        size="sm"
        aria-label="重置为 100%"
        title="重置为 100%"
        disabled={disabled}
        onClick={onResetZoom}
        className="w-[54px] shrink-0 px-2 tabular-nums motion-reduce:transition-none"
      >
        100%
      </Button>
      {showFitView ? (
        <>
          <IconButton
            aria-label="适应画布"
            tooltip="适应画布"
            size="sm"
            disabled={disabled}
            onClick={onFitView}
          >
            <Scan className="h-4 w-4" aria-hidden />
          </IconButton>

          <span
            role="separator"
            aria-orientation="vertical"
            className="mx-1 h-5 w-px shrink-0 bg-[var(--border)]"
          />
        </>
      ) : null}

      <IconButton
        aria-label={gridVisible ? "隐藏网格" : "显示网格"}
        tooltip={gridVisible ? "隐藏网格" : "显示网格"}
        aria-pressed={gridVisible}
        size="sm"
        disabled={disabled}
        onClick={() => onGridVisibleChange(!gridVisible)}
        className={cn(
          "motion-reduce:transition-none",
          gridVisible && "bg-[var(--accent-soft)] text-[var(--accent)]",
        )}
      >
        <Grid3X3 className="h-4 w-4" aria-hidden />
      </IconButton>
      <IconButton
        aria-label={minimapVisible ? "隐藏小地图" : "显示小地图"}
        tooltip={minimapVisible ? "隐藏小地图" : "显示小地图"}
        aria-pressed={minimapVisible}
        size="sm"
        disabled={disabled}
        onClick={() => onMinimapVisibleChange(!minimapVisible)}
        className={cn(
          "motion-reduce:transition-none",
          minimapVisible && "bg-[var(--accent-soft)] text-[var(--accent)]",
        )}
      >
        <Map className="h-4 w-4" aria-hidden />
      </IconButton>
    </div>
  );
}
