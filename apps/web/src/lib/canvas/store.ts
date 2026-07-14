import { createStore, type StoreApi } from "zustand/vanilla";

import {
  addCanvasNode,
  cloneCanvasGraph,
  createCanvasEdge,
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
import {
  canvasConfigEqual,
  canvasSizeEqual,
  cloneCanonicalCanvasGraph,
  coalescePendingOperationGroups,
  consumePendingOperationGroups,
  createHistoryEntry,
  dirtySaveStatus,
  documentSettingsEqual,
  edgeWithDetails,
  incompatibleVideoEdgeIds,
  nodeMetaOperation,
  nodeWithAppearance,
  normalizeEdgeOrders,
  normalizePendingOperationGroupSizes,
  operationsBetween,
  reorderCanvasEdgeGroup,
  sameIds,
  trimHistoryEntries,
  uniqueExistingIds,
  validCanvasEdgeMetadata,
  validCanvasPosition,
  validCanvasSize,
  validDocumentSettings,
  validGraphForStoreCommit,
  type MergeableCanvasHistoryEntry,
} from "./storeHelpers";
import {
  canvasFixedVideoMode,
  type CanvasNodeCreateOverrides,
} from "#canvas-registry";
import type {
  CanvasDocumentSettings,
  CanvasEdgeDetailsUpdate,
  CanvasGraph,
  CanvasHistoryEntry,
  CanvasNodeAppearanceUpdate,
  CanvasPosition,
  CanvasSize,
  CanvasNodeType,
  CanvasOperation,
  CanvasSaveState,
  CanvasToolMode,
  ConnectionDraft,
} from "#canvas-types";

export {
  CANVAS_HISTORY_GRAPH_BYTE_BUDGET,
  operationsBetween,
} from "./storeHelpers";

export interface CanvasNodeMove {
  nodeId: string;
  position: CanvasPosition;
}

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
  pendingOperationGroupSizes: number[];
  inFlightOperationCount: number;
  retryPrefixOperationCount: number;
  history: CanvasHistoryEntry[];
  future: CanvasHistoryEntry[];
  saveState: CanvasSaveState;
  saveMessage: string | null;
  hydrate: (graph: CanvasGraph, revision: number) => void;
  addNode: (
    type: CanvasNodeType,
    position: { x: number; y: number },
    overrides?: CanvasNodeCreateOverrides,
  ) => string;
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
      const pending = coalescePendingOperationGroups(
        current.pendingOperations,
        current.pendingOperationGroupSizes,
        operations,
        current.retryPrefixOperationCount,
      );
      set({
        graph: nextGraph,
        history,
        future: [],
        pendingOperations: pending.operations,
        pendingOperationGroupSizes: pending.groupSizes,
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
        )
      ) {
        return;
      }
      const updatedEdges = current.edges.map((item) =>
        item.id === edgeId ? nextEdge : item,
      );
      const nextGraph = {
        ...current,
        edges:
          typeof details.order === "number"
            ? reorderCanvasEdgeGroup(updatedEdges, edgeId, details.order)
            : updatedEdges,
      };
      const operations = operationsBetween(current, nextGraph);
      if (
        operations.length === 0 ||
        operations.length > CANVAS_AUTOSAVE_OPERATION_LIMIT ||
        !validGraphForStoreCommit(nextGraph)
      ) {
        return;
      }
      commit(nextGraph, "更新连接详情", operations);
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
      const nextGraph = normalizeEdgeOrders({
        ...current.graph,
        nodes: current.graph.nodes.filter(
          (node) => !removedNodeIds.has(node.id),
        ),
        edges: current.graph.edges.filter(
          (edge) => !removedEdgeIds.has(edge.id),
        ),
      });
      if (
        operationsBetween(nextGraph, current.graph).length >
        CANVAS_AUTOSAVE_OPERATION_LIMIT
      ) {
        set({
          saveMessage: "一次删除范围过大，请缩小选区后重试。",
        });
        return;
      }
      commit(nextGraph, label, operations);

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
      pendingOperationGroupSizes: [],
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
          pendingOperationGroupSizes: [],
          inFlightOperationCount: 0,
          retryPrefixOperationCount: 0,
          history: [],
          future: [],
          saveState: "saved",
          saveMessage: null,
        });
      },
      addNode(type, position, overrides) {
        if (!validCanvasPosition(position)) return "";
        const result = addCanvasNode(get().graph, type, position, overrides);
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
        if (!node) return;
        const fixedMode = canvasFixedVideoMode(node.type);
        const nextConfig = fixedMode
          ? { ...config, mode: fixedMode }
          : { ...config };
        if (canvasConfigEqual(node.config, nextConfig)) return;
        const videoMode =
          nextConfig.mode === "i2v" || nextConfig.mode === "reference"
            ? nextConfig.mode
            : "t2v";
        const removedEdgeIds = incompatibleVideoEdgeIds(
          current,
          nodeId,
          videoMode,
        );
        const nextGraph = {
          ...current,
          nodes: current.nodes.map((item) =>
            item.id === nodeId ? { ...item, config: nextConfig } : item,
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
              config: nextConfig,
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
        const pendingGroups = normalizePendingOperationGroupSizes(
          current.pendingOperations.length,
          current.pendingOperationGroupSizes,
        );
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
          pendingOperationGroupSizes: [...pendingGroups, operations.length],
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
        const pendingGroups = normalizePendingOperationGroupSizes(
          current.pendingOperations.length,
          current.pendingOperationGroupSizes,
        );
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
          pendingOperationGroupSizes: [...pendingGroups, operations.length],
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
          pendingOperationGroupSizes: consumePendingOperationGroups(
            current.pendingOperationGroupSizes,
            current.pendingOperations.length,
            acknowledgedCount,
          ),
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
          pendingOperationGroupSizes: [],
          inFlightOperationCount: 0,
          retryPrefixOperationCount: 0,
          saveState: "saved",
          saveMessage: null,
        });
      },
    };
  });
}
