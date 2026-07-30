"use client";

import {
  useEffect,
  useId,
  useRef,
} from "react";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";

interface UseMobileLightboxDialogOptions {
  open: boolean;
  paramsOpen: boolean;
  onClose: () => void;
  onGoto: (delta: 1 | -1) => void;
  onCloseParams: () => void;
}

export function useMobileLightboxDialog({
  open,
  paramsOpen,
  onClose,
  onGoto,
  onCloseParams,
}: UseMobileLightboxDialogOptions) {
  const dialogRootRef = useRef<HTMLDivElement | null>(null);
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  const previouslyFocusedRef = useRef<HTMLElement | null>(null);
  const dialogTitleId = useId();

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        if (paramsOpen) {
          onCloseParams();
          return;
        }
        onClose();
        return;
      }
      if (event.key === "ArrowLeft") {
        onGoto(-1);
        return;
      }
      if (event.key === "ArrowRight") {
        onGoto(1);
        return;
      }
      if (event.key !== "Tab") return;

      const root = dialogRootRef.current;
      if (!root) return;
      const focusables = Array.from(
        root.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      ).filter((element) => !element.hasAttribute("data-focus-skip"));
      if (focusables.length === 0) {
        event.preventDefault();
        return;
      }
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      const active = document.activeElement as HTMLElement | null;
      if (event.shiftKey) {
        if (active === first || !root.contains(active)) {
          event.preventDefault();
          last.focus();
        }
      } else if (active === last) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, onCloseParams, onGoto, open, paramsOpen]);

  useEffect(() => {
    if (!open) return;
    previouslyFocusedRef.current =
      document.activeElement as HTMLElement | null;
    const frame = requestAnimationFrame(() => {
      const target =
        closeButtonRef.current ?? dialogRootRef.current;
      target?.focus({ preventScroll: true });
    });
    return () => {
      cancelAnimationFrame(frame);
      const previous = previouslyFocusedRef.current;
      if (previous && typeof previous.focus === "function") {
        try {
          previous.focus({ preventScroll: true });
        } catch {
          // The previous element may have unmounted.
        }
      }
      previouslyFocusedRef.current = null;
    };
  }, [open]);

  useBodyScrollLock(open, {
    documentOverscrollBehavior: "none",
  });

  return {
    dialogRootRef,
    closeButtonRef,
    dialogTitleId,
  };
}
