"use client";

import { Button } from "@/components/ui/primitives";
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
    <Button
      size="sm"
      variant="outline"
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
        "leading-none transition-colors",
        compact
          ? "h-5 px-0.5 type-overline max-sm:min-h-5 max-sm:min-w-0 max-sm:px-0.5"
          : "h-6 px-1 type-caption max-sm:min-h-6 max-sm:min-w-0 max-sm:px-1",
        role === "ask_target"
          ? "border-info-border bg-info-soft text-info"
          : role === "edit_target"
            ? "border-accent-border bg-accent-soft text-accent"
            : "border-[var(--border)] bg-[var(--bg-0)]/82 text-[var(--fg-0)]",
      )}
    >
      {compact ? label : `${label} #${imageNumber}`}
    </Button>
  );
}
