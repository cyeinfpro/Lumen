"use client";

// MaskBoard is shared by the composer and the standalone inpaint modal.
// Rendering, input gestures, board state, and geometry live in mask-board/.

import type Konva from "konva";
import {
  type CSSProperties,
  forwardRef,
  useImperativeHandle,
  useRef,
} from "react";

import { cn } from "@/lib/utils";

import { MaskBoardCanvas } from "./mask-board/MaskBoardCanvas";
import { MaskBoardToolbar } from "./mask-board/MaskBoardToolbar";
import { clampBrush } from "./mask-board/geometry";
import type { MaskBoardHandle } from "./mask-board/types";
import { useMaskBoardState } from "./mask-board/useMaskBoardState";
import { useMaskPointerInteraction } from "./mask-board/useMaskPointerInteraction";
import type { Stroke } from "./types";

export type { MaskBoardHandle, MaskExport } from "./mask-board/types";
export type { Stroke, Tool } from "./types";

interface MaskBoardProps {
  imageSrc: string;
  /** 提交中：禁止笔画与工具切换 */
  disabled?: boolean;
  /** 初始 strokes（用于回填上次未提交的草稿，仅 mount 时生效） */
  initialStrokes?: Stroke[] | null;
  /** strokes 变化（去抖 380ms）— 父组件可写入 store 持久化 */
  onStrokesChange?: (strokes: Stroke[]) => void;
  /** 实时统计回调（覆盖率 0..1，stroke 数量） */
  onStatsChange?: (stats: { coverage: number; strokeCount: number }) => void;
  className?: string;
  style?: CSSProperties;
}

export const MaskBoard = forwardRef<MaskBoardHandle, MaskBoardProps>(
  function MaskBoard(
    {
      imageSrc,
      disabled,
      initialStrokes,
      onStrokesChange,
      onStatsChange,
      className,
      style,
    },
    ref,
  ) {
    const stageRef = useRef<Konva.Stage | null>(null);
    const containerRef = useRef<HTMLDivElement | null>(null);
    const state = useMaskBoardState({
      imageSrc,
      initialStrokes,
      onStrokesChange,
      onStatsChange,
    });
    const interaction = useMaskPointerInteraction({
      stageRef,
      disabled: Boolean(disabled),
      imageSrc,
      displayKey: state.displayKey,
      displayDims: state.displayDims,
      tool: state.tool,
      brushSize: state.brushSize,
      setBrushSize: state.setBrushSize,
      setStrokes: state.setStrokes,
      setCursor: state.setCursor,
      view: state.view,
      setView: state.setView,
    });
    const exportMask = state.exportMask;
    const setStrokes = state.setStrokes;
    const strokeCount = state.strokes.length;

    useImperativeHandle(
      ref,
      () => ({
        exportMask,
        hasStrokes: () => strokeCount > 0,
        clear: () => setStrokes([]),
      }),
      [exportMask, setStrokes, strokeCount],
    );

    return (
      <div
        ref={containerRef}
        className={cn("flex flex-col gap-3 h-full min-h-0", className)}
        style={style}
        onPointerDown={interaction.onContainerPointerDown}
      >
        <MaskBoardCanvas
          boardAreaRef={state.boardAreaRef}
          stageRef={stageRef}
          imgError={state.imgError}
          imgEl={state.imgEl}
          imgFadeIn={state.imgFadeIn}
          displayDims={state.displayDims}
          view={state.view}
          strokes={state.strokes}
          overlayColor={state.overlayColor}
          cursor={state.cursor}
          disabled={Boolean(disabled)}
          brushSize={state.brushSize}
          cursorStroke={state.cursorStroke}
          cursorFill={state.cursorFill}
          tool={state.tool}
          onWheel={interaction.handleWheel}
          onRetry={state.retryImage}
          onPointerDown={interaction.handlePointerDown}
          onPointerMove={interaction.handlePointerMove}
          onPointerUp={interaction.handlePointerUp}
          onPointerLeave={interaction.handlePointerLeave}
        />
        <MaskBoardToolbar
          tool={state.tool}
          brushSize={state.brushSize}
          disabled={Boolean(disabled)}
          isDarkBg={state.isDarkBg}
          hasImage={Boolean(state.imgEl)}
          view={state.view}
          viewIsFit={state.viewIsFit}
          hasStroke={state.hasStroke}
          liveCoverage={state.liveCoverage}
          strokeCount={strokeCount}
          onToolChange={state.setTool}
          onBrushSizeChange={(value) =>
            state.setBrushSize(clampBrush(value))
          }
          onFitView={interaction.fitView}
          onUndo={state.undo}
          onReset={state.reset}
        />
      </div>
    );
  },
);
