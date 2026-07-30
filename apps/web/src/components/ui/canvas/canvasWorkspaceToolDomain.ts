import { ClipboardPaste, Copy, Grid3X3, Keyboard, LayoutGrid, LocateFixed, Play, Scan, Search, Trash2 } from "lucide-react";

import { validateCanvasConnection, validateCanvasNodeExecution } from "@/lib/canvas/graph";
import {
  CANVAS_NODE_CATALOG,
  CANVAS_NODE_SPECS,
  createCanvasNodeFromCatalog,
  findCanvasNodeCatalogItem,
  isCanvasExecutableNodeType,
} from "@/lib/canvas/registry";
import type { CanvasEditorStore } from "@/lib/canvas/store";
import type { CanvasGraph, CanvasNodeDefinition, CanvasPosition } from "@/lib/canvas/types";
import { toast } from "@/components/ui/primitives";
import type { CanvasCommandMenuItem } from "./CanvasCommandMenu";
import type { CanvasViewportActionRequest } from "./CanvasViewport";

export function buildCommandItems({
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

export function selectedNodes(
  state: ReturnType<CanvasEditorStore["getState"]>,
): CanvasNodeDefinition[] {
  const selected = new Set(state.selectedNodeIds);
  return state.graph.nodes.filter((node) => selected.has(node.id));
}

export function connectDraftToNewNode(
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

export function catalogAcceptsConnection(
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

export function deleteContextTarget(
  store: CanvasEditorStore,
  request: CanvasViewportActionRequest | null,
) {
  if (request?.edgeId) {
    store.getState().removeEdges([request.edgeId]);
  } else if (request?.nodeId) {
    store.getState().removeNodes([request.nodeId]);
  }
}

export function commandSuffix(id: string, prefix: string): string | null {
  const marker = `${prefix}:`;
  return id.startsWith(marker) ? id.slice(marker.length) : null;
}

export function findOpenNodePosition(
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

export function rectanglesOverlap(
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
