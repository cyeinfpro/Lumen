"use client";

import {
  ClipboardPaste,
  Copy,
  Grid3X3,
  Keyboard,
  LayoutGrid,
  LocateFixed,
  Play,
  Scan,
  Search,
  Trash2,
} from "lucide-react";
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
  validateCanvasConnection,
  validateCanvasNodeExecution,
} from "@/lib/canvas/graph";
import {
  CANVAS_NODE_CATALOG,
  CANVAS_NODE_SPECS,
  createCanvasNodeFromCatalog,
  findCanvasNodeCatalogItem,
  isCanvasExecutableNodeType,
  isCanvasNodeType,
} from "@/lib/canvas/registry";
import type { CanvasEditorStore } from "@/lib/canvas/store";
import type {
  CanvasGraph,
  CanvasNodeDefinition,
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

function buildCommandItems({
  graph,
  actionRequest,
  selectedNodeId,
  selectedCount,
  selectedEdgeId,
}: {
  graph: CanvasGraph;
  actionRequest: CanvasViewportActionRequest | null;
  selectedNodeId: string | null;
  selectedCount: number;
  selectedEdgeId: string | null;
}): CanvasCommandMenuItem[] {
  const draftType = actionRequest?.connectionDraft?.dataType ?? null;
  const selectedNode = graph.nodes.find((node) => node.id === selectedNodeId);
  const selectedNodeRunnable = Boolean(
    selectedNode &&
      isCanvasExecutableNodeType(selectedNode.type) &&
      validateCanvasNodeExecution(graph, selectedNode.id).valid,
  );
  const nodeItems = CANVAS_NODE_CATALOG.filter((item) =>
    !draftType ||
    catalogAcceptsConnection(
      graph,
      item.id,
      actionRequest?.connectionDraft ?? null,
    ),
  ).map((item) => {
    const spec = CANVAS_NODE_SPECS[item.type];
    return {
      id: `add:${item.id}`,
      kind: "node" as const,
      label: `添加${item.label}`,
      description: item.description,
      keywords: [item.id, item.type, ...item.keywords, ...spec.inputs.map((port) => port.label)],
      icon: spec.icon,
    };
  });
  const commandItems: CanvasCommandMenuItem[] = [
    {
      id: "view:fit",
      kind: "command",
      label: "适应全部节点",
      description: "将整个工作流放入当前视口",
      icon: Scan,
      shortcut: ["Mod", "0"],
    },
    {
      id: "view:fit-selection",
      kind: "command",
      label: "适应当前选区",
      icon: LocateFixed,
      shortcut: ["Shift", "2"],
      disabled: selectedCount === 0,
    },
    {
      id: "selection:copy",
      kind: "command",
      label: "复制选区",
      icon: Copy,
      shortcut: ["Mod", "C"],
      disabled: selectedCount === 0,
    },
    {
      id: "selection:paste",
      kind: "command",
      label: "粘贴节点",
      icon: ClipboardPaste,
      shortcut: ["Mod", "V"],
    },
    {
      id: "selection:duplicate",
      kind: "command",
      label: "重复选区",
      description: "复制节点及选区内部连线",
      icon: Copy,
      shortcut: ["Mod", "D"],
      disabled: selectedCount === 0,
    },
    {
      id: "selection:auto-layout",
      kind: "command",
      label: "整理选区",
      icon: LayoutGrid,
      shortcut: ["Shift", "A"],
      disabled: selectedCount < 2,
    },
    {
      id: "canvas:auto-layout",
      kind: "command",
      label: "整理整张画布",
      icon: LayoutGrid,
      disabled: graph.nodes.length < 2,
    },
    {
      id: "canvas:toggle-grid",
      kind: "command",
      label: graph.settings.snap_to_grid ? "关闭网格吸附" : "开启网格吸附",
      icon: Grid3X3,
      shortcut: ["G"],
    },
    {
      id: "run:selected",
      kind: "command",
      label: "运行当前节点",
      icon: Play,
      shortcut: ["Mod", "Enter"],
      disabled: selectedCount !== 1 || !selectedNodeRunnable,
    },
    {
      id: "selection:delete",
      kind: "command",
      label: "删除选区",
      icon: Trash2,
      shortcut: ["Delete"],
      disabled: selectedCount === 0 && !selectedEdgeId,
    },
    {
      id: "help:shortcuts",
      kind: "command",
      label: "查看画布快捷键",
      icon: Keyboard,
      shortcut: ["?"],
    },
  ];
  if (actionRequest?.nodeId || actionRequest?.edgeId) {
    commandItems.unshift({
      id: "context:delete",
      kind: "command",
      label: actionRequest.edgeId ? "删除连接" : "删除节点",
      icon: Trash2,
    });
  }
  const focusItems = graph.nodes.map((node) => ({
    id: `focus:${node.id}`,
    kind: "command" as const,
    label: `定位：${node.title}`,
    description: CANVAS_NODE_SPECS[node.type].label,
    keywords: [node.id, node.type],
    icon: Search,
  }));
  return [...nodeItems, ...commandItems, ...focusItems];
}

function selectedNodes(
  state: ReturnType<CanvasEditorStore["getState"]>,
): CanvasNodeDefinition[] {
  const selected = new Set(state.selectedNodeIds);
  return state.graph.nodes.filter((node) => selected.has(node.id));
}

function connectDraftToNewNode(
  store: CanvasEditorStore,
  nodeId: string,
  request: CanvasViewportActionRequest | null,
) {
  const draft = request?.connectionDraft;
  if (!draft) return;
  const state = store.getState();
  const node = state.graph.nodes.find((item) => item.id === nodeId);
  if (!node) return;
  const targetPort = CANVAS_NODE_SPECS[node.type].inputs.find(
    (port) =>
      validateCanvasConnection(state.graph, {
        sourceNodeId: draft.sourceNodeId,
        sourceHandle: draft.sourceHandle,
        targetNodeId: nodeId,
        targetHandle: port.id,
      }).valid,
  );
  if (targetPort) {
    const result = state.addEdge({
      sourceNodeId: draft.sourceNodeId,
      sourceHandle: draft.sourceHandle,
      targetNodeId: nodeId,
      targetHandle: targetPort.id,
    });
    if (!result.ok) toast.error(result.reason);
  } else {
    toast.error("新节点没有可用的兼容输入端口");
  }
  store.getState().setConnectionDraft(null);
}

function catalogAcceptsConnection(
  graph: CanvasGraph,
  catalogId: string,
  draft: CanvasViewportActionRequest["connectionDraft"],
): boolean {
  if (!draft) return true;
  const item = findCanvasNodeCatalogItem(catalogId);
  if (!item) return false;
  const candidate = createCanvasNodeFromCatalog(item.id, { x: 0, y: 0 }, {
    id: "__canvas_catalog_connection_candidate__",
  });
  const candidateGraph = {
    ...graph,
    nodes: [...graph.nodes, candidate],
  };
  return CANVAS_NODE_SPECS[candidate.type].inputs.some((port) =>
    validateCanvasConnection(candidateGraph, {
      sourceNodeId: draft.sourceNodeId,
      sourceHandle: draft.sourceHandle,
      targetNodeId: candidate.id,
      targetHandle: port.id,
    }).valid,
  );
}

function deleteContextTarget(
  store: CanvasEditorStore,
  request: CanvasViewportActionRequest | null,
) {
  if (request?.edgeId) {
    store.getState().removeEdges([request.edgeId]);
  } else if (request?.nodeId) {
    store.getState().removeNodes([request.nodeId]);
  }
}

function commandSuffix(id: string, prefix: string): string | null {
  const marker = `${prefix}:`;
  return id.startsWith(marker) ? id.slice(marker.length) : null;
}

function findOpenNodePosition(
  origin: CanvasPosition,
  width: number,
  height: number,
  nodes: readonly CanvasNodeDefinition[],
): CanvasPosition {
  for (let attempt = 0; attempt < 48; attempt += 1) {
    const ring = attempt === 0 ? 0 : Math.floor((attempt - 1) / 8) + 1;
    const spoke = attempt === 0 ? 0 : (attempt - 1) % 8;
    const angle = spoke * (Math.PI / 4);
    const distance = ring * 64;
    const candidate = {
      x: origin.x + Math.cos(angle) * distance,
      y: origin.y + Math.sin(angle) * distance,
    };
    if (
      !nodes.some((node) =>
        rectanglesOverlap(candidate, width, height, node),
      )
    ) {
      return candidate;
    }
  }
  return {
    x: origin.x + nodes.length * 18,
    y: origin.y + nodes.length * 18,
  };
}

function rectanglesOverlap(
  position: CanvasPosition,
  width: number,
  height: number,
  node: CanvasNodeDefinition,
): boolean {
  const nodeWidth = node.size?.width ?? CANVAS_NODE_SPECS[node.type].width;
  const nodeHeight = node.size?.height ?? (node.type === "frame" ? 220 : 180);
  const gap = 24;
  return !(
    position.x + width + gap <= node.position.x ||
    position.x >= node.position.x + nodeWidth + gap ||
    position.y + height + gap <= node.position.y ||
    position.y >= node.position.y + nodeHeight + gap
  );
}
