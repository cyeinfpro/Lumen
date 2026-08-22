"use client";

import { ArrowLeft, ArrowRight, Eye, X } from "lucide-react";
import { IconButton, Select } from "@/components/ui/primitives";
import type { AttachmentRole } from "@/lib/types";
import type { AgentDraftAttachment } from "../model/contracts";

const ROLE_OPTIONS: Array<{ value: AttachmentRole; label: string }> = [
  { value: "reference", label: "参考" },
  { value: "subject", label: "主体" },
  { value: "product", label: "产品" },
  { value: "style", label: "风格" },
  { value: "edit_target", label: "编辑目标" },
  { value: "background", label: "背景" },
  { value: "other", label: "其他" },
];

export function AgentAttachmentTray({
  attachments,
  disabled,
  onPreview,
  onRemove,
  onMove,
  onRoleChange,
}: {
  attachments: AgentDraftAttachment[];
  disabled: boolean;
  onPreview: (attachment: AgentDraftAttachment) => void;
  onRemove: (imageId: string) => void;
  onMove: (imageId: string, direction: -1 | 1) => void;
  onRoleChange: (imageId: string, role: AttachmentRole) => void;
}) {
  if (attachments.length === 0) return null;

  return (
    <div
      className="grid gap-2 border-b border-[var(--border-subtle)] p-3 sm:grid-cols-2"
      aria-label="参考图"
    >
      {attachments.map((attachment, index) => (
        <div
          key={attachment.imageId}
          className="flex min-w-0 items-center gap-2 rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-2)] p-2"
        >
          <button
            type="button"
            onClick={() => onPreview(attachment)}
            className="relative h-12 w-12 shrink-0 overflow-hidden rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-1)]"
            aria-label={`${attachment.name || "参考图"} ${index + 1}，预览`}
          >
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={attachment.previewUrl}
              alt={`${attachment.name || "参考图"} ${index + 1}`}
              className="h-full w-full object-cover"
            />
            <Eye className="absolute bottom-0.5 right-0.5 h-3.5 w-3.5 rounded-full bg-[var(--media-control-bg)] p-0.5 text-[var(--media-control-fg)]" aria-hidden />
          </button>
          <div className="min-w-0 flex-1">
            <p className="truncate type-caption text-[var(--fg-1)]">
              {index + 1}. {attachment.name}
            </p>
            <Select
              value={attachment.role}
              onChange={(event) =>
                onRoleChange(attachment.imageId, event.target.value as AttachmentRole)
              }
              disabled={disabled}
              aria-label={`参考图 ${index + 1} 角色`}
              className="mt-1 h-9"
            >
              {ROLE_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </Select>
          </div>
          <div className="flex shrink-0 flex-col gap-0.5">
            <IconButton
              size="sm"
              variant="ghost"
              onClick={() => onRemove(attachment.imageId)}
              disabled={disabled}
              aria-label={`移除参考图 ${index + 1}`}
            >
              <X className="h-3.5 w-3.5" aria-hidden />
            </IconButton>
            <div className="flex">
              <IconButton
                size="sm"
                variant="ghost"
                onClick={() => onMove(attachment.imageId, -1)}
                disabled={disabled || index === 0}
                aria-label={`参考图 ${index + 1} 前移`}
              >
                <ArrowLeft className="h-3.5 w-3.5" aria-hidden />
              </IconButton>
              <IconButton
                size="sm"
                variant="ghost"
                onClick={() => onMove(attachment.imageId, 1)}
                disabled={disabled || index === attachments.length - 1}
                aria-label={`参考图 ${index + 1} 后移`}
              >
                <ArrowRight className="h-3.5 w-3.5" aria-hidden />
              </IconButton>
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}
