import { Loader2 } from "lucide-react";
import {
  type RefObject,
  type WheelEventHandler,
} from "react";
import { Group, Image as KonvaImage, Layer, Line, Stage } from "react-konva";
import type Konva from "konva";

import { Button } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

import type { Stroke, Tool } from "../types";
import { isTouchDevice } from "./geometry";
import type {
  DisplayDimensions,
  MaskCursor,
  MaskStagePointerHandler,
  ViewTransform,
} from "./types";

interface MaskBoardCanvasProps {
  boardAreaRef: RefObject<HTMLDivElement | null>;
  stageRef: RefObject<Konva.Stage | null>;
  imgError: string | null;
  imgEl: HTMLImageElement | null;
  imgFadeIn: boolean;
  displayDims: DisplayDimensions;
  view: ViewTransform;
  strokes: Stroke[];
  overlayColor: string;
  cursor: MaskCursor | null;
  disabled: boolean;
  brushSize: number;
  cursorStroke: string;
  cursorFill: string;
  tool: Tool;
  onWheel: WheelEventHandler<HTMLDivElement>;
  onRetry: () => void;
  onPointerDown: MaskStagePointerHandler;
  onPointerMove: MaskStagePointerHandler;
  onPointerUp: MaskStagePointerHandler;
  onPointerLeave: MaskStagePointerHandler;
}

export function MaskBoardCanvas({
  boardAreaRef,
  stageRef,
  imgError,
  imgEl,
  imgFadeIn,
  displayDims,
  view,
  strokes,
  overlayColor,
  cursor,
  disabled,
  brushSize,
  cursorStroke,
  cursorFill,
  tool,
  onWheel,
  onRetry,
  onPointerDown,
  onPointerMove,
  onPointerUp,
  onPointerLeave,
}: MaskBoardCanvasProps) {
  return (
    <div
      ref={boardAreaRef}
      className={cn(
        "relative flex-1 min-h-0 rounded-[var(--radius-card)] bg-[var(--bg-0)]",
        "p-2 sm:p-4 overflow-hidden",
        "flex items-center justify-center",
      )}
      onWheel={onWheel}
    >
      <div className="flex items-center justify-center w-full h-full">
        <MaskBoardCanvasContent
          stageRef={stageRef}
          imgError={imgError}
          imgEl={imgEl}
          imgFadeIn={imgFadeIn}
          displayDims={displayDims}
          view={view}
          strokes={strokes}
          overlayColor={overlayColor}
          cursor={cursor}
          disabled={disabled}
          brushSize={brushSize}
          cursorStroke={cursorStroke}
          cursorFill={cursorFill}
          tool={tool}
          onRetry={onRetry}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerLeave={onPointerLeave}
        />
      </div>
    </div>
  );
}

function MaskBoardCanvasContent({
  stageRef,
  imgError,
  imgEl,
  imgFadeIn,
  displayDims,
  view,
  strokes,
  overlayColor,
  cursor,
  disabled,
  brushSize,
  cursorStroke,
  cursorFill,
  tool,
  onRetry,
  onPointerDown,
  onPointerMove,
  onPointerUp,
  onPointerLeave,
}: Omit<MaskBoardCanvasProps, "boardAreaRef" | "onWheel">) {
  if (imgError) {
    return (
      <div className="flex flex-col items-center gap-2 type-body-sm text-[var(--danger-fg)]">
        <span>{imgError}</span>
        <Button variant="link" onClick={onRetry}>
          重试
        </Button>
      </div>
    );
  }
  if (!imgEl) {
    return (
      <div className="flex items-center gap-2 type-body-sm text-[var(--fg-1)]">
        <Loader2 className="w-4 h-4 animate-spin" />
        加载中
      </div>
    );
  }
  return (
    <MaskCanvasStage
      stageRef={stageRef}
      imgEl={imgEl}
      imgFadeIn={imgFadeIn}
      displayDims={displayDims}
      view={view}
      strokes={strokes}
      overlayColor={overlayColor}
      cursor={cursor}
      disabled={disabled}
      brushSize={brushSize}
      cursorStroke={cursorStroke}
      cursorFill={cursorFill}
      tool={tool}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerLeave={onPointerLeave}
    />
  );
}

function MaskCanvasStage({
  stageRef,
  imgEl,
  imgFadeIn,
  displayDims,
  view,
  strokes,
  overlayColor,
  cursor,
  disabled,
  brushSize,
  cursorStroke,
  cursorFill,
  tool,
  onPointerDown,
  onPointerMove,
  onPointerUp,
  onPointerLeave,
}: Omit<MaskBoardCanvasProps, "boardAreaRef" | "imgError" | "onWheel" | "onRetry"> & {
  imgEl: HTMLImageElement;
}) {
  return (
    <div
      data-mask-canvas-stage
      className={cn(
        "relative rounded-[var(--radius-card)] overflow-hidden border border-[var(--border-subtle)]",
        "shadow-[var(--shadow-1)]",
        "touch-none select-none",
        isTouchDevice() ? null : "cursor-crosshair",
      )}
      style={{
        width: displayDims.width,
        height: displayDims.height,
        opacity: imgFadeIn ? 1 : 0,
        transition: "opacity 220ms ease-out",
      }}
    >
      <Stage
        ref={stageRef}
        width={displayDims.width}
        height={displayDims.height}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerLeave={onPointerLeave}
        onPointerCancel={onPointerUp}
      >
        <Layer listening={false}>
          <Group
            x={view.x}
            y={view.y}
            scaleX={view.scale}
            scaleY={view.scale}
          >
            <KonvaImage
              image={imgEl}
              width={displayDims.width}
              height={displayDims.height}
            />
          </Group>
        </Layer>
        <Layer listening={false}>
          <Group
            x={view.x}
            y={view.y}
            scaleX={view.scale}
            scaleY={view.scale}
          >
            {strokes.map((stroke, index) => (
              <Line
                key={index}
                points={stroke.points}
                stroke={overlayColor}
                strokeWidth={stroke.radius * 2}
                tension={0}
                lineCap="round"
                lineJoin="round"
                globalCompositeOperation={
                  stroke.tool === "brush"
                    ? "source-over"
                    : "destination-out"
                }
              />
            ))}
          </Group>
        </Layer>
      </Stage>

      <MaskCursorPreview
        cursor={cursor}
        disabled={disabled}
        brushSize={brushSize}
        viewScale={view.scale}
        cursorStroke={cursorStroke}
        cursorFill={cursorFill}
        tool={tool}
      />
    </div>
  );
}

function MaskCursorPreview({
  cursor,
  disabled,
  brushSize,
  viewScale,
  cursorStroke,
  cursorFill,
  tool,
}: {
  cursor: MaskCursor | null;
  disabled: boolean;
  brushSize: number;
  viewScale: number;
  cursorStroke: string;
  cursorFill: string;
  tool: Tool;
}) {
  if (!cursor || cursor.pointerType === "touch" || disabled) return null;
  return (
    <div
      aria-hidden
      className="pointer-events-none absolute"
      style={{
        left: cursor.x - brushSize * viewScale,
        top: cursor.y - brushSize * viewScale,
        width: brushSize * 2 * viewScale,
        height: brushSize * 2 * viewScale,
        borderRadius: "50%",
        border: `1.5px solid ${cursorStroke}`,
        background: tool === "brush" ? cursorFill : "transparent",
        boxShadow: "0 0 0 1px rgba(0,0,0,0.32) inset",
      }}
    />
  );
}
