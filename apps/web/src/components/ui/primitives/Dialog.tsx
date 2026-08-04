"use client";

import { AnimatePresence, motion } from "framer-motion";
import {
  type ComponentPropsWithoutRef,
  type KeyboardEventHandler,
  type MutableRefObject,
  type Ref,
  type RefObject,
  useCallback,
  useRef,
} from "react";

import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import { cn } from "@/lib/utils";

import { useModalLayer } from "./mobile/useModalLayer";

type DialogEase = readonly [number, number, number, number];

const FALLBACK_DIALOG_DURATION_SECONDS = 0.18;
const FALLBACK_DIALOG_EASE: DialogEase = [0.22, 1, 0.36, 1];

let cachedDialogTransition:
  | { duration: number; ease: DialogEase }
  | undefined;

function parseDurationSeconds(value: string): number {
  const duration = Number.parseFloat(value);
  if (!Number.isFinite(duration)) return FALLBACK_DIALOG_DURATION_SECONDS;
  return value.trim().endsWith("ms") ? duration / 1000 : duration;
}

function parseCubicBezier(value: string): DialogEase {
  const match = value.match(/cubic-bezier\(([^)]+)\)/);
  if (!match) return FALLBACK_DIALOG_EASE;
  const points = match[1].split(",").map((point) => Number(point.trim()));
  if (points.length !== 4 || points.some((point) => !Number.isFinite(point))) {
    return FALLBACK_DIALOG_EASE;
  }
  return [points[0], points[1], points[2], points[3]];
}

function getDialogTransition() {
  if (cachedDialogTransition) return cachedDialogTransition;
  if (typeof document === "undefined") {
    return {
      duration: FALLBACK_DIALOG_DURATION_SECONDS,
      ease: FALLBACK_DIALOG_EASE,
    };
  }

  const styles = window.getComputedStyle(document.documentElement);
  cachedDialogTransition = {
    duration: parseDurationSeconds(styles.getPropertyValue("--dur-dialog")),
    ease: parseCubicBezier(styles.getPropertyValue("--ease-develop")),
  };
  return cachedDialogTransition;
}

function assignRef<T>(ref: Ref<T> | undefined, value: T | null) {
  if (typeof ref === "function") {
    ref(value);
    return;
  }
  if (ref) {
    (ref as MutableRefObject<T | null>).current = value;
  }
}

export interface DialogProps {
  open: boolean;
  onClose: () => void;
  children: React.ReactNode;
  className?: string;
  dialogRef?: Ref<HTMLDivElement>;
  initialFocusRef?: RefObject<HTMLElement | null>;
  closeOnBackdrop?: boolean;
  closeOnEscape?: boolean;
  restoreFocus?: boolean;
  "aria-label"?: string;
  "aria-labelledby"?: string;
  "aria-describedby"?: string;
  "aria-busy"?: boolean;
  onKeyDown?: KeyboardEventHandler<HTMLDivElement>;
}

function DialogRoot({
  open,
  onClose,
  children,
  className,
  dialogRef,
  initialFocusRef,
  closeOnBackdrop = true,
  closeOnEscape = true,
  restoreFocus = true,
  "aria-label": ariaLabel,
  "aria-labelledby": ariaLabelledBy,
  "aria-describedby": ariaDescribedBy,
  "aria-busy": ariaBusy,
  onKeyDown,
}: DialogProps) {
  const rootRef = useRef<HTMLDivElement | null>(null);
  const setRootRef = useCallback(
    (node: HTMLDivElement | null) => {
      rootRef.current = node;
      assignRef(dialogRef, node);
    },
    [dialogRef],
  );
  const onModalKeyDown = useModalLayer({
    open,
    rootRef,
    onClose,
    initialFocusRef,
    closeOnEscape,
    restoreFocus,
  });
  const handleKeyDown = useCallback<KeyboardEventHandler<HTMLDivElement>>(
    (event) => {
      onKeyDown?.(event);
      if (!event.defaultPrevented) onModalKeyDown(event);
    },
    [onKeyDown, onModalKeyDown],
  );
  const transition = getDialogTransition();

  useBodyScrollLock(open);

  return (
    <AnimatePresence>
      {open ? (
        <motion.div
          key="dialog-layer"
          data-lumen-modal-layer
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={transition}
          className="mobile-dialog-shell fixed inset-0 z-[var(--z-dialog)] flex items-end justify-center sm:items-center"
          role="presentation"
        >
          <div
            className="absolute inset-0 bg-[var(--surface-scrim)]"
            aria-hidden
            onPointerDown={(event) => {
              if (
                closeOnBackdrop &&
                event.button === 0 &&
                event.target === event.currentTarget
              ) {
                onClose();
              }
            }}
          />
          <motion.div
            ref={setRootRef}
            role="dialog"
            aria-modal="true"
            aria-label={ariaLabel}
            aria-labelledby={ariaLabelledBy}
            aria-describedby={ariaDescribedBy}
            aria-busy={ariaBusy || undefined}
            tabIndex={-1}
            onKeyDown={handleKeyDown}
            initial={{ opacity: 0, scale: 0.96, y: 8 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.96, y: 8 }}
            transition={transition}
            className={cn(
              "mobile-dialog-panel surface-dialog dialog-layout relative w-full overflow-hidden text-[var(--fg-0)] focus-visible:outline-none",
              "max-sm:rounded-[var(--radius-sheet)] sm:rounded-[var(--radius-dialog)]",
              className,
            )}
          >
            {children}
          </motion.div>
        </motion.div>
      ) : null}
    </AnimatePresence>
  );
}

function DialogHeader({
  className,
  ...props
}: ComponentPropsWithoutRef<"header">) {
  return <header className={cn("dialog-header", className)} {...props} />;
}

function DialogBody({
  className,
  ...props
}: ComponentPropsWithoutRef<"div">) {
  return (
    <div
      className={cn("dialog-body mobile-dialog-scroll", className)}
      {...props}
    />
  );
}

function DialogFooter({
  className,
  ...props
}: ComponentPropsWithoutRef<"footer">) {
  return (
    <footer
      className={cn("dialog-footer mobile-dialog-footer", className)}
      {...props}
    />
  );
}

export const Dialog = Object.assign(DialogRoot, {
  Header: DialogHeader,
  Body: DialogBody,
  Footer: DialogFooter,
});
