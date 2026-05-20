"use client";

import { cn } from "@/lib/utils";
import {
  attachmentRoleHint,
  attachmentRoleLabel,
  type ComposerAttachmentRole,
} from "./attachmentRoles";

export function AttachmentRoleBadge({
  role,
  imageNumber,
  onClick,
  compact = false,
}: {
  role: ComposerAttachmentRole;
  imageNumber: number;
  onClick: () => void;
  compact?: boolean;
}) {
  const label = attachmentRoleLabel(role);
  const title = `用途：${label}。${attachmentRoleHint(role)}。点击切换`;
  return (
    <button
      type="button"
      data-composer-attachment-action="true"
      onPointerDown={(event) => event.stopPropagation()}
      onClick={(event) => {
        event.stopPropagation();
        onClick();
      }}
      aria-label={`图 ${imageNumber} 用途：${label}，点击切换`}
      title={title}
      className={cn(
        "absolute inset-x-0.5 bottom-0.5 inline-flex items-center justify-center",
        "rounded-[var(--radius-control)] border backdrop-blur-sm",
        "font-semibold leading-none transition-colors",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/70",
        compact ? "h-4 px-0.5 text-[8px]" : "h-5 px-1 text-[10px]",
        role === "ask_target"
          ? "border-[var(--info)]/35 bg-[var(--info-soft)] text-[var(--info-fg)]"
          : role === "edit_target"
            ? "border-[var(--amber-400)]/50 bg-[var(--amber-400)]/20 text-[var(--amber-300)]"
            : "border-[var(--border)] bg-[var(--bg-0)]/82 text-[var(--fg-0)]",
      )}
    >
      {compact ? label : `${label} #${imageNumber}`}
    </button>
  );
}
