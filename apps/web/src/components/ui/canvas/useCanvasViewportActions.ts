import type {
  Edge,
  OnConnectEnd,
  OnSelectionChangeParams,
  ReactFlowInstance,
} from "@xyflow/react";
import {
  useCallback,
  useEffect,
  useRef,
  type Dispatch,
  type MouseEvent as ReactMouseEvent,
  type MutableRefObject,
  type RefObject,
  type SetStateAction,
} from "react";

import {
  fitCanvasViewport,
  flowViewportBounds,
  focusCanvasNode,
  pointerClientPosition,
  viewportAnimationDuration,
  type CanvasViewportPreferences,
} from "./CanvasViewportModel";
import type { CanvasFlowNode } from "./nodes/CanvasNodes";
import type {
  CanvasViewportActionRequest,
  CanvasViewportApi,
} from "./CanvasViewportTypes";
import type { ConnectionDraft } from "@/lib/canvas/types";

interface UseCanvasViewportActionsOptions {
  cancelledConnectionRef: MutableRefObject<boolean>;
  connectionDraft: ConnectionDraft | null;
  connectionDraftRef: MutableRefObject<ConnectionDraft | null>;
  finishInteraction: (nodes?: CanvasFlowNode[]) => void;
  instanceRef: MutableRefObject<
    ReactFlowInstance<CanvasFlowNode, Edge> | null
  >;
  isMobile: boolean;
  onOpenContextMenu?: (request: CanvasViewportActionRequest) => void;
  onOpenQuickAdd?: (request: CanvasViewportActionRequest) => void;
  onReady?: (api: CanvasViewportApi) => void;
  selectEdge: (edgeId: string | null) => void;
  selectNode: (nodeId: string | null) => void;
  selectNodes: (nodeIds: string[]) => void;
  selectedNodeIds: string[];
  selectedNodeIdSet: Set<string>;
  setInstance: Dispatch<
    SetStateAction<ReactFlowInstance<CanvasFlowNode, Edge> | null>
  >;
  setMiniMapVisible: Dispatch<SetStateAction<boolean>>;
  snapGrid: [number, number];
  snapToGrid: boolean;
  toolMode: string;
  updateConnectionDraft: (draft: ConnectionDraft | null) => void;
  viewportPreferencesRef: MutableRefObject<CanvasViewportPreferences>;
  viewportRef: RefObject<HTMLDivElement | null>;
  addPromptNode: (position: { x: number; y: number }) => void;
}

export function useCanvasViewportActions({
  addPromptNode,
  cancelledConnectionRef,
  connectionDraft,
  connectionDraftRef,
  finishInteraction,
  instanceRef,
  isMobile,
  onOpenContextMenu,
  onOpenQuickAdd,
  onReady,
  selectEdge,
  selectNode,
  selectNodes,
  selectedNodeIds,
  selectedNodeIdSet,
  setInstance,
  setMiniMapVisible,
  snapGrid,
  snapToGrid,
  toolMode,
  updateConnectionDraft,
  viewportPreferencesRef,
  viewportRef,
}: UseCanvasViewportActionsOptions) {
  const connectionDropPositionRef = useRef<{ x: number; y: number } | null>(
    null,
  );
  const suppressPaneClickRef = useRef(false);
  const suppressPaneClickTimerRef = useRef<number | null>(null);

  const createActionRequest = useCallback(
    (
      clientPosition: { x: number; y: number },
      trigger: CanvasViewportActionRequest["trigger"],
      draft: ConnectionDraft | null = connectionDraftRef.current,
      target: Pick<CanvasViewportActionRequest, "nodeId" | "edgeId"> = {},
    ): CanvasViewportActionRequest | null => {
      const current = instanceRef.current;
      if (!current) return null;
      return {
        position: current.screenToFlowPosition(clientPosition, {
          snapToGrid,
          snapGrid,
        }),
        clientPosition,
        trigger,
        connectionDraft: draft,
        ...target,
      };
    },
    [connectionDraftRef, instanceRef, snapGrid, snapToGrid],
  );

  const openQuickAdd = useCallback(
    (request: CanvasViewportActionRequest, fallbackToPrompt = false) => {
      if (onOpenQuickAdd) {
        onOpenQuickAdd(request);
        return;
      }
      if (fallbackToPrompt) addPromptNode(request.position);
    },
    [addPromptNode, onOpenQuickAdd],
  );

  const handlePaneClick = useCallback(
    (event: ReactMouseEvent | MouseEvent) => {
      selectNode(null);
      selectEdge(null);
      const suppressDraftReset = suppressPaneClickRef.current;
      suppressPaneClickRef.current = false;
      if (!suppressDraftReset) updateConnectionDraft(null);
      if (event.detail !== 2) return;
      const request = createActionRequest(
        { x: event.clientX, y: event.clientY },
        "pane-double-click",
      );
      if (request) openQuickAdd(request, true);
    },
    [
      createActionRequest,
      openQuickAdd,
      selectEdge,
      selectNode,
      updateConnectionDraft,
    ],
  );

  const handleSelectionChange = useCallback(
    ({ nodes, edges }: OnSelectionChangeParams<CanvasFlowNode, Edge>) => {
      if (edges.length > 0) {
        selectEdge(edges.at(-1)?.id ?? null);
        return;
      }
      selectNodes(nodes.map((node) => node.id));
    },
    [selectEdge, selectNodes],
  );

  const handleNodeClick = useCallback(
    (event: ReactMouseEvent, node: CanvasFlowNode) => {
      if (event.shiftKey) {
        selectNodes(
          selectedNodeIdSet.has(node.id)
            ? selectedNodeIds.filter((nodeId) => nodeId !== node.id)
            : [...selectedNodeIds, node.id],
        );
        return;
      }
      selectNodes([node.id]);
    },
    [selectNodes, selectedNodeIdSet, selectedNodeIds],
  );

  const handlePaneContextMenu = useCallback(
    (event: ReactMouseEvent | MouseEvent) => {
      if (!onOpenContextMenu) return;
      event.preventDefault();
      const request = createActionRequest(
        { x: event.clientX, y: event.clientY },
        "pane-context-menu",
      );
      if (request) onOpenContextMenu(request);
    },
    [createActionRequest, onOpenContextMenu],
  );

  const handleNodeContextMenu = useCallback(
    (event: ReactMouseEvent, node: CanvasFlowNode) => {
      if (!onOpenContextMenu) return;
      event.preventDefault();
      selectNodes([node.id]);
      const request = createActionRequest(
        { x: event.clientX, y: event.clientY },
        "node-context-menu",
        connectionDraftRef.current,
        { nodeId: node.id },
      );
      if (request) onOpenContextMenu(request);
    },
    [
      connectionDraftRef,
      createActionRequest,
      onOpenContextMenu,
      selectNodes,
    ],
  );

  const handleEdgeContextMenu = useCallback(
    (event: ReactMouseEvent, edge: Edge) => {
      if (!onOpenContextMenu) return;
      event.preventDefault();
      selectEdge(edge.id);
      const request = createActionRequest(
        { x: event.clientX, y: event.clientY },
        "edge-context-menu",
        connectionDraftRef.current,
        { edgeId: edge.id },
      );
      if (request) onOpenContextMenu(request);
    },
    [connectionDraftRef, createActionRequest, onOpenContextMenu, selectEdge],
  );

  const handleConnectEnd = useCallback<OnConnectEnd>(
    (event, connectionState) => {
      try {
        if (cancelledConnectionRef.current) {
          cancelledConnectionRef.current = false;
          connectionDropPositionRef.current = null;
          updateConnectionDraft(null);
          return;
        }
        if (connectionState.isValid) {
          connectionDropPositionRef.current = null;
          return;
        }
        const draft = connectionDraftRef.current;
        const clientPosition = pointerClientPosition(event);
        if (draft && connectionState.toNode === null && clientPosition) {
          const request = createActionRequest(
            clientPosition,
            "connection-drop",
            draft,
          );
          if (request) {
            connectionDropPositionRef.current = request.position;
            suppressPaneClickRef.current = true;
            if (suppressPaneClickTimerRef.current !== null) {
              window.clearTimeout(suppressPaneClickTimerRef.current);
            }
            suppressPaneClickTimerRef.current = window.setTimeout(() => {
              suppressPaneClickRef.current = false;
              suppressPaneClickTimerRef.current = null;
            }, 0);
            onOpenQuickAdd?.(request);
            return;
          }
        }
        if (!isMobile || toolMode !== "connect") {
          window.setTimeout(() => updateConnectionDraft(null), 0);
        }
      } finally {
        finishInteraction();
      }
    },
    [
      cancelledConnectionRef,
      connectionDraftRef,
      createActionRequest,
      finishInteraction,
      isMobile,
      onOpenQuickAdd,
      toolMode,
      updateConnectionDraft,
    ],
  );

  const handleEmptyQuickAdd = useCallback(() => {
    const bounds = flowViewportBounds(viewportRef.current);
    if (!bounds) return;
    const request = createActionRequest(
      {
        x: bounds.left + bounds.width / 2,
        y: bounds.top + bounds.height / 2,
      },
      "empty-state",
      null,
    );
    if (request) openQuickAdd(request, true);
  }, [createActionRequest, openQuickAdd, viewportRef]);

  const handleInit = useCallback(
    (next: ReactFlowInstance<CanvasFlowNode, Edge>) => {
      instanceRef.current = next;
      setInstance(next);
      onReady?.(
        createViewportApi({
          instance: next,
          setMiniMapVisible,
          viewportPreferencesRef,
          viewportRef,
        }),
      );
    },
    [
      instanceRef,
      onReady,
      setInstance,
      setMiniMapVisible,
      viewportPreferencesRef,
      viewportRef,
    ],
  );

  useEffect(() => {
    if (!connectionDraft) connectionDropPositionRef.current = null;
  }, [connectionDraft]);

  useEffect(
    () => () => {
      if (suppressPaneClickTimerRef.current !== null) {
        window.clearTimeout(suppressPaneClickTimerRef.current);
      }
    },
    [],
  );

  return {
    handleConnectEnd,
    handleEdgeContextMenu,
    handleEmptyQuickAdd,
    handleInit,
    handleNodeClick,
    handleNodeContextMenu,
    handlePaneClick,
    handlePaneContextMenu,
    handleSelectionChange,
  };
}

interface CreateViewportApiOptions {
  instance: ReactFlowInstance<CanvasFlowNode, Edge>;
  setMiniMapVisible: Dispatch<SetStateAction<boolean>>;
  viewportPreferencesRef: MutableRefObject<CanvasViewportPreferences>;
  viewportRef: RefObject<HTMLDivElement | null>;
}

function createViewportApi({
  instance,
  setMiniMapVisible,
  viewportPreferencesRef,
  viewportRef,
}: CreateViewportApiOptions): CanvasViewportApi {
  return {
    fitView: (options) =>
      fitCanvasViewport(
        instance,
        viewportPreferencesRef.current,
        undefined,
        undefined,
        undefined,
        options?.instant ? 0 : undefined,
      ),
    fitSelection: (nodeIds, options) => {
      const ids = nodeIds ?? viewportPreferencesRef.current.selectedNodeIds;
      const nodes = ids
        .map((nodeId) => instance.getNode(nodeId))
        .filter((node): node is CanvasFlowNode => Boolean(node));
      if (nodes.length === 0) return;
      fitCanvasViewport(
        instance,
        viewportPreferencesRef.current,
        nodes,
        0.26,
        1.2,
        options?.instant ? 0 : undefined,
      );
    },
    focusNode: (nodeId) =>
      focusCanvasNode(instance, nodeId, viewportPreferencesRef.current),
    zoomIn: (options) => {
      void instance.zoomIn({
        duration: viewportApiAnimationDuration(options, viewportPreferencesRef),
      });
    },
    zoomOut: (options) => {
      void instance.zoomOut({
        duration: viewportApiAnimationDuration(options, viewportPreferencesRef),
      });
    },
    resetZoom: (options) => {
      void instance.zoomTo(1, {
        duration: viewportApiAnimationDuration(options, viewportPreferencesRef),
      });
    },
    toggleMiniMap: () => setMiniMapVisible((current) => !current),
    getZoom: () => instance.getZoom(),
    getViewportCenter: () => {
      const bounds = flowViewportBounds(viewportRef.current);
      if (!bounds) return { x: 0, y: 0 };
      return instance.screenToFlowPosition({
        x: bounds.left + bounds.width / 2,
        y: bounds.top + bounds.height / 2,
      });
    },
  };
}

function viewportApiAnimationDuration(
  options: { instant?: boolean } | undefined,
  viewportPreferencesRef: MutableRefObject<CanvasViewportPreferences>,
) {
  return options?.instant
    ? 0
    : viewportAnimationDuration(
        viewportPreferencesRef.current.reducedMotion,
      );
}
