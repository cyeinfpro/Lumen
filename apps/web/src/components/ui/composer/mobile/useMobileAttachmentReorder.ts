"use client";

import {
  type MouseEvent as ReactMouseEvent,
  type PointerEvent as ReactPointerEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

import type { HapticKind } from "@/hooks/useHaptic";

const ATTACHMENT_REORDER_LONG_PRESS_MS = 220;
const ATTACHMENT_REORDER_MOVE_SLOP_PX = 10;

interface AttachmentReorderState {
  pointerId: number;
  sourceId: string;
  startX: number;
  startY: number;
  active: boolean;
  lastTargetId: string | null;
  timer: ReturnType<typeof setTimeout> | null;
}

interface AttachmentReorderListeners {
  move: ((event: PointerEvent) => void) | null;
  end: ((event: PointerEvent) => void) | null;
}

export function useMobileAttachmentReorder({
  attachmentCount,
  moveAttachment,
  haptic,
}: {
  attachmentCount: number;
  moveAttachment: (id: string, targetId: string) => void;
  haptic: (kind: HapticKind) => void;
}) {
  const [draggingAttachmentId, setDraggingAttachmentId] = useState<
    string | null
  >(null);
  const [reorderTargetAttachmentId, setReorderTargetAttachmentId] = useState<
    string | null
  >(null);
  const reorderStateRef = useRef<AttachmentReorderState | null>(null);
  const listenersRef = useRef<AttachmentReorderListeners | null>(null);
  const suppressNextClickRef = useRef(false);

  const reset = useCallback(
    (commit: boolean) => {
      const state = reorderStateRef.current;
      if (!state) return;
      if (state.timer) {
        clearTimeout(state.timer);
        state.timer = null;
      }
      const listeners = listenersRef.current;
      if (listeners?.move) {
        window.removeEventListener("pointermove", listeners.move);
      }
      if (listeners?.end) {
        window.removeEventListener("pointerup", listeners.end);
        window.removeEventListener("pointercancel", listeners.end);
      }
      listenersRef.current = null;
      reorderStateRef.current = null;
      if (
        commit &&
        state.active &&
        state.lastTargetId &&
        state.lastTargetId !== state.sourceId
      ) {
        moveAttachment(state.sourceId, state.lastTargetId);
      }
      if (state.active) {
        suppressNextClickRef.current = true;
      }
      setDraggingAttachmentId(null);
      setReorderTargetAttachmentId(null);
    },
    [moveAttachment],
  );

  const begin = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>, id: string) => {
      if (attachmentCount <= 1) return;
      if (!event.isPrimary || event.button !== 0) return;
      const target = event.target as HTMLElement | null;
      if (target?.closest("[data-composer-attachment-action='true']")) return;
      if (reorderStateRef.current) return;

      const state: AttachmentReorderState = {
        pointerId: event.pointerId,
        sourceId: id,
        startX: event.clientX,
        startY: event.clientY,
        active: false,
        lastTargetId: null,
        timer: null,
      };
      reorderStateRef.current = state;

      const moveListener = (nativeEvent: PointerEvent) => {
        const current = reorderStateRef.current;
        if (!current || nativeEvent.pointerId !== current.pointerId) return;

        const dx = nativeEvent.clientX - current.startX;
        const dy = nativeEvent.clientY - current.startY;
        if (!current.active) {
          if (Math.hypot(dx, dy) > ATTACHMENT_REORDER_MOVE_SLOP_PX) {
            reset(false);
          }
          return;
        }

        nativeEvent.preventDefault();
        const element = document.elementFromPoint(
          nativeEvent.clientX,
          nativeEvent.clientY,
        );
        const tile =
          element instanceof Element
            ? (element.closest(
                "[data-composer-attachment-id]",
              ) as HTMLElement | null)
            : null;
        const targetId = tile?.dataset.composerAttachmentId ?? null;
        const nextTargetId =
          targetId && targetId !== current.sourceId ? targetId : null;
        current.lastTargetId = nextTargetId;
        setReorderTargetAttachmentId(nextTargetId);
      };

      const endListener = (nativeEvent: PointerEvent) => {
        const current = reorderStateRef.current;
        if (!current || nativeEvent.pointerId !== current.pointerId) return;
        if (current.active) nativeEvent.preventDefault();
        reset(current.active);
      };

      listenersRef.current = {
        move: moveListener,
        end: endListener,
      };
      window.addEventListener("pointermove", moveListener, { passive: false });
      window.addEventListener("pointerup", endListener);
      window.addEventListener("pointercancel", endListener);

      state.timer = setTimeout(() => {
        const current = reorderStateRef.current;
        if (
          !current ||
          current.pointerId !== state.pointerId ||
          current.sourceId !== id
        ) {
          return;
        }
        current.active = true;
        current.timer = null;
        setDraggingAttachmentId(current.sourceId);
        setReorderTargetAttachmentId(null);
        haptic("light");
      }, ATTACHMENT_REORDER_LONG_PRESS_MS);
    },
    [attachmentCount, haptic, reset],
  );

  const handleClickCapture = useCallback(
    (event: ReactMouseEvent<HTMLDivElement>) => {
      if (!suppressNextClickRef.current) return;
      suppressNextClickRef.current = false;
      event.preventDefault();
      event.stopPropagation();
    },
    [],
  );

  useEffect(() => {
    return () => {
      const state = reorderStateRef.current;
      if (state?.timer) clearTimeout(state.timer);
      const listeners = listenersRef.current;
      if (listeners?.move) {
        window.removeEventListener("pointermove", listeners.move);
      }
      if (listeners?.end) {
        window.removeEventListener("pointerup", listeners.end);
        window.removeEventListener("pointercancel", listeners.end);
      }
      listenersRef.current = null;
      reorderStateRef.current = null;
    };
  }, []);

  return {
    beginAttachmentReorder: begin,
    handleAttachmentClickCapture: handleClickCapture,
    draggingAttachmentId,
    reorderTargetAttachmentId,
  };
}
