"use client";

import { AnimatePresence, motion } from "framer-motion";
import type { DragEvent as ReactDragEvent } from "react";
import {
  Paperclip,
  SquareDashedMousePointer,
  X,
} from "lucide-react";

import type { AttachmentImage } from "@/lib/types";
import { DURATION, EASE } from "@/lib/motion";
import { cn } from "@/lib/utils";

import { MAX_COMPOSER_ATTACHMENTS } from "../shared/attachments";
import { AttachmentRoleBadge } from "../shared/AttachmentRoleBadge";
import type { useComposerAttachmentRoles } from "../shared/attachmentRoles";
import { renderWhen, selectValue } from "../shared/composerViewState";
import type { useMaskInpaint } from "../shared/useMaskInpaint";

type AttachmentRoles = ReturnType<typeof useComposerAttachmentRoles>;
type MaskInpaint = ReturnType<typeof useMaskInpaint>;

interface DesktopComposerAttachmentTrayProps {
  attachments: AttachmentImage[];
  attachmentRoles: AttachmentRoles;
  draggingAttachmentId: string | null;
  inpaint: MaskInpaint;
  isDragActive: boolean;
  isImageMode: boolean;
  onAttachmentDragStart: (
    event: ReactDragEvent<HTMLDivElement>,
    id: string,
  ) => void;
  onAttachmentDragOver: (event: ReactDragEvent<HTMLDivElement>) => void;
  onAttachmentDrop: (
    event: ReactDragEvent<HTMLDivElement>,
    targetId: string,
  ) => void;
  onAttachmentDragEnd: () => void;
  onInsertImageMention: (imageNumber: number) => void;
  onRemoveAttachment: (id: string) => void;
}

export function DesktopComposerAttachmentTray({
  attachments,
  attachmentRoles,
  draggingAttachmentId,
  inpaint,
  isDragActive,
  isImageMode,
  onAttachmentDragStart,
  onAttachmentDragOver,
  onAttachmentDrop,
  onAttachmentDragEnd,
  onInsertImageMention,
  onRemoveAttachment,
}: DesktopComposerAttachmentTrayProps) {
  return (
    <>
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
                "mx-3 mt-3 flex items-center justify-center gap-2 rounded-[var(--radius-card)]",
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
                draggable={attachments.length > 1}
                onDragStart={(event) =>
                  onAttachmentDragStart(event, attachment.id)
                }
                onDragOver={onAttachmentDragOver}
                onDrop={(event) => onAttachmentDrop(event, attachment.id)}
                onDragEnd={onAttachmentDragEnd}
                className={cn(
                  "relative shrink-0 w-16 h-16 rounded-[var(--radius-panel)] overflow-hidden",
                  "border bg-[var(--bg-2)]",
                  attachments.length > 1 &&
                    "cursor-grab active:cursor-grabbing",
                  draggingAttachmentId === attachment.id && "opacity-55",
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
                  onClick={() => onInsertImageMention(index + 1)}
                  aria-label={`插入 @图${index + 1}`}
                  title={`插入 @图${index + 1}`}
                  className={cn(
                    "absolute top-0.5 left-0.5 h-5 px-1 rounded-[var(--radius-control)]",
                    "bg-[var(--bg-0)]/80 text-[10px] font-semibold text-[var(--amber-400)]",
                    "backdrop-blur-sm leading-none",
                    "active:scale-[0.94] transition-transform",
                  )}
                  style={{ fontFamily: "var(--font-mono)" }}
                >
                  @图{index + 1}
                </button>
                <AttachmentRoleBadge
                  role={role}
                  imageNumber={index + 1}
                  onClick={() => attachmentRoles.cycleRole(attachment.id)}
                />
                <button
                  type="button"
                  onClick={() => onRemoveAttachment(attachment.id)}
                  aria-label="移除参考图"
                  className={cn(
                    "absolute top-0.5 right-0.5 w-5 h-5 rounded-full",
                    "bg-[var(--media-control-bg)] backdrop-blur-sm text-[var(--media-control-fg)]",
                    "flex items-center justify-center",
                    "active:scale-[0.92] transition-transform",
                  )}
                >
                  <X className="w-3 h-3" aria-hidden />
                </button>
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
                "w-16 h-16 rounded-[var(--radius-panel)] border text-[10px] font-medium",
                "transition-colors",
                selectValue(
                  inpaint.disabled,
                  "border-[var(--border-subtle)] text-[var(--fg-3)] bg-[var(--bg-2)]/40 cursor-not-allowed",
                  selectValue(
                    inpaint.maskActive,
                    "border-[var(--amber-400)]/70 text-[var(--amber-400)] bg-[var(--amber-400)]/10 hover:bg-[var(--amber-400)]/15",
                    "border-dashed border-[var(--border-subtle)] text-[var(--fg-1)] hover:text-[var(--fg-0)] hover:border-[var(--border)]",
                  ),
                ),
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
              )}
            >
              <SquareDashedMousePointer
                className="w-4 h-4"
                aria-hidden
              />
              <span>
                {selectValue(inpaint.maskActive, "重涂", "局部")}
              </span>
            </button>
          ))}
        </div>
      ))}
      {renderWhen(Boolean(attachmentRoles.hint), (
        <div className="px-3 pt-1 text-[11px] leading-4 text-[var(--fg-2)]">
          {attachmentRoles.hint}
        </div>
      ))}
    </>
  );
}
