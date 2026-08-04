"use client";

import { MoreHorizontal } from "lucide-react";
import { type ReactNode, useEffect, useId, useRef, useState } from "react";

import { MediaControlButton } from "@/components/ui/primitives/MediaControlButton";
import { cn } from "@/lib/utils";

export interface LightboxMenuAction {
  label: string;
  icon: ReactNode;
  onSelect: () => void;
  disabled?: boolean;
}

interface LightboxActionMenuProps {
  actions: LightboxMenuAction[];
  side?: "top" | "bottom";
  tabIndex?: number;
  className?: string;
}

export function LightboxActionMenu({
  actions,
  side = "bottom",
  tabIndex,
  className,
}: LightboxActionMenuProps) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const menuId = useId();

  useEffect(() => {
    if (!open) return;

    const handlePointerDown = (event: PointerEvent) => {
      if (
        event.target instanceof Node &&
        !rootRef.current?.contains(event.target)
      ) {
        setOpen(false);
      }
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopPropagation();
      setOpen(false);
      triggerRef.current?.focus({ preventScroll: true });
    };

    document.addEventListener("pointerdown", handlePointerDown, true);
    document.addEventListener("keydown", handleKeyDown, true);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown, true);
      document.removeEventListener("keydown", handleKeyDown, true);
    };
  }, [open]);

  return (
    <div ref={rootRef} className={cn("relative", className)}>
      <MediaControlButton
        ref={triggerRef}
        size="lg"
        aria-label="更多操作"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        tabIndex={tabIndex}
        onClick={() => setOpen((value) => !value)}
        className="shadow-[var(--shadow-2)]"
      >
        <MoreHorizontal className="h-5 w-5" aria-hidden />
      </MediaControlButton>
      {open ? (
        <div
          id={menuId}
          role="menu"
          aria-label="更多操作"
          className={cn(
            "surface-panel absolute right-0 z-[var(--z-tray)] min-w-44 p-1",
            side === "top"
              ? "bottom-[calc(100%+var(--space-2))]"
              : "top-[calc(100%+var(--space-2))]",
          )}
        >
          {actions.map((action) => (
            <button
              key={action.label}
              type="button"
              role="menuitem"
              disabled={action.disabled}
              onClick={() => {
                setOpen(false);
                action.onSelect();
              }}
              className={cn(
                "type-body-sm flex min-h-10 w-full items-center gap-2 rounded-[var(--radius-control)] px-3 text-left",
                "text-[var(--fg-1)] transition-colors hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)]",
                "focus-visible:outline-none focus-visible:shadow-[var(--ring)]",
                "disabled:cursor-not-allowed disabled:opacity-40",
              )}
            >
              <span
                className="inline-flex h-4 w-4 shrink-0 items-center justify-center"
                aria-hidden
              >
                {action.icon}
              </span>
              <span className="min-w-0 flex-1 truncate">{action.label}</span>
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}
