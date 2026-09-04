"use client";

import { ArrowLeft, ArrowRight, Eye, FileText, X } from "lucide-react";
import { IconButton, Select } from "@/components/ui/primitives";
import type { AttachmentRole } from "@/lib/types";
import { cn } from "@/lib/utils";
import type {
  AgentDraftAttachment,
  AgentDraftFile,
} from "../model/contracts";

const ROLE_OPTIONS: Array<{ value: AttachmentRole; label: string }> = [
  { value: "reference", label: "参考" },
  { value: "subject", label: "主体" },
  { value: "product", label: "产品" },
  { value: "style", label: "风格" },
  { value: "edit_target", label: "编辑目标" },
  { value: "background", label: "背景" },
  { value: "other", label: "其他" },
];

export function AgentMediaDrawer({
  attachments,
  files,
  disabled,
  onPreview,
  onRemoveAttachment,
  onMoveAttachment,
  onRoleChange,
  onRemoveFile,
}: {
  attachments: AgentDraftAttachment[];
  files: AgentDraftFile[];
  disabled: boolean;
  onPreview: (attachment: AgentDraftAttachment) => void;
  onRemoveAttachment: (imageId: string) => void;
  onMoveAttachment: (imageId: string, direction: -1 | 1) => void;
  onRoleChange: (imageId: string, role: AttachmentRole) => void;
  onRemoveFile: (name: string) => void;
}) {
  const hasMedia = attachments.length > 0 || files.length > 0;
  const summary = [
    attachments.length > 0 ? `本轮输入 ${attachments.length} 张` : null,
    files.length > 0 ? `文件 ${files.length} 个` : null,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <div
      data-testid="agent-media-drawer"
      data-open={hasMedia ? "true" : "false"}
      aria-hidden={!hasMedia}
      className={cn(
        "grid border-[var(--border-subtle)] transition-[grid-template-rows,opacity,border-color] duration-[var(--dur-collapse)] ease-[var(--ease-develop)] motion-reduce:transition-none",
        hasMedia
          ? "grid-rows-[1fr] border-b opacity-100"
          : "grid-rows-[0fr] border-b-0 opacity-0",
      )}
    >
      <div className="min-h-0 overflow-hidden">
        <div className="flex h-7 items-center px-3 type-caption text-[var(--fg-2)]">
          <span className="truncate">{summary || "本轮媒体"}</span>
        </div>
        <div
          className="scrollbar-thin flex h-[5.25rem] snap-x snap-mandatory gap-2 overflow-x-auto overflow-y-hidden px-2 pb-2"
          aria-label="本轮媒体"
        >
          {attachments.map((attachment, index) => (
            <AgentImageDrawerItem
              key={attachment.imageId}
              attachment={attachment}
              index={index}
              count={attachments.length}
              disabled={disabled}
              onPreview={onPreview}
              onRemove={onRemoveAttachment}
              onMove={onMoveAttachment}
              onRoleChange={onRoleChange}
            />
          ))}
          {files.map((file) => (
            <AgentFileDrawerItem
              key={file.name}
              file={file}
              disabled={disabled}
              onRemove={onRemoveFile}
            />
          ))}
        </div>
      </div>
    </div>
  );
}

function AgentImageDrawerItem({
  attachment,
  index,
  count,
  disabled,
  onPreview,
  onRemove,
  onMove,
  onRoleChange,
}: {
  attachment: AgentDraftAttachment;
  index: number;
  count: number;
  disabled: boolean;
  onPreview: (attachment: AgentDraftAttachment) => void;
  onRemove: (imageId: string) => void;
  onMove: (imageId: string, direction: -1 | 1) => void;
  onRoleChange: (imageId: string, role: AttachmentRole) => void;
}) {
  return (
    <div className="flex h-[4.5rem] min-w-[18rem] snap-start items-center gap-2 rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-2)] p-1.5 sm:min-w-64">
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
            onRoleChange(
              attachment.imageId,
              event.target.value as AttachmentRole,
            )
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
      <div className="flex shrink-0 items-center gap-0.5">
        <IconButton
          size="sm"
          variant="ghost"
          onClick={() => onMove(attachment.imageId, -1)}
          disabled={disabled || index === 0}
          aria-label={`参考图 ${index + 1} 前移`}
          tooltip="前移"
        >
          <ArrowLeft className="h-3.5 w-3.5" aria-hidden />
        </IconButton>
        <IconButton
          size="sm"
          variant="ghost"
          onClick={() => onMove(attachment.imageId, 1)}
          disabled={disabled || index === count - 1}
          aria-label={`参考图 ${index + 1} 后移`}
          tooltip="后移"
        >
          <ArrowRight className="h-3.5 w-3.5" aria-hidden />
        </IconButton>
        <IconButton
          size="sm"
          variant="ghost"
          onClick={() => onRemove(attachment.imageId)}
          disabled={disabled}
          aria-label={`移除参考图 ${index + 1}`}
          tooltip="移除参考图"
        >
          <X className="h-3.5 w-3.5" aria-hidden />
        </IconButton>
      </div>
    </div>
  );
}

function AgentFileDrawerItem({
  file,
  disabled,
  onRemove,
}: {
  file: AgentDraftFile;
  disabled: boolean;
  onRemove: (name: string) => void;
}) {
  return (
    <div className="flex h-[4.5rem] min-w-52 snap-start items-center gap-2 rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-2)] py-2 pl-3 pr-1.5">
      <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-[var(--radius-control)] bg-[var(--bg-1)] text-accent">
        <FileText className="h-4 w-4" aria-hidden />
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate type-caption text-[var(--fg-1)]">
          {file.name}
        </span>
        <span className="block type-caption tabular-nums text-[var(--fg-3)]">
          {formatBytes(file.size)}
        </span>
      </span>
      <IconButton
        size="sm"
        variant="ghost"
        onClick={() => onRemove(file.name)}
        disabled={disabled}
        aria-label={`移除文件 ${file.name}`}
        tooltip="移除文件"
      >
        <X className="h-3.5 w-3.5" aria-hidden />
      </IconButton>
    </div>
  );
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  return `${Math.ceil(bytes / 1024)} KB`;
}
