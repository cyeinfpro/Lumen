"use client";

import { AnimatePresence, motion } from "framer-motion";
import { PanelLeftOpen, Plus, X } from "lucide-react";
import {
  type ReactNode,
  type RefObject,
  useEffect,
  useRef,
} from "react";
import { Button, IconButton } from "@/components/ui/primitives";
import { SPRING } from "@/lib/motion";
import { cn } from "@/lib/utils";

export function DesktopPrivateSidebarDrawer({
  open,
  onClose,
  backgroundRef,
  returnFocusRef,
  title,
  children,
}: {
  open: boolean;
  onClose: () => void;
  backgroundRef: RefObject<HTMLElement | null>;
  returnFocusRef: RefObject<HTMLButtonElement | null>;
  title: string;
  children: ReactNode;
}) {
  const panelRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const panel = panelRef.current;
    const background = backgroundRef.current;
    const returnFocusTarget = returnFocusRef.current;
    const previousBodyOverflow = document.body.style.overflow;
    const previousBackgroundInert = background?.inert ?? false;
    const previousBackgroundAriaHidden =
      background?.getAttribute("aria-hidden") ?? null;

    if (background) {
      background.inert = true;
      background.setAttribute("aria-hidden", "true");
    }
    document.body.style.overflow = "hidden";

    const focusFrame = window.requestAnimationFrame(() => panel?.focus());
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== "Tab" || !panel) return;
      const focusable = Array.from(
        panel.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      ).filter(
        (element) =>
          !element.hasAttribute("hidden") && element.getClientRects().length > 0,
      );
      if (focusable.length === 0) {
        event.preventDefault();
        panel.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (
        event.shiftKey &&
        (document.activeElement === first || document.activeElement === panel)
      ) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.cancelAnimationFrame(focusFrame);
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = previousBodyOverflow;
      if (background) {
        background.inert = previousBackgroundInert;
        if (previousBackgroundAriaHidden === null) {
          background.removeAttribute("aria-hidden");
        } else {
          background.setAttribute("aria-hidden", previousBackgroundAriaHidden);
        }
      }
      window.requestAnimationFrame(() => returnFocusTarget?.focus());
    };
  }, [backgroundRef, open, onClose, returnFocusRef]);

  return (
    <AnimatePresence>
      {open ? (
        <>
          <motion.button
            type="button"
            key="private-sidebar-backdrop"
            className="fixed inset-x-0 bottom-0 z-[calc(var(--z-dialog)-1)] bg-[var(--surface-scrim)] min-[1440px]:hidden"
            style={{
              top: "calc(var(--top-banner-stack-height, 0px) + env(safe-area-inset-top, 0px))",
            }}
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.18 }}
            onClick={onClose}
            aria-label={`关闭${title}`}
          />
          <motion.aside
            ref={panelRef}
            key="private-sidebar-panel"
            tabIndex={-1}
            className="fixed bottom-0 left-0 z-[var(--z-dialog)] w-[var(--sidebar-panel-w)] overflow-hidden border-r border-[var(--border-subtle)] bg-[var(--bg-1)] pb-[env(safe-area-inset-bottom,0px)] min-[1440px]:hidden"
            style={{
              top: "calc(var(--top-banner-stack-height, 0px) + env(safe-area-inset-top, 0px))",
            }}
            initial={{ x: "-100%" }}
            animate={{ x: 0 }}
            exit={{ x: "-100%" }}
            transition={SPRING.drawer}
            role="dialog"
            aria-modal="true"
            aria-label={title}
          >
            <IconButton
              size="sm"
              variant="ghost"
              onClick={onClose}
              aria-label={`关闭${title}`}
              className="absolute right-3 top-3 z-[var(--z-header)] rounded-[var(--radius-control)]"
            >
              <X className="h-4 w-4" aria-hidden />
            </IconButton>
            {children}
          </motion.aside>
        </>
      ) : null}
    </AnimatePresence>
  );
}

export function DesktopPrivateSidebarDock({
  expanded,
  onToggle,
  onCreate,
  creating,
  label,
  children,
}: {
  expanded: boolean;
  onToggle: () => void;
  onCreate: () => void;
  creating: boolean;
  label: string;
  children: ReactNode;
}) {
  return (
    <aside
      aria-label={label}
      className={cn(
        "hidden min-[1120px]:flex shrink-0 overflow-hidden border-r border-[var(--border-subtle)] bg-[var(--bg-1)]",
        "transition-[width] duration-[var(--dur-panel)]",
        expanded
          ? "w-[var(--sidebar-rail-w)] min-[1440px]:w-[var(--sidebar-panel-w)]"
          : "w-[var(--sidebar-rail-w)]",
      )}
    >
      {expanded ? (
        <div className="hidden h-full min-w-0 flex-1 min-[1440px]:flex">
          {children}
        </div>
      ) : null}
      <div
        className={cn(
          "flex h-full w-[var(--sidebar-rail-w)] shrink-0 flex-col items-center gap-2 px-2 py-3",
          expanded && "min-[1440px]:hidden",
        )}
      >
        <IconButton
          size="md"
          variant="ghost"
          onClick={onToggle}
          aria-label={`展开${label}`}
          tooltip={`展开${label}`}
        >
          <PanelLeftOpen className="h-[18px] w-[18px]" aria-hidden />
        </IconButton>
        <Button
          size="md"
          variant="primary"
          onClick={onCreate}
          disabled={creating}
          aria-label="新建会话"
          title="新建会话"
          className="h-10 w-10 px-0"
        >
          <Plus className="h-4 w-4" aria-hidden />
        </Button>
      </div>
    </aside>
  );
}
