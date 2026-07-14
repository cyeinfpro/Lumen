import { createStore, type StoreApi } from "zustand/vanilla";

import {
  addCanvasNode,
  canvasGraphReadyToSave,
  cloneCanvasGraph,
  createCanvasEdge,
  MAX_CANVAS_GRAPH_BYTES,
  validateCanvasConnection,
  type CanvasConnectionInput,
} from "#canvas-graph";
import { CANVAS_AUTOSAVE_OPERATION_LIMIT } from "#canvas-autosave";
import {
  copySubgraph,
  insertSubgraph as insertCanvasSubgraph,
  type CanvasSubgraph,
  type InsertSubgraphOptions,
} from "./clipboard";
import { CANVAS_NODE_SPECS } from "#canvas-registry";
import type {
  CanvasDocumentSettings,
  CanvasEdgeDefinition,
  CanvasEdgeDetailsUpdate,
  CanvasEdgeRole,
  CanvasGraph,
  CanvasHistoryEntry,
  CanvasNodeDefinition,
  CanvasNodeAppearanceUpdate,
  CanvasPosition,
  CanvasSize,
  CanvasNodeType,
  CanvasOperation,
  CanvasSaveState,
  CanvasToolMode,
  ConnectionDraft,
} from "#canvas-types";

const HISTORY_LIMIT = 100;
const CANVAS_COORDINATE_LIMIT = 10_000_000;
export const CANVAS_HISTORY_GRAPH_BYTE_BUDGET = MAX_CANVAS_GRAPH_BYTES;
const CANVAS_EDGE_ROLES = new Set<CanvasEdgeRole>([
  "reference",
  "subject",
  "product",
  "style",
  "edit_target",
  "background",
  "other",
]);

export interface CanvasNodeMove {
  nodeId: string;
  position: CanvasPosition;
}

type MergeableCanvasHistoryEntry = CanvasHistoryEntry & {
  mergeKey?: string;
  graphBytes?: number;
};

export interface CanvasEditorState {
  graph: CanvasGraph;
  revision: number;
  selectedNodeId: string | null;
  selectedNodeIds: string[];
  selectedEdgeId: string | null;
  editingNodeId: string | null;
  toolMode: CanvasToolMode;
  connectionDraft: ConnectionDraft | null;
  activeInteractionCount: number;
  pendingOperations: CanvasOperation[];
  inFlightOperationCount: number;
  retryPrefixOperationCount: number;
  history: CanvasHistoryEntry[];
  future: CanvasHistoryEntry[];
  saveState: CanvasSaveState;
  saveMessage: string | null;
  hydrate: (graph: CanvasGraph, revision: number) => void;
  addNode: (type: CanvasNodeType, position: { x: number; y: number }) => string;
  updateNodeConfig: (nodeId: string, config: Record<string, unknown>) => void;
  beginNodeConfigEdit: (nodeId: string) => void;
  endNodeConfigEdit: (nodeId: string) => void;
  updateNodeAppearance: (
    nodeId: string,
    appearance: CanvasNodeAppearanceUpdate,
  ) => void;
  updateNodeTitle: (nodeId: string, title: string) => void;
  resizeNode: (
    nodeId: string,
    size: CanvasSize,
    position?: CanvasPosition,
  ) => void;
  moveNode: (nodeId: string, position: CanvasPosition) => void;
  moveNodes: (items: CanvasNodeMove[]) => void;
  duplicateNodes: (
    nodeIds: string[],
    offset?: CanvasPosition,
  ) => string[];
  insertSubgraph: (
    subgraph: CanvasSubgraph,
    options?: InsertSubgraphOptions,
  ) => string[];
  removeElements: (nodeIds: string[], edgeIds: string[]) => void;
  removeNodes: (nodeIds: string[]) => void;
  addEdge: (input: CanvasConnectionInput) => { ok: true } | { ok: false; reason: string };
  updateEdgeDetails: (
    edgeId: string,
    details: CanvasEdgeDetailsUpdate,
  ) => void;
  updateEdgeBinding: (
    edgeId: string,
    bindingMode: "follow_active" | "pinned",
    pinnedExecutionId?: string | null,
    pinnedOutputIndex?: number | null,
  ) => void;
  updateDocumentSettings: (
    settings: Partial<CanvasDocumentSettings>,
  ) => void;
  removeEdges: (edgeIds: string[]) => void;
  selectNode: (nodeId: string | null) => void;
  selectNodes: (nodeIds: string[]) => void;
  selectEdge: (edgeId: string | null) => void;
  beginNodeEdit: (nodeId: string) => void;
  endNodeEdit: (nodeId: string) => void;
  setToolMode: (mode: CanvasToolMode) => void;
  setConnectionDraft: (draft: ConnectionDraft | null) => void;
  beginInteraction: () => void;
  endInteraction: () => void;
  undo: () => void;
  redo: () => void;
  markSaving: (count?: number) => void;
  acknowledgeOperations: (count: number, revision: number) => boolean;
  markSaveError: (message: string, retryable?: boolean) => void;
  markConflict: (message: string) => void;
  replaceFromRemote: (graph: CanvasGraph, revision: number) => void;
}

export type CanvasEditorStore = StoreApi<CanvasEditorState>;

export function createCanvasEditorStore(
  graph: CanvasGraph,
  revision: number,
): CanvasEditorStore {
  return createStore<CanvasEditorState>((set, get) => {
    const commit = (
      nextGraph: CanvasGraph,
      label: string,
      operations: CanvasOperation[],
      historyMergeKey?: string,
    ) => {
      const current = get();
      const previous = current.history.at(-1) as
        | MergeableCanvasHistoryEntry
        | undefined;
      const mergesWithPrevious =
        historyMergeKey !== undefined &&
        previous?.mergeKey === historyMergeKey;
      const history = mergesWithPrevious
        ? current.history
        : trimHistoryEntries(
            [
              ...current.history,
              createHistoryEntry(
                current.graph,
                label,
                historyMergeKey,
              ),
            ],
            "newest",
          );
      set({
        graph: nextGraph,
        history,
        future: [],
        pendingOperations: coalescePendingOperations(
          current.pendingOperations,
          operations,
          current.retryPrefixOperationCount,
        ),
        ...dirtySaveStatus(current),
      });
    };

    const sealNodeConfigEdit = (nodeId: string) => {
      const current = get();
      const previous = current.history.at(-1) as
        | MergeableCanvasHistoryEntry
        | undefined;
      if (previous?.mergeKey !== `update_node_config:${nodeId}`) return;
      const sealed: MergeableCanvasHistoryEntry = {
        ...previous,
        mergeKey: undefined,
      };
      set({
        history: [
          ...current.history.slice(0, -1),
          sealed,
        ],
      });
    };

    const updateNodeAppearance = (
      nodeId: string,
      appearance: CanvasNodeAppearanceUpdate,
    ) => {
      const current = get().graph;
      const node = current.nodes.find((item) => item.id === nodeId);
      if (!node) return;
      const nextNode = nodeWithAppearance(node, appearance);
      const operation = nodeMetaOperation(node, nextNode);
      if (!operation) return;
      commit(
        {
          ...current,
          nodes: current.nodes.map((item) =>
            item.id === nodeId ? nextNode : item,
          ),
        },
        "更新节点外观",
        [operation],
      );
    };

    const resizeNode = (
      nodeId: string,
      size: CanvasSize,
      position?: CanvasPosition,
    ) => {
      if (
        !validCanvasSize(size) ||
        (position !== undefined && !validCanvasPosition(position))
      ) {
        return;
      }
      const current = get().graph;
      const node = current.nodes.find((item) => item.id === nodeId);
      if (!node) return;
      const sizeChanged = !canvasSizeEqual(node.size, size, node.type);
      const positionChanged =
        position !== undefined &&
        (node.position.x !== position.x || node.position.y !== position.y);
      if (!sizeChanged && !positionChanged) return;
      const nextSize = { ...size };
      const nextPosition = position ? { ...position } : node.position;
      const operations: CanvasOperation[] = [];
      if (positionChanged) {
        operations.push({
          op: "move_nodes",
          operation_schema_version: 1,
          items: [
            {
              node_id: nodeId,
              x: nextPosition.x,
              y: nextPosition.y,
            },
          ],
        });
      }
      if (sizeChanged) {
        operations.push({
          op: "resize_node",
          operation_schema_version: 1,
          node_id: nodeId,
          size: nextSize,
        });
      }
      commit(
        {
          ...current,
          nodes: current.nodes.map((item) =>
            item.id === nodeId
              ? {
                  ...item,
                  position: nextPosition,
                  size: nextSize,
                }
              : item,
          ),
        },
        positionChanged && sizeChanged ? "调整节点边界" : "调整节点尺寸",
        operations,
      );
    };

    const commitSubgraph = (
      subgraph: CanvasSubgraph,
      options: InsertSubgraphOptions,
      label: string,
    ): string[] => {
      if (
        subgraph.nodes.length + subgraph.edges.length >
        CANVAS_AUTOSAVE_OPERATION_LIMIT
      ) {
        return [];
      }
      const current = get().graph;
      const insertion = insertCanvasSubgraph(current, subgraph, options);
      if (
        insertion.nodes.length === 0 ||
        insertion.nodes.length + insertion.edges.length >
          CANVAS_AUTOSAVE_OPERATION_LIMIT ||
        !validGraphForStoreCommit(insertion.graph)
      ) {
        return [];
      }
      commit(insertion.graph, label, [
        ...insertion.nodes.map(
          (node): CanvasOperation => ({
            op: "add_node",
            operation_schema_version: 1,
            node,
          }),
        ),
        ...insertion.edges.map(
          (edge): CanvasOperation => ({
            op: "add_edge",
            operation_schema_version: 1,
            edge,
          }),
        ),
      ]);
      const insertedNodeIds = insertion.nodes.map((node) => node.id);
      set({
        selectedNodeId: insertedNodeIds[0] ?? null,
        selectedNodeIds: insertedNodeIds,
        selectedEdgeId: null,
      });
      return insertedNodeIds;
    };

    const updateEdgeDetails = (
      edgeId: string,
      details: CanvasEdgeDetailsUpdate,
    ) => {
      const current = get().graph;
      const edge = current.edges.find((item) => item.id === edgeId);
      if (!edge) return;
      const nextEdge = edgeWithDetails(edge, details);
      if (
        !nextEdge ||
        !validCanvasEdgeMetadata(
          nextEdge,
          current.nodes.find((node) => node.id === nextEdge.source_node_id)
            ?.type,
        ) ||
        canvasEdgeDetailsEqual(edge, nextEdge)
      ) {
        return;
      }
      commit(
        {
          ...current,
          edges: current.edges.map((item) =>
            item.id === edgeId ? nextEdge : item,
          ),
        },
        "更新连接详情",
        [edgeDetailsOperation(edge, nextEdge)],
      );
    };

    const updateDocumentSettings = (
      settings: Partial<CanvasDocumentSettings>,
    ) => {
      const current = get().graph;
      const nextSettings = { ...current.settings, ...settings };
      if (
        !validDocumentSettings(nextSettings) ||
        documentSettingsEqual(current.settings, nextSettings)
      ) {
        return;
      }
      commit(
        { ...current, settings: nextSettings },
        "更新画布设置",
        [
          {
            op: "update_document_settings",
            operation_schema_version: 1,
            settings: nextSettings,
          },
        ],
      );
    };

    const moveNodes = (items: CanvasNodeMove[]) => {
      if (items.length === 0) return;
      const current = get().graph;
      const nodesById = new Map(current.nodes.map((node) => [node.id, node]));
      const positions = new Map<string, { x: number; y: number }>();
      for (const item of items) {
        if (
          !nodesById.has(item.nodeId) ||
          !validCanvasPosition(item.position)
        ) {
          continue;
        }
        positions.set(item.nodeId, {
          x: item.position.x,
          y: item.position.y,
        });
      }
      const changedItems = [...positions]
        .filter(([nodeId, position]) => {
          const node = nodesById.get(nodeId);
          return (
            node &&
            (node.position.x !== position.x || node.position.y !== position.y)
          );
        })
        .map(([nodeId, position]) => ({ nodeId, position }));
      if (changedItems.length === 0) return;
      const changedPositions = new Map(
        changedItems.map((item) => [item.nodeId, item.position]),
      );
      commit(
        {
          ...current,
          nodes: current.nodes.map((node) => {
            const position = changedPositions.get(node.id);
            return position ? { ...node, position } : node;
          }),
        },
        "移动节点",
        [
          {
            op: "move_nodes",
            operation_schema_version: 1,
            items: changedItems.map(({ nodeId, position }) => ({
              node_id: nodeId,
              x: position.x,
              y: position.y,
            })),
          },
        ],
      );
    };

    const removeElements = (
      nodeIds: string[],
      edgeIds: string[],
      label = "删除元素",
    ) => {
      const current = get();
      const existingNodeIds = uniqueExistingIds(
        nodeIds,
        current.graph.nodes.map((node) => node.id),
      );
      const removedNodeIds = new Set(existingNodeIds);
      const associatedEdgeIds = current.graph.edges
        .filter(
          (edge) =>
            removedNodeIds.has(edge.source_node_id) ||
            removedNodeIds.has(edge.target_node_id),
        )
        .map((edge) => edge.id);
      const associatedEdges = new Set(associatedEdgeIds);
      const extraEdgeIds = uniqueExistingIds(
        edgeIds,
        current.graph.edges.map((edge) => edge.id),
      ).filter((edgeId) => !associatedEdges.has(edgeId));
      if (existingNodeIds.length === 0 && extraEdgeIds.length === 0) return;

      const removedEdgeIds = new Set([...associatedEdgeIds, ...extraEdgeIds]);
      const operations: CanvasOperation[] = [];
      if (existingNodeIds.length > 0) {
        operations.push({
          op: "remove_nodes",
          operation_schema_version: 1,
          node_ids: existingNodeIds,
          edge_ids: associatedEdgeIds,
        });
      }
      if (extraEdgeIds.length > 0) {
        operations.push({
          op: "remove_edges",
          operation_schema_version: 1,
          edge_ids: extraEdgeIds,
        });
      }
      commit(
        normalizeEdgeOrders({
          ...current.graph,
          nodes: current.graph.nodes.filter(
            (node) => !removedNodeIds.has(node.id),
          ),
          edges: current.graph.edges.filter(
            (edge) => !removedEdgeIds.has(edge.id),
          ),
        }),
        label,
        operations,
      );

      const selectedNodeIds = current.selectedNodeIds.filter(
        (nodeId) => !removedNodeIds.has(nodeId),
      );
      set({
        selectedNodeIds,
        selectedNodeId:
          current.selectedNodeId &&
          !removedNodeIds.has(current.selectedNodeId)
            ? current.selectedNodeId
            : (selectedNodeIds[0] ?? null),
        selectedEdgeId:
          current.selectedEdgeId &&
          !removedEdgeIds.has(current.selectedEdgeId)
            ? current.selectedEdgeId
            : null,
        editingNodeId:
          current.editingNodeId &&
          !removedNodeIds.has(current.editingNodeId)
            ? current.editingNodeId
            : null,
        connectionDraft: null,
      });
    };

    return {
      graph: cloneCanonicalCanvasGraph(graph),
      revision,
      selectedNodeId: null,
      selectedNodeIds: [],
      selectedEdgeId: null,
      editingNodeId: null,
      toolMode: "select",
      connectionDraft: null,
      activeInteractionCount: 0,
      pendingOperations: [],
      inFlightOperationCount: 0,
      retryPrefixOperationCount: 0,
      history: [],
      future: [],
      saveState: "idle",
      saveMessage: null,
      hydrate(nextGraph, nextRevision) {
        set({
          graph: cloneCanonicalCanvasGraph(nextGraph),
          revision: nextRevision,
          selectedNodeId: null,
          selectedNodeIds: [],
          selectedEdgeId: null,
          editingNodeId: null,
          connectionDraft: null,
          activeInteractionCount: 0,
          pendingOperations: [],
          inFlightOperationCount: 0,
          retryPrefixOperationCount: 0,
          history: [],
          future: [],
          saveState: "saved",
          saveMessage: null,
        });
      },
      addNode(type, position) {
        if (!validCanvasPosition(position)) return "";
        const result = addCanvasNode(get().graph, type, position);
        if (!validGraphForStoreCommit(result.graph)) return "";
        commit(result.graph, `添加${result.node.title}`, [
          { op: "add_node", operation_schema_version: 1, node: result.node },
        ]);
        set({
          selectedNodeId: result.node.id,
          selectedNodeIds: [result.node.id],
          selectedEdgeId: null,
        });
        return result.node.id;
      },
      updateNodeConfig(nodeId, config) {
        const current = get().graph;
        const node = current.nodes.find((item) => item.id === nodeId);
        if (!node || canvasConfigEqual(node.config, config)) return;
        const removedEdgeIds =
          node.type === "video_generate"
            ? incompatibleVideoEdgeIds(
                current,
                nodeId,
                String(config.mode ?? "t2v"),
              )
            : [];
        const nextGraph = {
          ...current,
          nodes: current.nodes.map((item) =>
            item.id === nodeId ? { ...item, config: { ...config } } : item,
          ),
          edges: current.edges.filter(
            (edge) => !removedEdgeIds.includes(edge.id),
          ),
        };
        const next =
          removedEdgeIds.length > 0
            ? normalizeEdgeOrders(nextGraph)
            : nextGraph;
        commit(
          next,
          `编辑${node.title}`,
          [
            ...(removedEdgeIds.length > 0
              ? [
                  {
                    op: "remove_edges" as const,
                    operation_schema_version: 1 as const,
                    edge_ids: removedEdgeIds,
                  },
                ]
              : []),
            {
              op: "update_node_config",
              operation_schema_version: 1,
              node_id: nodeId,
              config: { ...config },
            },
          ],
          removedEdgeIds.length === 0
            ? `update_node_config:${nodeId}`
            : undefined,
        );
      },
      beginNodeConfigEdit(nodeId) {
        sealNodeConfigEdit(nodeId);
      },
      endNodeConfigEdit(nodeId) {
        sealNodeConfigEdit(nodeId);
      },
      updateNodeAppearance(nodeId, appearance) {
        updateNodeAppearance(nodeId, appearance);
      },
      updateNodeTitle(nodeId, title) {
        updateNodeAppearance(nodeId, { title });
      },
      resizeNode(nodeId, size, position) {
        resizeNode(nodeId, size, position);
      },
      moveNode(nodeId, position) {
        moveNodes([{ nodeId, position }]);
      },
      moveNodes(items) {
        moveNodes(items);
      },
      duplicateNodes(nodeIds, offset) {
        const subgraph = copySubgraph(get().graph, nodeIds);
        return commitSubgraph(
          subgraph,
          offset ? { offset } : {},
          "复制节点",
        );
      },
      insertSubgraph(subgraph, options = {}) {
        return commitSubgraph(subgraph, options, "粘贴节点");
      },
      removeElements(nodeIds, edgeIds) {
        removeElements(nodeIds, edgeIds);
      },
      removeNodes(nodeIds) {
        removeElements(nodeIds, [], "删除节点");
      },
      addEdge(input) {
        const current = get().graph;
        const validation = validateCanvasConnection(current, input);
        if (!validation.valid) return { ok: false, reason: validation.reason };
        const edge = createCanvasEdge(current, input);
        if (!edge) return { ok: false, reason: "连接无效" };
        const nextGraph = { ...current, edges: [...current.edges, edge] };
        if (!validGraphForStoreCommit(nextGraph)) {
          return { ok: false, reason: "画布已达到容量上限" };
        }
        commit(nextGraph, "连接节点", [
          { op: "add_edge", operation_schema_version: 1, edge },
        ]);
        set({
          selectedEdgeId: edge.id,
          selectedNodeId: null,
          selectedNodeIds: [],
          connectionDraft: null,
        });
        return { ok: true };
      },
      updateEdgeDetails(edgeId, details) {
        updateEdgeDetails(edgeId, details);
      },
      updateEdgeBinding(
        edgeId,
        bindingMode,
        pinnedExecutionId = null,
        pinnedOutputIndex = null,
      ) {
        updateEdgeDetails(edgeId, {
          binding_mode: bindingMode,
          pinned_execution_id:
            bindingMode === "pinned" ? pinnedExecutionId : null,
          pinned_output_index:
            bindingMode === "pinned" ? pinnedOutputIndex : null,
        });
      },
      updateDocumentSettings(settings) {
        updateDocumentSettings(settings);
      },
      removeEdges(edgeIds) {
        removeElements([], edgeIds, "删除连接");
      },
      selectNode(nodeId) {
        if (nodeId === null) {
          const current = get();
          if (
            current.selectedNodeId === null &&
            current.selectedNodeIds.length === 0 &&
            current.selectedEdgeId === null
          ) {
            return;
          }
          set({
            selectedNodeId: null,
            selectedNodeIds: [],
            selectedEdgeId: null,
          });
          return;
        }
        const current = get();
        if (!current.graph.nodes.some((node) => node.id === nodeId)) return;
        if (
          current.selectedNodeId === nodeId &&
          current.selectedNodeIds.includes(nodeId) &&
          current.selectedEdgeId === null
        ) {
          return;
        }
        set({
          selectedNodeId: nodeId,
          selectedNodeIds: current.selectedNodeIds.includes(nodeId)
            ? current.selectedNodeIds
            : [nodeId],
          selectedEdgeId: null,
        });
      },
      selectNodes(nodeIds) {
        const current = get();
        const selectedNodeIds = uniqueExistingIds(
          nodeIds,
          current.graph.nodes.map((node) => node.id),
        );
        const selectedNodeId =
          current.selectedNodeId &&
          selectedNodeIds.includes(current.selectedNodeId)
            ? current.selectedNodeId
            : (selectedNodeIds[0] ?? null);
        if (
          sameIds(current.selectedNodeIds, selectedNodeIds) &&
          current.selectedNodeId === selectedNodeId &&
          current.selectedEdgeId === null
        ) {
          return;
        }
        set({
          selectedNodeIds,
          selectedNodeId,
          selectedEdgeId: null,
        });
      },
      selectEdge(edgeId) {
        const current = get();
        if (
          current.selectedEdgeId === edgeId &&
          current.selectedNodeId === null &&
          current.selectedNodeIds.length === 0
        ) {
          return;
        }
        set({
          selectedEdgeId: edgeId,
          selectedNodeId: null,
          selectedNodeIds: [],
        });
      },
      beginNodeEdit(nodeId) {
        const current = get();
        if (
          current.editingNodeId === nodeId ||
          !current.graph.nodes.some((node) => node.id === nodeId)
        ) {
          return;
        }
        set({ editingNodeId: nodeId });
      },
      endNodeEdit(nodeId) {
        if (get().editingNodeId !== nodeId) return;
        set({ editingNodeId: null });
      },
      setToolMode(toolMode) {
        set({
          toolMode,
          connectionDraft: null,
          editingNodeId: toolMode === "select" ? get().editingNodeId : null,
        });
      },
      setConnectionDraft(connectionDraft) {
        set((current) => ({
          connectionDraft,
          editingNodeId: connectionDraft ? null : current.editingNodeId,
        }));
      },
      beginInteraction() {
        set((current) => ({
          activeInteractionCount: current.activeInteractionCount + 1,
        }));
      },
      endInteraction() {
        set((current) => ({
          activeInteractionCount: Math.max(
            0,
            current.activeInteractionCount - 1,
          ),
        }));
      },
      undo() {
        const current = get();
        const previous = current.history.at(-1);
        if (!previous) return;
        const operations = operationsBetween(current.graph, previous.graph);
        if (
          operations.length > CANVAS_AUTOSAVE_OPERATION_LIMIT ||
          !validGraphForStoreCommit(previous.graph)
        ) {
          return;
        }
        set({
          graph: cloneCanvasGraph(previous.graph),
          history: current.history.slice(0, -1),
          future: trimHistoryEntries(
            [
              createHistoryEntry(current.graph, previous.label),
              ...current.future,
            ],
            "oldest",
          ),
          pendingOperations: [...current.pendingOperations, ...operations],
          ...dirtySaveStatus(current),
          selectedNodeId: null,
          selectedNodeIds: [],
          selectedEdgeId: null,
          editingNodeId: null,
          connectionDraft: null,
        });
      },
      redo() {
        const current = get();
        const nextEntry = current.future[0];
        if (!nextEntry) return;
        const operations = operationsBetween(current.graph, nextEntry.graph);
        if (
          operations.length > CANVAS_AUTOSAVE_OPERATION_LIMIT ||
          !validGraphForStoreCommit(nextEntry.graph)
        ) {
          return;
        }
        set({
          graph: cloneCanvasGraph(nextEntry.graph),
          history: trimHistoryEntries(
            [
              ...current.history,
              createHistoryEntry(current.graph, nextEntry.label),
            ],
            "newest",
          ),
          future: current.future.slice(1),
          pendingOperations: [...current.pendingOperations, ...operations],
          ...dirtySaveStatus(current),
          selectedNodeId: null,
          selectedNodeIds: [],
          selectedEdgeId: null,
          editingNodeId: null,
          connectionDraft: null,
        });
      },
      markSaving(count) {
        const current = get();
        if (current.saveState === "conflict") return;
        const pendingCount = current.pendingOperations.length;
        const activeCount = Math.min(
          Math.max(0, count ?? pendingCount),
          pendingCount,
        );
        set({
          inFlightOperationCount: activeCount,
          retryPrefixOperationCount: activeCount,
          saveState: "saving",
          saveMessage: null,
        });
      },
      acknowledgeOperations(count, nextRevision) {
        const current = get();
        const acknowledgedCount = Math.min(
          Math.max(0, count),
          current.inFlightOperationCount,
        );
        if (acknowledgedCount === 0) return false;
        const pending = current.pendingOperations.slice(acknowledgedCount);
        set({
          pendingOperations: pending,
          inFlightOperationCount: 0,
          retryPrefixOperationCount: 0,
          revision: nextRevision,
          saveState: pending.length > 0 ? "dirty" : "saved",
          saveMessage: null,
        });
        return true;
      },
      markSaveError(message, retryable = true) {
        set((current) =>
          current.saveState === "conflict"
            ? {}
            : {
                inFlightOperationCount: 0,
                retryPrefixOperationCount: retryable
                  ? current.retryPrefixOperationCount
                  : 0,
                saveState: "error",
                saveMessage: message,
              },
        );
      },
      markConflict(message) {
        set({
          inFlightOperationCount: 0,
          retryPrefixOperationCount: 0,
          saveState: "conflict",
          saveMessage: message,
        });
      },
      replaceFromRemote(nextGraph, nextRevision) {
        set({
          graph: cloneCanonicalCanvasGraph(nextGraph),
          revision: nextRevision,
          selectedNodeId: null,
          selectedNodeIds: [],
          selectedEdgeId: null,
          editingNodeId: null,
          connectionDraft: null,
          activeInteractionCount: 0,
          history: [],
          future: [],
          pendingOperations: [],
          inFlightOperationCount: 0,
          retryPrefixOperationCount: 0,
          saveState: "saved",
          saveMessage: null,
        });
      },
    };
  });
}

function dirtySaveStatus(
  state: Pick<CanvasEditorState, "saveState" | "saveMessage">,
): Pick<CanvasEditorState, "saveState" | "saveMessage"> {
  return state.saveState === "conflict"
    ? {
        saveState: "conflict",
        saveMessage: state.saveMessage,
      }
    : {
        saveState: "dirty",
        saveMessage: null,
      };
}

function canvasConfigEqual(
  left: Record<string, unknown>,
  right: Record<string, unknown>,
): boolean {
  return jsonValueEqual(left, right);
}

function jsonValueEqual(left: unknown, right: unknown): boolean {
  if (Object.is(left, right)) return true;
  if (Array.isArray(left) || Array.isArray(right)) {
    return (
      Array.isArray(left) &&
      Array.isArray(right) &&
      left.length === right.length &&
      left.every((value, index) => jsonValueEqual(value, right[index]))
    );
  }
  if (
    !left ||
    !right ||
    typeof left !== "object" ||
    typeof right !== "object"
  ) {
    return false;
  }
  const leftRecord = left as Record<string, unknown>;
  const rightRecord = right as Record<string, unknown>;
  const leftKeys = Object.keys(leftRecord);
  const rightKeys = Object.keys(rightRecord);
  return (
    leftKeys.length === rightKeys.length &&
    leftKeys.every(
      (key) =>
        Object.prototype.hasOwnProperty.call(rightRecord, key) &&
        jsonValueEqual(leftRecord[key], rightRecord[key]),
    )
  );
}

function cloneCanonicalCanvasGraph(graph: CanvasGraph): CanvasGraph {
  const cloned = cloneCanvasGraph(graph);
  return {
    ...cloned,
    nodes: cloned.nodes.map((node) => ({
      ...node,
      size: canonicalCanvasSize(node.size, node.type),
    })),
  };
}

function createHistoryEntry(
  graph: CanvasGraph,
  label: string,
  mergeKey?: string,
): MergeableCanvasHistoryEntry {
  const cloned = cloneCanvasGraph(graph);
  return {
    graph: cloned,
    label,
    mergeKey,
    graphBytes: canvasGraphByteLength(cloned),
  };
}

function trimHistoryEntries(
  entries: CanvasHistoryEntry[],
  keep: "newest" | "oldest",
): CanvasHistoryEntry[] {
  const candidates =
    keep === "newest"
      ? [...entries].reverse().slice(0, HISTORY_LIMIT)
      : entries.slice(0, HISTORY_LIMIT);
  const retained: CanvasHistoryEntry[] = [];
  let retainedBytes = 0;
  for (const entry of candidates) {
    const mergeable = entry as MergeableCanvasHistoryEntry;
    const entryBytes =
      mergeable.graphBytes ?? canvasGraphByteLength(entry.graph);
    if (
      entryBytes > CANVAS_HISTORY_GRAPH_BYTE_BUDGET - retainedBytes
    ) {
      break;
    }
    const retainedEntry: MergeableCanvasHistoryEntry =
      mergeable.graphBytes === entryBytes
        ? mergeable
        : { ...mergeable, graphBytes: entryBytes };
    retained.push(retainedEntry);
    retainedBytes += entryBytes;
  }
  return keep === "newest" ? retained.reverse() : retained;
}

function canvasGraphByteLength(graph: CanvasGraph): number {
  try {
    return new TextEncoder().encode(JSON.stringify(graph)).byteLength;
  } catch {
    return CANVAS_HISTORY_GRAPH_BYTE_BUDGET + 1;
  }
}

function nodeWithAppearance(
  node: CanvasNodeDefinition,
  appearance: CanvasNodeAppearanceUpdate,
): CanvasNodeDefinition {
  const title =
    hasOwn(appearance, "title") && typeof appearance.title === "string"
      ? appearance.title.trim().slice(0, 255) || node.title
      : node.title;
  const parentGroupId =
    hasOwn(appearance, "parent_group_id") &&
    appearance.parent_group_id !== undefined
      ? appearance.parent_group_id
      : node.parent_group_id;
  const ui =
    appearance.ui === undefined
      ? node.ui
      : { ...node.ui, ...appearance.ui };
  return {
    ...node,
    title,
    parent_group_id: parentGroupId,
    ui,
  };
}

function nodeMetaOperation(
  before: CanvasNodeDefinition,
  after: CanvasNodeDefinition,
): Extract<CanvasOperation, { op: "update_node_meta" }> | null {
  const operation: Extract<CanvasOperation, { op: "update_node_meta" }> = {
    op: "update_node_meta",
    operation_schema_version: 1,
    node_id: after.id,
  };
  let changed = false;
  if (before.title !== after.title) {
    operation.title = after.title;
    changed = true;
  }
  if (
    (before.parent_group_id ?? null) !== (after.parent_group_id ?? null)
  ) {
    operation.parent_group_id = after.parent_group_id ?? null;
    changed = true;
  }
  if (!jsonValueEqual(before.ui, after.ui)) {
    operation.ui = { ...after.ui };
    changed = true;
  }
  return changed ? operation : null;
}

function canvasSizeEqual(
  left: CanvasSize | null | undefined,
  right: CanvasSize | null | undefined,
  nodeType: CanvasNodeType,
): boolean {
  const canonicalLeft = canonicalCanvasSize(left, nodeType);
  const canonicalRight = canonicalCanvasSize(right, nodeType);
  return (
    canonicalLeft.width === canonicalRight.width &&
    canonicalLeft.height === canonicalRight.height
  );
}

function canonicalCanvasSize(
  size: CanvasSize | null | undefined,
  nodeType: CanvasNodeType,
): CanvasSize {
  if (size) return { ...size };
  return {
    width: CANVAS_NODE_SPECS[nodeType].width,
    height: nodeType === "frame" ? 220 : 180,
  };
}

function validCanvasSize(size: CanvasSize): boolean {
  return (
    Number.isFinite(size.width) &&
    Number.isFinite(size.height) &&
    size.width >= 40 &&
    size.width <= 10_000 &&
    size.height >= 40 &&
    size.height <= 10_000
  );
}

function validCanvasPosition(position: CanvasPosition): boolean {
  return (
    Number.isFinite(position.x) &&
    Number.isFinite(position.y) &&
    position.x >= -CANVAS_COORDINATE_LIMIT &&
    position.x <= CANVAS_COORDINATE_LIMIT &&
    position.y >= -CANVAS_COORDINATE_LIMIT &&
    position.y <= CANVAS_COORDINATE_LIMIT
  );
}

function validGraphForStoreCommit(graph: CanvasGraph): boolean {
  const nodeTypesById = new Map(
    graph.nodes.map((node) => [node.id, node.type]),
  );
  return (
    canvasGraphReadyToSave(graph) &&
    graph.nodes.every(
      (node) =>
        validCanvasPosition(node.position) &&
        validCanvasSize(canonicalCanvasSize(node.size, node.type)),
    ) &&
    graph.edges.every((edge) =>
      validCanvasEdgeMetadata(edge, nodeTypesById.get(edge.source_node_id)),
    )
  );
}

function validCanvasEdgeMetadata(
  edge: CanvasEdgeDefinition,
  sourceType: CanvasNodeType | undefined,
): boolean {
  if (!validEdgeRole(edge.role) || !validEdgeOrder(edge.order)) return false;
  if (
    edge.role != null &&
    edge.data_type !== "image" &&
    edge.data_type !== "mask"
  ) {
    return false;
  }
  if (edge.binding_mode === "follow_active") {
    return (
      edge.pinned_execution_id == null &&
      edge.pinned_output_index == null
    );
  }
  return (
    edge.binding_mode === "pinned" &&
    (sourceType === "image_generate" || sourceType === "video_generate") &&
    validPinnedBinding(
      edge.pinned_execution_id,
      edge.pinned_output_index,
    )
  );
}

function edgeWithDetails(
  edge: CanvasEdgeDefinition,
  details: CanvasEdgeDetailsUpdate,
): CanvasEdgeDefinition | null {
  const role = hasOwn(details, "role") ? details.role : edge.role;
  if (!validEdgeRole(role)) return null;
  const binding = resolveEdgeBinding(edge, details);
  if (!binding) return null;
  const order = hasOwn(details, "order") ? details.order : edge.order;
  if (!validEdgeOrder(order)) return null;
  return {
    ...edge,
    ...binding,
    role,
    order,
  };
}

function resolveEdgeBinding(
  edge: CanvasEdgeDefinition,
  details: CanvasEdgeDetailsUpdate,
): Pick<
  CanvasEdgeDefinition,
  "binding_mode" | "pinned_execution_id" | "pinned_output_index"
> | null {
  const bindingMode = hasOwn(details, "binding_mode")
    ? details.binding_mode
    : edge.binding_mode;
  if (
    bindingMode !== "follow_active" &&
    bindingMode !== "pinned"
  ) {
    return null;
  }
  const pinnedExecutionId = hasOwn(details, "pinned_execution_id")
    ? details.pinned_execution_id
    : edge.pinned_execution_id;
  const pinnedOutputIndex = hasOwn(details, "pinned_output_index")
    ? details.pinned_output_index
    : edge.pinned_output_index;
  if (bindingMode === "follow_active") {
    if (
      (hasOwn(details, "pinned_execution_id") &&
        pinnedExecutionId != null) ||
      (hasOwn(details, "pinned_output_index") &&
        pinnedOutputIndex != null)
    ) {
      return null;
    }
    return {
      binding_mode: bindingMode,
      pinned_execution_id: null,
      pinned_output_index: null,
    };
  }
  if (!validPinnedBinding(pinnedExecutionId, pinnedOutputIndex)) return null;
  return {
    binding_mode: bindingMode,
    pinned_execution_id: pinnedExecutionId,
    pinned_output_index: pinnedOutputIndex,
  };
}

function validPinnedBinding(
  executionId: string | null | undefined,
  outputIndex: number | null | undefined,
): executionId is string {
  return (
    typeof executionId === "string" &&
    executionId.length > 0 &&
    executionId.length <= 36 &&
    Number.isSafeInteger(outputIndex) &&
    (outputIndex ?? -1) >= 0
  );
}

function validEdgeRole(
  role: unknown,
): role is CanvasEdgeRole | null | undefined {
  return (
    role == null ||
    (typeof role === "string" &&
      CANVAS_EDGE_ROLES.has(role as CanvasEdgeRole))
  );
}

function validEdgeOrder(order: number | null | undefined): boolean {
  return order == null || (Number.isSafeInteger(order) && order >= 0);
}

function canvasEdgeDetailsEqual(
  left: CanvasEdgeDefinition,
  right: CanvasEdgeDefinition,
): boolean {
  return (
    left.binding_mode === right.binding_mode &&
    (left.pinned_execution_id ?? null) ===
      (right.pinned_execution_id ?? null) &&
    (left.pinned_output_index ?? null) ===
      (right.pinned_output_index ?? null) &&
    (left.role ?? null) === (right.role ?? null) &&
    (left.order ?? null) === (right.order ?? null)
  );
}

function edgeDetailsOperation(
  before: CanvasEdgeDefinition,
  after: CanvasEdgeDefinition,
): Extract<CanvasOperation, { op: "update_edge" }> {
  const operation = edgeBindingOperation(after);
  if ((before.role ?? null) !== (after.role ?? null)) {
    operation.role = after.role ?? null;
  }
  if ((before.order ?? null) !== (after.order ?? null)) {
    operation.order = after.order ?? null;
  }
  return operation;
}

function validDocumentSettings(settings: CanvasDocumentSettings): boolean {
  return (
    typeof settings.snap_to_grid === "boolean" &&
    Number.isInteger(settings.grid_size) &&
    settings.grid_size >= 1 &&
    settings.grid_size <= 256
  );
}

function documentSettingsEqual(
  left: CanvasDocumentSettings,
  right: CanvasDocumentSettings,
): boolean {
  return (
    left.snap_to_grid === right.snap_to_grid &&
    left.grid_size === right.grid_size
  );
}

function hasOwn<T extends object>(value: T, key: PropertyKey): boolean {
  return Object.prototype.hasOwnProperty.call(value, key);
}

function coalescePendingOperations(
  pending: CanvasOperation[],
  incoming: CanvasOperation[],
  protectedPrefixCount = 0,
): CanvasOperation[] {
  if (incoming.length !== 1 || incoming[0].op !== "update_node_config") {
    return [...pending, ...incoming];
  }
  const protectedCount = Math.min(
    Math.max(0, protectedPrefixCount),
    pending.length,
  );
  if (pending.length <= protectedCount) {
    return [...pending, incoming[0]];
  }
  const previous = pending.at(-1);
  if (
    previous?.op !== "update_node_config" ||
    previous.node_id !== incoming[0].node_id
  ) {
    return [...pending, incoming[0]];
  }
  return [...pending.slice(0, -1), incoming[0]];
}

export function operationsBetween(
  current: CanvasGraph,
  next: CanvasGraph,
): CanvasOperation[] {
  const currentNodes = new Map(current.nodes.map((node) => [node.id, node]));
  const nextNodes = new Map(next.nodes.map((node) => [node.id, node]));
  const currentEdges = new Map(current.edges.map((edge) => [edge.id, edge]));
  const nextEdgeIds = new Set(next.edges.map((edge) => edge.id));
  const removedNodeIds = current.nodes
    .filter((node) => !nextNodes.has(node.id))
    .map((node) => node.id);
  return [
    ...removedEntityOperations(current, nextEdgeIds, removedNodeIds),
    ...changedNodeOperations(currentNodes, next),
    ...changedEdgeOperations(currentEdges, next),
    ...(documentSettingsEqual(current.settings, next.settings)
      ? []
      : [
          {
            op: "update_document_settings" as const,
            operation_schema_version: 1 as const,
            settings: { ...next.settings },
          },
        ]),
  ];
}

function removedEntityOperations(
  current: CanvasGraph,
  nextEdgeIds: ReadonlySet<string>,
  removedNodeIds: string[],
): CanvasOperation[] {
  const removedNodes = new Set(removedNodeIds);
  const associatedEdgeIds = current.edges
    .filter(
      (edge) =>
        removedNodes.has(edge.source_node_id) ||
        removedNodes.has(edge.target_node_id),
    )
    .map((edge) => edge.id);
  const associatedEdges = new Set(associatedEdgeIds);
  const independentlyRemovedEdgeIds = current.edges
    .filter(
      (edge) =>
        !associatedEdges.has(edge.id) &&
        !nextEdgeIds.has(edge.id),
    )
    .map((edge) => edge.id);
  const operations: CanvasOperation[] = [];
  if (removedNodeIds.length > 0) {
    operations.push({
      op: "remove_nodes",
      operation_schema_version: 1,
      node_ids: removedNodeIds,
      edge_ids: associatedEdgeIds,
    });
  }
  if (independentlyRemovedEdgeIds.length > 0) {
    operations.push({
      op: "remove_edges",
      operation_schema_version: 1,
      edge_ids: independentlyRemovedEdgeIds,
    });
  }
  return operations;
}

function changedNodeOperations(
  currentNodes: Map<string, CanvasGraph["nodes"][number]>,
  next: CanvasGraph,
): CanvasOperation[] {
  const operations: CanvasOperation[] = [];
  const movedItems: Extract<
    CanvasOperation,
    { op: "move_nodes" }
  >["items"] = [];
  for (const node of next.nodes) {
    const before = currentNodes.get(node.id);
    if (!before) {
      operations.push({ op: "add_node", operation_schema_version: 1, node });
      continue;
    }
    const metaOperation = nodeMetaOperation(before, node);
    if (metaOperation) operations.push(metaOperation);
    if (!canvasConfigEqual(before.config, node.config)) {
      operations.push({
        op: "update_node_config",
        operation_schema_version: 1,
        node_id: node.id,
        config: node.config,
      });
    }
    const nextSize = canonicalCanvasSize(node.size, node.type);
    if (!canvasSizeEqual(before.size, node.size, node.type)) {
      operations.push({
        op: "resize_node",
        operation_schema_version: 1,
        node_id: node.id,
        size: nextSize,
      });
    }
    if (
      validCanvasPosition(node.position) &&
      (before.position.x !== node.position.x ||
        before.position.y !== node.position.y)
    ) {
      movedItems.push({
        node_id: node.id,
        x: node.position.x,
        y: node.position.y,
      });
    }
  }
  if (movedItems.length > 0) {
    operations.push({
      op: "move_nodes",
      operation_schema_version: 1,
      items: movedItems,
    });
  }
  return operations;
}

function uniqueExistingIds(ids: string[], existingIds: string[]): string[] {
  const existing = new Set(existingIds);
  return [...new Set(ids)].filter((id) => existing.has(id));
}

function sameIds(left: string[], right: string[]): boolean {
  return (
    left.length === right.length &&
    left.every((value, index) => value === right[index])
  );
}

function changedEdgeOperations(
  currentEdges: ReadonlyMap<string, CanvasGraph["edges"][number]>,
  next: CanvasGraph,
): CanvasOperation[] {
  const operations: CanvasOperation[] = [];
  for (const edge of next.edges) {
    const before = currentEdges.get(edge.id);
    if (!before) {
      operations.push({ op: "add_edge", operation_schema_version: 1, edge });
    } else if (!canvasEdgeDetailsEqual(before, edge)) {
      operations.push(edgeDetailsOperation(before, edge));
    }
  }
  return operations;
}

function edgeBindingOperation(
  edge: CanvasGraph["edges"][number],
): Extract<CanvasOperation, { op: "update_edge" }> {
  return {
    op: "update_edge",
    operation_schema_version: 1,
    edge_id: edge.id,
    binding_mode: edge.binding_mode,
    pinned_execution_id: edge.pinned_execution_id ?? null,
    pinned_output_index: edge.pinned_output_index ?? null,
  };
}

function normalizeEdgeOrders(graph: CanvasGraph): CanvasGraph {
  const groups = new Map<string, CanvasGraph["edges"]>();
  for (const edge of graph.edges) {
    const key = `${edge.target_node_id}\0${edge.target_handle}`;
    groups.set(key, [...(groups.get(key) ?? []), edge]);
  }
  const normalized = new Map<string, number>();
  for (const edges of groups.values()) {
    edges
      .sort(
        (left, right) =>
          (left.order ?? 0) - (right.order ?? 0) ||
          left.id.localeCompare(right.id),
      )
      .forEach((edge, order) => normalized.set(edge.id, order));
  }
  return {
    ...graph,
    edges: graph.edges.map((edge) => ({
      ...edge,
      order: normalized.get(edge.id) ?? 0,
    })),
  };
}

function incompatibleVideoEdgeIds(
  graph: CanvasGraph,
  nodeId: string,
  mode: string,
): string[] {
  const blocked =
    mode === "t2v"
      ? new Set(["first_frame", "reference_images", "reference_videos"])
      : mode === "i2v"
        ? new Set(["reference_images", "reference_videos"])
        : new Set(["first_frame"]);
  return graph.edges
    .filter(
      (edge) =>
        edge.target_node_id === nodeId && blocked.has(edge.target_handle),
    )
    .map((edge) => edge.id);
}
