import type { Edge, ReactFlowInstance } from "@xyflow/react";
import {
  useCallback,
  useEffect,
  useState,
  type Dispatch,
  type MutableRefObject,
  type SetStateAction,
} from "react";

import { updateCanvasTransientPositions } from "@/lib/canvas/interaction";
import type { CanvasPosition, ConnectionDraft } from "@/lib/canvas/types";
import {
  omitCanvasNodeMeasurements,
  type CanvasNodeGeometry,
} from "./CanvasViewportModel";
import type { CanvasFlowNode } from "./nodes/CanvasNodes";

interface UseCanvasViewportDomainInteractionOptions {
  interactionActiveRef: MutableRefObject<boolean>;
  cancelledConnectionRef: MutableRefObject<boolean>;
  cancelledResizeRef: MutableRefObject<boolean>;
  connectionDraft: ConnectionDraft | null;
  editorFocusRequestRef: MutableRefObject<number>;
  instanceRef: MutableRefObject<
    ReactFlowInstance<CanvasFlowNode, Edge> | null
  >;
  resizingNodeIdsRef: MutableRefObject<Set<string>>;
  beginInteraction: () => void;
  endInteraction: () => void;
  moveNodes: (
    positions: Array<{ nodeId: string; position: CanvasPosition }>,
  ) => void;
  setTargetPickerOpen: Dispatch<SetStateAction<boolean>>;
  toolMode: string;
  updateConnectionDraft: (draft: ConnectionDraft | null) => void;
}

export function useCanvasViewportDomainInteraction({
  interactionActiveRef,
  cancelledConnectionRef,
  cancelledResizeRef,
  connectionDraft,
  editorFocusRequestRef,
  instanceRef,
  resizingNodeIdsRef,
  beginInteraction,
  endInteraction,
  moveNodes,
  setTargetPickerOpen,
  toolMode,
  updateConnectionDraft,
}: UseCanvasViewportDomainInteractionOptions) {
  const [transientPositions, setTransientPositions] = useState<
    Record<string, CanvasPosition>
  >({});
  const [measuredDimensions, setMeasuredDimensions] = useState<
    Record<string, CanvasNodeGeometry["size"]>
  >({});

  const clearTransientNodeState = useCallback(
    (nodeIds?: readonly string[]) => {
      if (!nodeIds) {
        setTransientPositions({});
        setMeasuredDimensions({});
        return;
      }
      setTransientPositions((current) =>
        updateCanvasTransientPositions(current, [], nodeIds),
      );
      setMeasuredDimensions((current) =>
        omitCanvasNodeMeasurements(current, nodeIds),
      );
    },
    [],
  );

  const startInteraction = useCallback(() => {
    if (interactionActiveRef.current) return;
    interactionActiveRef.current = true;
    beginInteraction();
  }, [beginInteraction, interactionActiveRef]);

  const finishInteraction = useCallback(
    (nodes: CanvasFlowNode[] = []) => {
      if (nodes.length > 0) {
        const positions = nodes.map((node) => ({
          nodeId: node.id,
          position: node.position,
        }));
        clearTransientNodeState(positions.map((item) => item.nodeId));
        moveNodes(positions);
      }
      if (!interactionActiveRef.current) return;
      interactionActiveRef.current = false;
      endInteraction();
    },
    [
      clearTransientNodeState,
      endInteraction,
      interactionActiveRef,
      moveNodes,
    ],
  );

  const markInteractionCancelled = useCallback(() => {
    cancelledConnectionRef.current = true;
    if (resizingNodeIdsRef.current.size > 0) {
      cancelledResizeRef.current = true;
    }
  }, [cancelledConnectionRef, cancelledResizeRef, resizingNodeIdsRef]);

  const cancelDomainInteraction = useCallback(() => {
    if (resizingNodeIdsRef.current.size > 0) {
      cancelledResizeRef.current = true;
    }
    updateConnectionDraft(null);
    setTargetPickerOpen(false);
    resizingNodeIdsRef.current.clear();
    clearTransientNodeState();
    finishInteraction();
  }, [
    cancelledResizeRef,
    clearTransientNodeState,
    finishInteraction,
    resizingNodeIdsRef,
    setTargetPickerOpen,
    updateConnectionDraft,
  ]);

  useEffect(
    () => () => {
      editorFocusRequestRef.current += 1;
      instanceRef.current = null;
      resizingNodeIdsRef.current.clear();
      if (interactionActiveRef.current) endInteraction();
    },
    [
      editorFocusRequestRef,
      endInteraction,
      instanceRef,
      interactionActiveRef,
      resizingNodeIdsRef,
    ],
  );

  useEffect(() => {
    if (toolMode !== "select" || connectionDraft) {
      editorFocusRequestRef.current += 1;
    }
  }, [connectionDraft, editorFocusRequestRef, toolMode]);

  return {
    cancelDomainInteraction,
    clearTransientNodeState,
    finishInteraction,
    markInteractionCancelled,
    measuredDimensions,
    setMeasuredDimensions,
    setTransientPositions,
    startInteraction,
    transientPositions,
  };
}
