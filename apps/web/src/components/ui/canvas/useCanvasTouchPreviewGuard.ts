"use client";

import { useCallback, useEffect, useRef, type MouseEvent, type PointerEvent, type RefObject } from "react";
import { CanvasTouchPreviewIntent } from "./CanvasTouchPreviewIntent";
import { useCanvasStoreApi } from "./CanvasStoreProvider";

const INTERACTIVE_ORIGIN = "button,a,input,textarea,select,[role=button],[contenteditable=true],.nodrag,.react-flow__handle";
function point(event: PointerEvent<HTMLDivElement>) {
  return { pointerId: event.pointerId, x: event.clientX, y: event.clientY, now: performance.now() };
}

/** WebKit may retarget a tiny header's touch compatibility click to a media button. */
export function useCanvasTouchPreviewGuard(viewportRef: RefObject<HTMLDivElement | null>) {
  const store = useCanvasStoreApi();
  const intent = useRef(new CanvasTouchPreviewIntent());
  const origin = useRef<{ id: string; x: number; y: number } | null>(null);
  const clear = useCallback(() => { intent.current.clear(); origin.current = null; }, []);
  useEffect(() => {
    const clearExternalPointer = (event: Event) => {
      if (!(event.target instanceof Node) || !viewportRef.current?.contains(event.target)) clear();
    };
    // A finger can finish outside the surface, without a matching React capture
    // event there. Never clear on pointerleave/lostpointercapture: those can
    // normally occur between pointerup and its compatibility click.
    window.addEventListener("pointerdown", clearExternalPointer, true);
    window.addEventListener("pointerup", clearExternalPointer, true);
    window.addEventListener("pointercancel", clearExternalPointer, true);
    window.addEventListener("blur", clear);
    return () => {
      window.removeEventListener("pointerdown", clearExternalPointer, true);
      window.removeEventListener("pointerup", clearExternalPointer, true);
      window.removeEventListener("pointercancel", clearExternalPointer, true);
      window.removeEventListener("blur", clear);
      clear();
    };
  }, [clear, viewportRef]);

  const onPointerDownCapture = (event: PointerEvent<HTMLDivElement>) => {
    const state = store.getState();
    const target = event.target instanceof Element ? event.target : null;
    const nodeElement = target?.closest(".react-flow__node[data-id]");
    const id = nodeElement?.getAttribute("data-id");
    const node = id ? state.graph.nodes.find((item) => item.id === id) : null;
    const eligible = state.toolMode === "select" && !state.connectionDraft
      && target?.closest(".canvas-node-drag-handle") && !target.closest(INTERACTIVE_ORIGIN)
      && nodeElement && event.currentTarget.contains(nodeElement) && node;
    origin.current = eligible ? { id: node.id, ...node.position } : null;
    intent.current.down({ ...point(event), pointerType: event.pointerType,
      isPrimary: event.isPrimary, nodeId: eligible ? node.id : null });
  };
  const onClickCapture = (event: MouseEvent<HTMLDivElement>) => {
    const target = event.target instanceof Element ? event.target : null;
    const nodeId = intent.current.consume({ x: event.clientX, y: event.clientY,
      now: performance.now(), detail: event.detail,
      preview: Boolean(target?.closest("[data-canvas-output-preview]")) });
    const original = origin.current;
    origin.current = null;
    if (!nodeId || !original || original.id !== nodeId) return;
    const state = store.getState();
    const node = state.graph.nodes.find((item) => item.id === nodeId);
    if (state.toolMode !== "select" || state.connectionDraft || state.activeInteractionCount > 0
      || !node || node.position.x !== original.x || node.position.y !== original.y) return;
    event.preventDefault();
    event.stopPropagation();
    state.selectNodes(event.shiftKey
      ? state.selectedNodeIds.includes(nodeId)
        ? state.selectedNodeIds.filter((id) => id !== nodeId)
        : [...state.selectedNodeIds, nodeId]
      : [nodeId]);
  };
  return {
    onPointerDownCapture,
    onPointerMoveCapture: (event: PointerEvent<HTMLDivElement>) => intent.current.move(point(event)),
    onPointerUpCapture: (event: PointerEvent<HTMLDivElement>) => intent.current.up(point(event)),
    onClickCapture,
    clear,
  };
}
