"use client";

import { FileText, X } from "lucide-react";
import { IconButton } from "@/components/ui/primitives";
import type { AgentDraftFile } from "../model/contracts";

export function AgentFileTray({
  files,
  disabled,
  onRemove,
}: {
  files: AgentDraftFile[];
  disabled: boolean;
  onRemove: (name: string) => void;
}) {
  if (files.length === 0) return null;
  return (
    <div
      className="flex max-h-32 flex-wrap gap-2 overflow-y-auto border-b border-[var(--border-subtle)] px-3 py-2"
      aria-label="文本文件"
    >
      {files.map((file) => (
        <div
          key={file.name}
          className="flex min-w-0 max-w-full items-center gap-2 rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-2)] py-1 pl-2"
        >
          <FileText className="h-4 w-4 shrink-0 text-accent" aria-hidden />
          <span className="min-w-0 truncate type-caption text-[var(--fg-1)]">
            {file.name}
          </span>
          <span className="shrink-0 type-caption tabular-nums text-[var(--fg-3)]">
            {formatBytes(file.size)}
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
      ))}
    </div>
  );
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  return `${Math.ceil(bytes / 1024)} KB`;
}
