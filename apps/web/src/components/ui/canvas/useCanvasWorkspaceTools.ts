"use client";

import { useCallback, useMemo, useRef, useState } from "react";
import { useStore } from "zustand";

import {
  copySubgraph,
  parseCanvasSubgraph,
  serializeCanvasSubgraph,
  type CanvasSubgraph,
} from "@/lib/canvas/clipboard";
import { centeredCanvasNodePosition } from "@/lib/canvas/interaction";
import {
  alignNodes,
  autoLayoutDag,
  distributeNodes,
  type CanvasAlignment,
  type CanvasDistributionAxis,
} from "@/lib/canvas/layout";
import {
  CANVAS_NODE_SPECS,
  findCanvasNodeCatalogItem,
  isCanvasNodeType,
} from "@/lib/canvas/registry";
import type { CanvasEditorStore } from "@/lib/canvas/store";
import type {
  CanvasGraph,
  CanvasNodeType,
  CanvasPosition,
} from "@/lib/canvas/types";
import { toast } from "@/components/ui/primitives";
import type {
  CanvasCommandMenuItem,
} from "./CanvasCommandMenu";
import type {
  CanvasViewportActionRequest,
  CanvasViewportApi,
} from "./CanvasViewport";

import {
  buildCommandItems,
  selectedNodes,
  connectDraftToNewNode,
  deleteContextTarget,
  commandSuffix,
  findOpenNodePosition,
} from "./canvasWorkspaceToolDomain";

interface UseCanvasWorkspaceToolsOptions {
  graph: CanvasGraph;
  selectedNodeIds: readonly string[];
  selectedEdgeId: string | null;
  store: CanvasEditorStore;
  viewportApi: CanvasViewportApi | null;
  onRunSelected: () => void;
}

export function useCanvasWorkspaceTools({
  graph,
  selectedNodeIds,
  selectedEdgeId,
  store,
  viewportApi,
  onRunSelected,
}: UseCanvasWorkspaceToolsOptions) {
  const [commandMenuOpen, setCommandMenuOpenState] = useState(false);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const [actionRequest, setActionRequest] =
    useState<CanvasViewportActionRequest | null>(null);
  const clipboardRef = useRef<CanvasSubgraph | null>(null);
  const selectedNodeId = useStore(store, (state) => state.selectedNodeId);

  const selectedCount = selectedNodeIds.length;

  const openCommandMenu = useCallback(
    (request: CanvasViewportActionRequest | null = null) => {
      setActionRequest(request);
      setCommandMenuOpenState(true);
    },
    [],
  );
  const setCommandMenuOpen = useCallback(
    (open: boolean) => {
      setCommandMenuOpenState(open);
      if (open) return;
      setActionRequest(null);
      if (actionRequest?.connectionDraft) {
        store.getState().setConnectionDraft(null);
      }
    },
    [actionRequest, store],
  );
  const openShortcuts = useCallback(() => setShortcutsOpen(true), []);

  const addNode = useCallback(
    (nodeReference: CanvasNodeType | string, position?: CanvasPosition) => {
      const state = store.getState();
      const catalogItem = findCanvasNodeCatalogItem(nodeReference);
      const type = catalogItem?.type ?? (isCanvasNodeType(nodeReference) ? nodeReference : null);
      if (!type) {
        toast.error("未找到要添加的节点");
        return "";
      }
      const spec = CANVAS_NODE_SPECS[type];
      const center =
        position ?? viewportApi?.getViewportCenter() ?? { x: 360, y: 260 };
      const resolvedPosition = position
        ? position
        : findOpenNodePosition(
            centeredCanvasNodePosition({
              center,
              width: spec.width,
              height: type === "frame" ? 220 : 180,
            }),
            spec.width,
            type === "frame" ? 220 : 180,
            state.graph.nodes,
          );
      const nodeId = state.addNode(
        type,
        resolvedPosition,
        catalogItem
          ? {
              ...catalogItem.overrides,
              ui: {
                ...(catalogItem.overrides?.ui ?? {}),
                preset_id: catalogItem.id,
              },
            }
          : undefined,
      );
      if (!nodeId) {
        toast.error("画布已达到节点或存储大小上限");
        return "";
      }
      connectDraftToNewNode(store, nodeId, actionRequest);
      window.requestAnimationFrame(() => viewportApi?.focusNode(nodeId));
      setActionRequest(null);
      return nodeId;
    },
    [actionRequest, store, viewportApi],
  );

  const copySelection = useCallback(async () => {
    const state = store.getState();
    if (state.selectedNodeIds.length === 0) return null;
    let subgraph: CanvasSubgraph;
    let serialized: string;
    try {
      subgraph = copySubgraph(state.graph, state.selectedNodeIds);
      serialized = serializeCanvasSubgraph(subgraph);
      clipboardRef.current = subgraph;
    } catch {
      toast.error("复制失败，选区数据无效或过大");
      return null;
    }
    try {
      await navigator.clipboard?.writeText(serialized);
    } catch {
      // The in-memory clipboard remains available when browser permission is denied.
    }
    toast.success(`已复制 ${subgraph.nodes.length} 个节点`);
    return subgraph;
  }, [store]);

  const pasteSelection = useCallback(async () => {
    let subgraph = clipboardRef.current;
    try {
      const text = await navigator.clipboard?.readText();
      if (typeof text === "string") {
        subgraph = parseCanvasSubgraph(text);
      }
    } catch {
      // Use the in-memory clipboard if system clipboard access is unavailable.
    }
    if (!subgraph || subgraph.nodes.length === 0) {
      toast.error("剪贴板中没有可粘贴的画布节点");
      return;
    }
    clipboardRef.current = subgraph;
    let inserted: string[];
    try {
      inserted = store.getState().insertSubgraph(subgraph, {
        position: viewportApi?.getViewportCenter(),
      });
    } catch {
      toast.error("粘贴失败，剪贴板数据无效或超出画布限制");
      return;
    }
    if (inserted.length === 0) {
      toast.error("画布已达到节点、连接或存储大小上限");
      return;
    }
    window.requestAnimationFrame(() => viewportApi?.fitSelection(inserted));
    toast.success(`已粘贴 ${inserted.length} 个节点`);
  }, [store, viewportApi]);

  const duplicateSelection = useCallback(() => {
    const state = store.getState();
    if (state.selectedNodeIds.length === 0) return;
    let inserted: string[];
    try {
      inserted = state.duplicateNodes(state.selectedNodeIds);
    } catch {
      toast.error("复制失败，选区数据无效或超出画布限制");
      return;
    }
    if (inserted.length === 0) {
      toast.error("画布已达到节点、连接或存储大小上限");
      return;
    }
    window.requestAnimationFrame(() => viewportApi?.fitSelection(inserted));
  }, [store, viewportApi]);

  const alignSelection = useCallback(
    (alignment: CanvasAlignment) => {
      try {
        const nodes = selectedNodes(store.getState());
        store.getState().moveNodes(alignNodes(nodes, alignment));
      } catch {
        toast.error("对齐失败，节点位置数据无效");
      }
    },
    [store],
  );

  const distributeSelection = useCallback(
    (axis: CanvasDistributionAxis) => {
      try {
        const nodes = selectedNodes(store.getState());
        store.getState().moveNodes(distributeNodes(nodes, axis));
      } catch {
        toast.error("分布失败，节点位置数据无效");
      }
    },
    [store],
  );

  const autoLayoutSelection = useCallback(() => {
    try {
      const state = store.getState();
      const selected = new Set(state.selectedNodeIds);
      const nodes = state.graph.nodes.filter(
        (node) => selected.has(node.id) && node.type !== "frame",
      );
      if (nodes.length < 2) return;
      const nodeIds = new Set(nodes.map((node) => node.id));
      const subgraph: CanvasGraph = {
        schema_version: 1,
        nodes,
        edges: state.graph.edges.filter(
          (edge) =>
            nodeIds.has(edge.source_node_id) &&
            nodeIds.has(edge.target_node_id),
        ),
        frames: [],
        settings: state.graph.settings,
      };
      state.moveNodes(autoLayoutDag(subgraph));
      window.requestAnimationFrame(() =>
        viewportApi?.fitSelection(nodes.map((node) => node.id)),
      );
    } catch {
      toast.error("自动布局失败，节点结构或位置数据无效");
    }
  }, [store, viewportApi]);

  const autoLayoutCanvas = useCallback(() => {
    try {
      const state = store.getState();
      const nodes = state.graph.nodes.filter((node) => node.type !== "frame");
      if (nodes.length < 2) return;
      const nodeIds = new Set(nodes.map((node) => node.id));
      state.moveNodes(
        autoLayoutDag({
          ...state.graph,
          nodes,
          edges: state.graph.edges.filter(
            (edge) =>
              nodeIds.has(edge.source_node_id) &&
              nodeIds.has(edge.target_node_id),
          ),
        }),
      );
      window.requestAnimationFrame(() => viewportApi?.fitView());
    } catch {
      toast.error("自动布局失败，节点结构或位置数据无效");
    }
  }, [store, viewportApi]);

  const fitSelection = useCallback(
    () => viewportApi?.fitSelection(),
    [viewportApi],
  );

  const deleteSelection = useCallback(() => {
    const state = store.getState();
    state.removeElements(
      state.selectedNodeIds,
      state.selectedEdgeId ? [state.selectedEdgeId] : [],
    );
  }, [store]);

  const toggleGrid = useCallback(() => {
    const state = store.getState();
    state.updateDocumentSettings({
      snap_to_grid: !state.graph.settings.snap_to_grid,
    });
  }, [store]);

  const commandItems = useMemo(
    () =>
      buildCommandItems({
        graph,
        actionRequest,
        selectedNodeId,
        selectedCount,
        selectedEdgeId,
      }),
    [
      actionRequest,
      graph,
      selectedCount,
      selectedEdgeId,
      selectedNodeId,
    ],
  );

  const commandHandlers = useMemo<Record<string, () => void>>(
    () => ({
      "view:fit": () => viewportApi?.fitView(),
      "view:fit-selection": fitSelection,
      "selection:copy": () => void copySelection(),
      "selection:paste": () => void pasteSelection(),
      "selection:duplicate": duplicateSelection,
      "selection:delete": deleteSelection,
      "selection:auto-layout": autoLayoutSelection,
      "canvas:auto-layout": autoLayoutCanvas,
      "canvas:toggle-grid": toggleGrid,
      "run:selected": onRunSelected,
      "help:shortcuts": openShortcuts,
      "context:delete": () => deleteContextTarget(store, actionRequest),
    }),
    [
      actionRequest,
      autoLayoutCanvas,
      autoLayoutSelection,
      copySelection,
      deleteSelection,
      duplicateSelection,
      fitSelection,
      onRunSelected,
      openShortcuts,
      pasteSelection,
      store,
      toggleGrid,
      viewportApi,
    ],
  );

  const handleCommandSelect = useCallback(
    (item: CanvasCommandMenuItem) => {
      const addCatalogId = commandSuffix(item.id, "add");
      const focusNodeId = commandSuffix(item.id, "focus");
      if (addCatalogId) {
        addNode(addCatalogId, actionRequest?.position);
      } else if (focusNodeId) {
        store.getState().selectNode(focusNodeId);
        viewportApi?.focusNode(focusNodeId);
      } else {
        commandHandlers[item.id]?.();
      }
      setActionRequest(null);
    },
    [actionRequest, addNode, commandHandlers, store, viewportApi],
  );

  return {
    commandMenuOpen,
    commandMenuTitle: actionRequest
      ? actionRequest.connectionDraft
        ? "添加并连接节点"
        : "画布快捷操作"
      : "画布命令",
    commandItems,
    shortcutsOpen,
    selectedCount,
    openCommandMenu,
    openQuickAdd: openCommandMenu,
    openContextMenu: openCommandMenu,
    openShortcuts,
    setCommandMenuOpen,
    setShortcutsOpen,
    handleCommandSelect,
    addNode,
    copySelection,
    pasteSelection,
    duplicateSelection,
    alignSelection,
    distributeSelection,
    autoLayoutSelection,
    autoLayoutCanvas,
    fitSelection,
    deleteSelection,
    toggleGrid,
  };
}
