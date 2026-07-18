"use client";

import { motion } from "framer-motion";
import { Loader2, Sparkles, X } from "lucide-react";
import {
  type KeyboardEvent as ReactKeyboardEvent,
  type RefObject,
} from "react";

import { Button, IconButton, Textarea, Tooltip } from "@/components/ui/primitives";
import { MAX_PROMPT_CHARS } from "@/lib/promptLimits";
import { cn } from "@/lib/utils";
import type { InpaintSource } from "@/store/useInpaintStore";

import { MaskBoard, type MaskBoardHandle } from "./MaskBoard";
import type { Stroke } from "./types";

const SOFT_PROMPT_LIMIT = 1500;

interface InpaintModalViewProps {
  source: InpaintSource | null;
  rootRef: RefObject<HTMLDivElement | null>;
  boardRef: RefObject<MaskBoardHandle | null>;
  promptRef: RefObject<HTMLTextAreaElement | null>;
  initialStrokes: Stroke[] | null;
  submitting: boolean;
  prompt: string;
  hasStroke: boolean;
  coverage: number;
  warning: string | null;
  confirmingClose: boolean;
  derivedAspect: string | null;
  promptOverSoftLimit: boolean;
  promptOverHardLimit: boolean;
  canSubmit: boolean;
  onClose: () => void;
  onKeyDown: (event: ReactKeyboardEvent<HTMLDivElement>) => void;
  onPromptChange: (value: string) => void;
  onPointerDownCanvas: () => void;
  onStrokesChange: (strokes: Stroke[]) => void;
  onStatsChange: (stats: { coverage: number; strokeCount: number }) => void;
  onSubmit: () => void;
}

export function InpaintModalView({
  source,
  rootRef,
  boardRef,
  promptRef,
  initialStrokes,
  submitting,
  prompt,
  hasStroke,
  coverage,
  warning,
  confirmingClose,
  derivedAspect,
  promptOverSoftLimit,
  promptOverHardLimit,
  canSubmit,
  onClose,
  onKeyDown,
  onPromptChange,
  onPointerDownCanvas,
  onStrokesChange,
  onStatsChange,
  onSubmit,
}: InpaintModalViewProps) {
  return (
    <motion.div
      key="inpaint-overlay"
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.16 }}
      className={cn(
        "fixed inset-0 z-[var(--z-dialog)]",
        "bg-black/76 backdrop-blur-md",
        "mobile-dialog-shell",
        "flex items-end justify-center sm:items-center",
      )}
      role="presentation"
      onPointerDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <motion.div
        ref={rootRef}
        role="dialog"
        aria-modal="true"
        aria-label="局部修改"
        initial={{ opacity: 0, scale: 0.96, y: 8 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.96, y: 8 }}
        transition={{ duration: 0.2, ease: [0.22, 1, 0.36, 1] }}
        onKeyDown={onKeyDown}
        className={cn(
          "mobile-dialog-panel",
          "w-full max-w-[1100px]",
          "h-[var(--mobile-dialog-max-height)] sm:h-[760px] sm:max-h-[calc(100dvh-3rem)]",
          "flex flex-col overflow-hidden",
          "max-sm:rounded-t-[var(--radius-sheet)] max-sm:rounded-b-none sm:rounded-[var(--radius-dialog)]",
          "border border-[var(--border)] bg-[var(--bg-1)]",
          "shadow-[var(--shadow-2)]",
        )}
      >
        <InpaintModalHeader
          source={source}
          confirmingClose={confirmingClose}
          submitting={submitting}
          onClose={onClose}
        />
        <div
          className={cn(
            "flex-1 min-h-0 overflow-hidden",
            "flex flex-col md:flex-row",
          )}
        >
          <InpaintCanvasPanel
            source={source}
            boardRef={boardRef}
            initialStrokes={initialStrokes}
            submitting={submitting}
            onPointerDown={onPointerDownCanvas}
            onStrokesChange={onStrokesChange}
            onStatsChange={onStatsChange}
          />
          <InpaintPromptPanel
            promptRef={promptRef}
            submitting={submitting}
            prompt={prompt}
            hasStroke={hasStroke}
            coverage={coverage}
            warning={warning}
            confirmingClose={confirmingClose}
            derivedAspect={derivedAspect}
            promptOverSoftLimit={promptOverSoftLimit}
            promptOverHardLimit={promptOverHardLimit}
            canSubmit={canSubmit}
            onClose={onClose}
            onPromptChange={onPromptChange}
            onSubmit={onSubmit}
          />
        </div>
      </motion.div>
    </motion.div>
  );
}

function InpaintModalHeader({
  source,
  confirmingClose,
  submitting,
  onClose,
}: Pick<
  InpaintModalViewProps,
  "source" | "confirmingClose" | "submitting" | "onClose"
>) {
  return (
    <div className="flex items-center justify-between gap-3 px-4 py-3 border-b border-[var(--border-subtle)]">
      <div className="flex items-center gap-3 min-w-0">
        {source ? (
          <div
            className={cn(
              "shrink-0 w-9 h-9 sm:w-10 sm:h-10 rounded-[var(--radius-control)] overflow-hidden",
              "border border-[var(--border-subtle)] bg-[var(--bg-2)]",
            )}
            aria-hidden
            title={source.alt ?? "源图"}
          >
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={source.src}
              alt=""
              className="w-full h-full object-cover"
              draggable={false}
            />
          </div>
        ) : null}
        <div className="flex flex-col min-w-0">
          <h2 className="type-card-title">局部修改</h2>
          <p className="type-body-sm text-[var(--fg-1)] truncate">
            涂抹要修改的区域 · 描述要替换成什么
          </p>
        </div>
      </div>
      <IconButton
        variant={confirmingClose ? "danger" : "ghost"}
        onClick={onClose}
        disabled={submitting}
        aria-label={confirmingClose ? "确认放弃涂抹" : "关闭"}
        tooltip={confirmingClose ? "再点一次确认放弃" : "关闭 (Esc)"}
        className="rounded-full"
      >
        <X className="w-4 h-4" />
      </IconButton>
    </div>
  );
}

interface InpaintCanvasPanelProps {
  source: InpaintSource | null;
  boardRef: RefObject<MaskBoardHandle | null>;
  initialStrokes: Stroke[] | null;
  submitting: boolean;
  onPointerDown: () => void;
  onStrokesChange: (strokes: Stroke[]) => void;
  onStatsChange: (stats: { coverage: number; strokeCount: number }) => void;
}

function InpaintCanvasPanel({
  source,
  boardRef,
  initialStrokes,
  submitting,
  onPointerDown,
  onStrokesChange,
  onStatsChange,
}: InpaintCanvasPanelProps) {
  return (
    <div
      className={cn(
        "flex-1 min-w-0 min-h-0 overflow-hidden p-3 sm:p-4 bg-[var(--bg-1)]",
        "md:border-r md:border-[var(--border-subtle)]",
      )}
      onPointerDown={onPointerDown}
    >
      {!source ? (
        <div className="flex h-full items-center justify-center type-body-sm text-[var(--fg-1)]">
          <Loader2 className="w-4 h-4 animate-spin mr-2" />
          图片加载中
        </div>
      ) : (
        <MaskBoard
          ref={boardRef}
          imageSrc={source.src}
          disabled={submitting}
          initialStrokes={initialStrokes}
          onStrokesChange={onStrokesChange}
          onStatsChange={onStatsChange}
        />
      )}
    </div>
  );
}

interface InpaintPromptPanelProps {
  promptRef: RefObject<HTMLTextAreaElement | null>;
  submitting: boolean;
  prompt: string;
  hasStroke: boolean;
  coverage: number;
  warning: string | null;
  confirmingClose: boolean;
  derivedAspect: string | null;
  promptOverSoftLimit: boolean;
  promptOverHardLimit: boolean;
  canSubmit: boolean;
  onClose: () => void;
  onPromptChange: (value: string) => void;
  onSubmit: () => void;
}

function InpaintPromptPanel({
  promptRef,
  submitting,
  prompt,
  hasStroke,
  coverage,
  warning,
  confirmingClose,
  derivedAspect,
  promptOverSoftLimit,
  promptOverHardLimit,
  canSubmit,
  onClose,
  onPromptChange,
  onSubmit,
}: InpaintPromptPanelProps) {
  const promptCounterClass = cn(
    "tabular-nums",
    promptOverHardLimit
      ? "text-danger"
      : promptOverSoftLimit
        ? "text-warning"
        : "text-[var(--fg-1)]/80",
  );

  return (
    <div
      className={cn(
        "shrink-0 flex flex-col gap-3 p-3 sm:p-4",
        "md:w-[320px] md:max-w-[320px]",
        "max-md:max-h-[min(44dvh,20rem)]",
        "bg-[var(--bg-0)]",
        "mobile-dialog-scroll overflow-y-auto",
        "border-t border-[var(--border-subtle)] md:border-t-0",
      )}
    >
      <div>
        <div className="mb-1.5 flex items-center justify-between gap-2">
          <label
            htmlFor="inpaint-prompt"
            className="block text-[12px] font-medium text-[var(--fg-1)]"
          >
            把涂抹区域改成什么？
          </label>
          {derivedAspect ? (
            <span
              className={cn(
                "shrink-0 inline-flex items-center gap-1 px-2 h-5 rounded-full",
                "text-[10px] tabular-nums",
                "bg-[var(--bg-2)] text-[var(--fg-1)] border border-[var(--border-subtle)]",
              )}
              title="按原图比例生成（避免构图变形）"
            >
              <span className="text-[var(--fg-2)]">比例</span>
              {derivedAspect}
            </span>
          ) : null}
        </div>
        <Textarea
          id="inpaint-prompt"
          ref={promptRef}
          value={prompt}
          onChange={(event) =>
            onPromptChange(event.target.value.slice(0, MAX_PROMPT_CHARS))
          }
          placeholder="描述涂抹区域要变成什么"
          rows={3}
          className={cn(
            "resize-none min-h-[84px] md:min-h-[120px]",
            promptOverHardLimit && "border-[var(--danger)]",
          )}
          disabled={submitting}
        />
        <div className="mt-1 flex items-center justify-between text-[11px] text-[var(--fg-1)]/80">
          <span className="truncate">⌘/Ctrl + Enter 提交</span>
          <span className={promptCounterClass}>
            {prompt.length}/{SOFT_PROMPT_LIMIT}
          </span>
        </div>
      </div>

      <div className="hidden md:block rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-1)]/40 p-2.5 text-[11.5px] leading-relaxed text-[var(--fg-1)]/90">
        <strong className="font-medium text-[var(--fg-0)]">提示</strong>
        ：仅描述涂抹区域，越具体越准。
        <Tooltip
          content="不要描述整张图；只写涂抹区域要变成什么。"
          side="top"
        >
          <span className="ml-1 text-[var(--info)] underline decoration-dotted cursor-help">
            详解
          </span>
        </Tooltip>
      </div>

      {warning ? (
        <motion.div
          initial={{ opacity: 0, y: -4 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -4 }}
          transition={{ duration: 0.18 }}
          className={cn(
            "rounded-[var(--radius-control)] p-2 text-[11.5px]",
            "bg-warning-soft text-warning",
          )}
          role="status"
          aria-live="polite"
        >
          {warning}
        </motion.div>
      ) : null}

      <div className="hidden md:block text-[10.5px] text-[var(--fg-1)]/70 leading-relaxed">
        <div className="grid grid-cols-2 gap-x-2 gap-y-0.5">
          <span>
            <Kbd>B</Kbd> 画笔 / <Kbd>E</Kbd> 橡皮
          </span>
          <span>
            <Kbd>[</Kbd> <Kbd>]</Kbd> 调画笔
          </span>
          <span>
            <Kbd>Z</Kbd> 撤销
          </span>
          <span>
            <Kbd>Esc</Kbd> 关闭
          </span>
        </div>
      </div>

      <div className="mobile-dialog-footer mt-auto flex items-center justify-end gap-2 pt-2">
        <Button
          variant={confirmingClose ? "danger" : "ghost"}
          size="md"
          onClick={onClose}
          disabled={submitting}
        >
          {confirmingClose ? "确认放弃" : "取消"}
        </Button>
        <Button
          variant="primary"
          size="md"
          onClick={onSubmit}
          disabled={!canSubmit}
          loading={submitting}
          className="min-w-[112px]"
        >
          {!hasStroke ? (
            "未涂抹"
          ) : !prompt.trim() ? (
            "指令为空"
          ) : promptOverHardLimit ? (
            "字数超限"
          ) : (
            <>
              <Sparkles className="w-3.5 h-3.5" />
              生成
            </>
          )}
        </Button>
      </div>

      <div className="md:hidden -mt-1 text-[11px] text-[var(--fg-1)]/70 text-right">
        {hasStroke ? `已涂抹 ${Math.round(coverage * 100)}%` : "未涂抹"}
      </div>
    </div>
  );
}

function Kbd({ children }: { children: React.ReactNode }) {
  return (
    <kbd
      className={cn(
        "inline-flex items-center justify-center min-w-4 h-4 px-1 mx-0.5 rounded",
        "border border-[var(--border-subtle)] bg-[var(--bg-2)]",
        "text-[9.5px] font-mono text-[var(--fg-1)]",
      )}
    >
      {children}
    </kbd>
  );
}
