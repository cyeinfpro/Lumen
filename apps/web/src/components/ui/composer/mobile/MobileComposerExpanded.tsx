"use client";

import { AnimatePresence, motion } from "framer-motion";
import {
  Loader2,
  Paperclip,
  Sparkles,
  SquareDashedMousePointer,
  X,
} from "lucide-react";
import type {
  ChangeEvent,
  ClipboardEvent,
  KeyboardEvent,
  MouseEvent as ReactMouseEvent,
  PointerEvent as ReactPointerEvent,
  RefObject,
} from "react";

import type {
  AspectRatio,
  AttachmentImage,
  Quality,
  RenderQualityChoice,
} from "@/lib/types";
import { DURATION, EASE } from "@/lib/motion";
import { MAX_PROMPT_CHARS } from "@/lib/promptLimits";
import { cn } from "@/lib/utils";

import { MAX_COMPOSER_ATTACHMENTS } from "../shared/attachments";
import { attachmentRoleLabel } from "../shared/attachmentRoles";
import type { useComposerAttachmentRoles } from "../shared/attachmentRoles";
import type { ComposerExecutionSummary } from "../shared/executionSummary";
import {
  allFlags,
  anyFlag,
  renderWhen,
  selectValue,
} from "../shared/composerViewState";
import { PromptEnhancementCandidate } from "../shared/PromptEnhancementCandidate";
import type { usePromptEnhancementCandidate } from "../shared/PromptEnhancementCandidate";
import type { useMaskInpaint } from "../shared/useMaskInpaint";
import { MobileComposerExecutionControls } from "./MobileComposerExecutionControls";
import {
  MobileComposerIconButton,
  MobileComposerModeSegment,
  MobileComposerSendButton,
  type MobileComposerMode,
} from "./MobileComposerButtons";
import {
  promptCounterColor,
  promptCounterText,
} from "./mobileComposerViewState";

type PromptEnhancement = ReturnType<typeof usePromptEnhancementCandidate>;
type AttachmentRoles = ReturnType<typeof useComposerAttachmentRoles>;
type Inpaint = ReturnType<typeof useMaskInpaint>;

interface MobileComposerExpandedProps {
  textareaRef: RefObject<HTMLTextAreaElement | null>;
  text: string;
  mode: MobileComposerMode;
  attachments: AttachmentImage[];
  isUploading: boolean;
  isDragActive: boolean;
  isEnhancing: boolean;
  isSending: boolean;
  shutterBurst: boolean;
  canSubmit: boolean;
  shouldShowCount: boolean;
  promptTooLong: boolean;
  composerError: string | null;
  expandedPaddingBottom: string;
  draggingAttachmentId: string | null;
  reorderTargetAttachmentId: string | null;
  attachmentRoles: AttachmentRoles;
  inpaint: Inpaint;
  promptEnhancement: PromptEnhancement;
  executionSummary: ComposerExecutionSummary;
  count: number;
  aspect: AspectRatio;
  quality: Quality;
  renderQuality: RenderQualityChoice;
  fast: boolean;
  costLabel?: string | null;
  costWarning?: boolean;
  onCollapse: () => void;
  onTextChange: (event: ChangeEvent<HTMLTextAreaElement>) => void;
  onKeyDown: (event: KeyboardEvent<HTMLTextAreaElement>) => void;
  onPaste: (
    event: ClipboardEvent<HTMLTextAreaElement>,
  ) => void | Promise<void>;
  onCompositionStart: () => void;
  onCompositionEnd: () => void;
  onOpenFilePicker: () => void;
  onOpenAttachmentMenu: (id: string) => void;
  onBeginAttachmentReorder: (
    event: ReactPointerEvent<HTMLDivElement>,
    id: string,
  ) => void;
  onAttachmentClickCapture: (
    event: ReactMouseEvent<HTMLDivElement>,
  ) => void;
  onClearComposerError: () => void;
  onCountChange: (value: number) => void;
  onOpenAspect: () => void;
  onQualityChange: (value: Quality) => void;
  onRenderQualityChange: (value: RenderQualityChoice) => void;
  onFastChange: (value: boolean) => void;
  onOpenAdvanced: () => void;
  onModeChange: (value: MobileComposerMode) => void;
  onSubmit: () => void;
}

export function MobileComposerExpanded({
  textareaRef,
  text,
  mode,
  attachments,
  isUploading,
  isDragActive,
  isEnhancing,
  isSending,
  shutterBurst,
  canSubmit,
  shouldShowCount,
  promptTooLong,
  composerError,
  expandedPaddingBottom,
  draggingAttachmentId,
  reorderTargetAttachmentId,
  attachmentRoles,
  inpaint,
  promptEnhancement,
  executionSummary,
  count,
  aspect,
  quality,
  renderQuality,
  fast,
  costLabel,
  costWarning,
  onCollapse,
  onTextChange,
  onKeyDown,
  onPaste,
  onCompositionStart,
  onCompositionEnd,
  onOpenFilePicker,
  onOpenAttachmentMenu,
  onBeginAttachmentReorder,
  onAttachmentClickCapture,
  onClearComposerError,
  onCountChange,
  onOpenAspect,
  onQualityChange,
  onRenderQualityChange,
  onFastChange,
  onOpenAdvanced,
  onModeChange,
  onSubmit,
}: MobileComposerExpandedProps) {
  const isImageMode = mode === "image";

  return (
    <div
      className="flex max-h-[inherit] min-h-0 flex-col overflow-y-auto overscroll-contain touch-pan-y"
      style={{ paddingBottom: expandedPaddingBottom }}
    >
      <button
        type="button"
        onPointerDown={(event: ReactPointerEvent) => event.preventDefault()}
        onClick={onCollapse}
        className="flex min-h-11 w-full items-center justify-center py-2 cursor-pointer active:opacity-60"
        aria-label="收起输入框"
      >
        <div className="w-9 h-1 rounded-full bg-[var(--fg-3)]/40" />
      </button>

      <AnimatePresence>
        {renderWhen(isDragActive, (
          <motion.div
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            transition={{ duration: DURATION.quick, ease: EASE.shutter }}
          >
            <div
              className={cn(
                "mx-3 mt-2 flex items-center justify-center gap-2 rounded-[var(--radius-card)]",
                "border border-dashed border-[var(--amber-400)]/60 bg-[var(--amber-400)]/10",
                "px-3 py-3 text-xs text-[var(--amber-400)]",
              )}
            >
              <Paperclip className="h-3.5 w-3.5" aria-hidden />
              <span>松开上传图片，最多 {MAX_COMPOSER_ATTACHMENTS} 张</span>
            </div>
          </motion.div>
        ))}
      </AnimatePresence>

      {renderWhen(attachments.length > 0, (
        <div
          className={cn(
            "flex gap-2 overflow-x-auto overscroll-x-contain no-scrollbar",
            "px-3 pt-3",
          )}
        >
          {attachments.map((attachment, index) => {
            const isFirst = index === 0;
            const showMaskBadge = isFirst && inpaint.maskActive;
            const role = attachmentRoles.getRole(attachment.id);
            return (
              <div
                key={attachment.id}
                data-composer-attachment-id={attachment.id}
                onPointerDown={(event) =>
                  onBeginAttachmentReorder(event, attachment.id)
                }
                onClickCapture={onAttachmentClickCapture}
                aria-grabbed={
                  draggingAttachmentId === attachment.id || undefined
                }
                className={cn(
                  "relative h-16 w-16 shrink-0 overflow-hidden rounded-[var(--radius-card)]",
                  "border bg-[var(--bg-2)]",
                  attachments.length > 1 &&
                    "cursor-grab active:cursor-grabbing",
                  draggingAttachmentId === attachment.id &&
                    "opacity-55 scale-[0.98]",
                  reorderTargetAttachmentId === attachment.id &&
                    "ring-2 ring-[var(--amber-400)]/70",
                  showMaskBadge
                    ? "border-[var(--amber-400)]/70"
                    : "border-[var(--border-subtle)]",
                )}
              >
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img
                  src={attachment.data_url}
                  alt=""
                  draggable={false}
                  className="w-full h-full object-cover"
                  loading="lazy"
                />
                <button
                  type="button"
                  data-composer-attachment-action="true"
                  onClick={() => onOpenAttachmentMenu(attachment.id)}
                  aria-label={`打开图 ${index + 1} 操作`}
                  aria-haspopup="dialog"
                  className="absolute inset-0 z-10 rounded-[var(--radius-card)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[var(--focus-ring)]"
                >
                  <span className="sr-only">打开附件操作</span>
                </button>
                <span
                  aria-hidden
                  className="pointer-events-none absolute left-1 top-1 rounded-[var(--radius-control)] bg-[var(--media-control-bg)] px-1.5 py-1 text-[9px] font-semibold leading-none text-[var(--media-control-fg)] backdrop-blur-sm"
                  style={{ fontFamily: "var(--font-mono)" }}
                >
                  @图{index + 1}
                </span>
                <span className="pointer-events-none absolute inset-x-1 bottom-1 truncate rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)]/88 px-1.5 py-1 text-center text-[9px] font-semibold leading-none text-[var(--fg-0)] backdrop-blur-sm">
                  {attachmentRoleLabel(role)}
                </span>
              </div>
            );
          })}
          {renderWhen(isImageMode, (
            <button
              type="button"
              onClick={inpaint.openInpaint}
              disabled={inpaint.disabled}
              aria-label="局部修改"
              title={inpaint.tooltip}
              className={cn(
                "shrink-0 inline-flex flex-col items-center justify-center gap-0.5",
                "w-12 h-12 rounded-[var(--radius-card)] border text-[9px] font-medium",
                "transition-colors",
                selectValue(
                  inpaint.disabled,
                  "border-[var(--border-subtle)] text-[var(--fg-3)] bg-[var(--bg-2)]/40 cursor-not-allowed",
                  selectValue(
                    inpaint.maskActive,
                    "border-[var(--amber-400)]/70 text-[var(--amber-400)] bg-[var(--amber-400)]/10",
                    "border-dashed border-[var(--border-subtle)] text-[var(--fg-1)]",
                  ),
                ),
              )}
            >
              <SquareDashedMousePointer
                className="w-3.5 h-3.5"
                aria-hidden
              />
              <span>{selectValue(inpaint.maskActive, "重涂", "局部")}</span>
            </button>
          ))}
        </div>
      ))}
      {renderWhen(Boolean(attachmentRoles.compactHint), (
        <div className="px-3 pt-1 text-[10.5px] leading-4 text-[var(--fg-2)] line-clamp-1">
          {attachmentRoles.compactHint}
        </div>
      ))}

      <AnimatePresence>
        {renderWhen(Boolean(composerError), (
          <motion.div
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            transition={{ duration: DURATION.quick, ease: EASE.shutter }}
          >
            <div
              role="alert"
              className={cn(
                "mx-3 mt-2 flex items-start gap-2 px-2.5 py-1.5 rounded-[var(--radius-card)]",
                "bg-danger-soft border border-danger-border",
                "type-caption text-[var(--danger-fg)]",
              )}
            >
              <span className="flex-1 break-words">{composerError}</span>
              <button
                type="button"
                aria-label="关闭错误提示"
                onClick={onClearComposerError}
                className="shrink-0 w-5 h-5 inline-flex items-center justify-center rounded-[var(--radius-control)] active:bg-[var(--bg-2)]"
              >
                <X className="w-3 h-3" />
              </button>
            </div>
          </motion.div>
        ))}
      </AnimatePresence>

      <PromptEnhancementCandidate
        status={promptEnhancement.status}
        candidate={promptEnhancement.candidate}
        onApply={promptEnhancement.apply}
        onCancel={promptEnhancement.cancel}
        onDiscard={promptEnhancement.discard}
      />

      <div className="relative px-3 pt-1.5 pb-1">
        <textarea
          ref={textareaRef}
          value={text}
          onChange={onTextChange}
          onKeyDown={onKeyDown}
          onPaste={onPaste}
          onCompositionStart={onCompositionStart}
          onCompositionEnd={onCompositionEnd}
          readOnly={isEnhancing}
          placeholder={selectValue(
            isImageMode,
            "描述画面...",
            "直接提问...",
          )}
          aria-label="输入提示词"
          maxLength={MAX_PROMPT_CHARS}
          rows={2}
          className={cn(
            "w-full bg-transparent outline-none resize-none",
            "text-[16px] leading-relaxed text-[var(--fg-0)] placeholder:text-[var(--fg-2)]",
            "min-h-[52px] max-h-[168px]",
            selectValue(isEnhancing, "cursor-wait", undefined),
          )}
        />
      </div>

      <MobileComposerExecutionControls
        mode={mode}
        summary={executionSummary}
        count={count}
        onCountChange={onCountChange}
        aspect={aspect}
        onOpenAspect={onOpenAspect}
        quality={quality}
        onQualityChange={onQualityChange}
        renderQuality={renderQuality}
        onRenderQualityChange={onRenderQualityChange}
        fast={fast}
        onFastChange={onFastChange}
        attachmentCount={attachments.length}
        costLabel={costLabel}
        costWarning={costWarning}
        onAdjust={onOpenAdvanced}
      />

      <div className="mx-3 h-px bg-[var(--border-subtle)]" />

      <div className="flex items-end gap-2 px-3 pb-3 pt-2">
        <div className="flex-1 min-w-0 flex flex-col gap-2">
          <MobileComposerModeSegment
            value={mode}
            onChange={onModeChange}
            className="w-full"
          />

          <div className="flex flex-wrap items-center gap-1.5">
            <MobileComposerIconButton
              label="添加参考图"
              onClick={onOpenFilePicker}
              disabled={isUploading}
            >
              {selectValue(
                isUploading,
                <Loader2 className="w-4 h-4 animate-spin" />,
                <Paperclip className="w-4 h-4" />,
              )}
            </MobileComposerIconButton>

            <MobileComposerIconButton
              label={promptEnhancement.triggerLabel}
              onClick={() => void promptEnhancement.trigger(text)}
              disabled={allFlags(!isEnhancing, !text.trim())}
            >
              {selectValue(
                isEnhancing,
                <X className="w-4 h-4 text-[var(--danger)]" />,
                <Sparkles className="w-4 h-4" />,
              )}
            </MobileComposerIconButton>

            <span
              className="mx-0.5 h-5 w-px shrink-0 bg-[var(--border-subtle)]"
              aria-hidden
            />

            {renderWhen(anyFlag(text.length > 0, shouldShowCount), (
              <span
                data-inline
                className={cn(
                  "text-caption tabular-nums transition-colors duration-200",
                  promptCounterColor(
                    promptTooLong,
                    shouldShowCount,
                    text.length,
                  ),
                )}
                style={{ fontFamily: "var(--font-mono)" }}
              >
                {promptCounterText(shouldShowCount, text.length)}
              </span>
            ))}
          </div>
        </div>

        <MobileComposerSendButton
          canSubmit={canSubmit}
          isSending={isSending}
          burst={shutterBurst}
          onClick={onSubmit}
        />
      </div>
    </div>
  );
}
