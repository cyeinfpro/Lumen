import type {
  CanvasEdgeDefinition,
  CanvasGraph,
  CanvasNodeDefinition,
  CanvasPosition,
} from "./types";
import {
  MAX_CANVAS_COORDINATE,
  MAX_CANVAS_EDGES,
  MAX_CANVAS_GRAPH_BYTES,
  MAX_CANVAS_NODES,
  MAX_CANVAS_NODE_CONFIG_BYTES,
  canvasGraphReadyToSave,
  canvasJsonByteLength,
  normalizeCanvasGraph,
  validateCanvasConnections,
} from "#canvas-graph";

export const DEFAULT_SUBGRAPH_OFFSET: CanvasPosition = { x: 32, y: 32 };
export const CANVAS_CLIPBOARD_PREFIX = "lumen-canvas-subgraph:v1:";
export { MAX_CANVAS_EDGES, MAX_CANVAS_NODES };

const MAX_CANVAS_GROUP_DEPTH = 4;
const MAX_CANVAS_CLIPBOARD_VALUE_DEPTH = 32;

export interface CanvasSubgraph {
  schema_version: 1;
  nodes: CanvasNodeDefinition[];
  edges: CanvasEdgeDefinition[];
}

export type CanvasClipboardEntityKind = "node" | "edge";
export type CanvasClipboardIdFactory = (
  kind: CanvasClipboardEntityKind,
  sourceId: string,
  attempt: number,
) => string;

export interface InsertSubgraphOptions {
  position?: CanvasPosition;
  offset?: CanvasPosition;
  idFactory?: CanvasClipboardIdFactory;
}

export interface InsertSubgraphResult {
  graph: CanvasGraph;
  nodes: CanvasNodeDefinition[];
  edges: CanvasEdgeDefinition[];
  nodeIdMap: Record<string, string>;
  edgeIdMap: Record<string, string>;
}

export function serializeCanvasSubgraph(subgraph: CanvasSubgraph): string {
  assertValidCanvasSubgraph(subgraph);
  const serialized = JSON.stringify(subgraph);
  if (utf8ByteLength(serialized) > MAX_CANVAS_GRAPH_BYTES) {
    throw new Error("Canvas subgraph exceeds the clipboard byte limit");
  }
  return `${CANVAS_CLIPBOARD_PREFIX}${serialized}`;
}

export function parseCanvasSubgraph(value: string): CanvasSubgraph | null {
  if (!value.startsWith(CANVAS_CLIPBOARD_PREFIX)) return null;
  try {
    const serialized = value.slice(CANVAS_CLIPBOARD_PREFIX.length);
    if (
      serialized.length > MAX_CANVAS_GRAPH_BYTES ||
      utf8ByteLength(serialized) > MAX_CANVAS_GRAPH_BYTES
    ) {
      return null;
    }
    const raw = JSON.parse(serialized) as Partial<CanvasSubgraph>;
    if (
      raw.schema_version !== 1 ||
      !Array.isArray(raw.nodes) ||
      !Array.isArray(raw.edges) ||
      raw.nodes.length > MAX_CANVAS_NODES ||
      raw.edges.length > MAX_CANVAS_EDGES ||
      !jsonValueDepthIsValid(raw)
    ) {
      return null;
    }
    const normalized = normalizeCanvasGraph({
      schema_version: 1,
      nodes: raw.nodes,
      edges: raw.edges,
      frames: [],
      settings: { snap_to_grid: false, grid_size: 16 },
    });
    if (
      normalized.nodes.length !== raw.nodes.length ||
      normalized.edges.length !== raw.edges.length
    ) {
      return null;
    }
    const subgraph = {
      schema_version: 1,
      nodes: normalized.nodes,
      edges: normalized.edges,
    } satisfies CanvasSubgraph;
    assertValidCanvasSubgraph(subgraph);
    return subgraph;
  } catch {
    return null;
  }
}

export function copySubgraph(
  graph: CanvasGraph,
  nodeIds: readonly string[],
): CanvasSubgraph {
  const selectedIds = new Set(nodeIds);
  const selectedNodes = graph.nodes.filter((node) => selectedIds.has(node.id));
  const selectedGroupIds = new Set(
    selectedNodes
      .filter((node) => node.type === "frame")
      .map((node) => node.id),
  );
  const nodes = structuredClone(selectedNodes).map((node) => ({
    ...node,
    parent_group_id:
      node.parent_group_id && selectedGroupIds.has(node.parent_group_id)
        ? node.parent_group_id
        : null,
  }));
  const includedIds = new Set(nodes.map((node) => node.id));
  const edges = graph.edges.filter(
    (edge) =>
      includedIds.has(edge.source_node_id) &&
      includedIds.has(edge.target_node_id),
  );
  return {
    schema_version: 1,
    nodes,
    edges: structuredClone(edges),
  };
}

export function createCanvasSubgraph(
  graph: CanvasGraph,
  nodeIds: readonly string[],
): CanvasSubgraph {
  return copySubgraph(graph, nodeIds);
}

export function insertSubgraph(
  graph: CanvasGraph,
  subgraph: CanvasSubgraph,
  options: InsertSubgraphOptions = {},
): InsertSubgraphResult {
  const targetGroupParents = canvasGroupParents(graph);
  assertValidCanvasSubgraph(subgraph, targetGroupParents);
  if (subgraph.nodes.length === 0) {
    return emptyInsertionResult(graph);
  }

  const idFactory = options.idFactory ?? defaultClipboardIdFactory;
  const internalEdges = internalSubgraphEdges(subgraph);
  if (
    graph.nodes.length + subgraph.nodes.length > MAX_CANVAS_NODES ||
    graph.edges.length + internalEdges.length > MAX_CANVAS_EDGES
  ) {
    return emptyInsertionResult(graph);
  }
  const nodeIdMap = allocateNodeIds(graph, subgraph.nodes, idFactory);
  const edgeIdMap = allocateEdgeIds(graph, internalEdges, idFactory);
  const translation = insertionTranslation(subgraph.nodes, options);
  const nodes = subgraph.nodes.map((node) =>
    remapNode(node, nodeIdMap, targetGroupParents, translation),
  );
  const edges = internalEdges.map((edge) =>
    remapEdge(edge, nodeIdMap, edgeIdMap),
  );
  const nextGraph = {
    ...graph,
    nodes: [...graph.nodes, ...nodes],
    edges: [...graph.edges, ...edges],
  };
  if (!canvasGraphReadyToSave(nextGraph)) {
    return emptyInsertionResult(graph);
  }

  return {
    graph: nextGraph,
    nodes,
    edges,
    nodeIdMap: Object.fromEntries(nodeIdMap),
    edgeIdMap: Object.fromEntries(edgeIdMap),
  };
}

function emptyInsertionResult(graph: CanvasGraph): InsertSubgraphResult {
  return {
    graph,
    nodes: [],
    edges: [],
    nodeIdMap: {},
    edgeIdMap: {},
  };
}

function assertValidCanvasSubgraph(
  subgraph: CanvasSubgraph,
  externalGroupParents: ReadonlyMap<string, string | null> = new Map(),
): void {
  if (
    subgraph.schema_version !== 1 ||
    subgraph.nodes.length > MAX_CANVAS_NODES ||
    subgraph.edges.length > MAX_CANVAS_EDGES ||
    !jsonValueDepthIsValid(subgraph)
  ) {
    throw new Error("Canvas subgraph is invalid");
  }
  const subgraphBytes = canvasJsonByteLength(subgraph);
  if (subgraphBytes === null || subgraphBytes > MAX_CANVAS_GRAPH_BYTES) {
    throw new Error("Canvas subgraph exceeds the clipboard byte limit");
  }
  for (const node of subgraph.nodes) {
    assertValidClipboardNode(node);
  }
  for (const edge of subgraph.edges) {
    assertValidClipboardEdge(edge);
  }
  const nodeIds = subgraph.nodes.map((node) => node.id);
  const edgeIds = subgraph.edges.map((edge) => edge.id);
  if (new Set(nodeIds).size !== nodeIds.length) {
    throw new Error("Canvas subgraph contains duplicate node IDs");
  }
  if (new Set(edgeIds).size !== edgeIds.length) {
    throw new Error("Canvas subgraph contains duplicate edge IDs");
  }
  assertValidParents(subgraph.nodes, externalGroupParents);
  const edgeValidation = validateCanvasConnections(
    {
      schema_version: 1,
      nodes: subgraph.nodes,
      edges: [],
      frames: [],
      settings: { snap_to_grid: false, grid_size: 16 },
    },
    subgraph.edges,
  );
  if (!edgeValidation.valid) {
    throw new Error(
      `Canvas subgraph edge ${edgeValidation.edgeId} is invalid: ${edgeValidation.reason}`,
    );
  }
}

function internalSubgraphEdges(
  subgraph: CanvasSubgraph,
): CanvasEdgeDefinition[] {
  const nodeIds = new Set(subgraph.nodes.map((node) => node.id));
  return subgraph.edges.filter(
    (edge) =>
      nodeIds.has(edge.source_node_id) &&
      nodeIds.has(edge.target_node_id),
  );
}

function allocateNodeIds(
  graph: CanvasGraph,
  nodes: CanvasNodeDefinition[],
  idFactory: CanvasClipboardIdFactory,
): Map<string, string> {
  const usedIds = new Set([
    ...graph.nodes.map((node) => node.id),
    ...frameIds(graph.frames),
  ]);
  return new Map(
    nodes.map((node) => [
      node.id,
      allocateId("node", node.id, usedIds, idFactory),
    ]),
  );
}

function allocateEdgeIds(
  graph: CanvasGraph,
  edges: CanvasEdgeDefinition[],
  idFactory: CanvasClipboardIdFactory,
): Map<string, string> {
  const usedIds = new Set(graph.edges.map((edge) => edge.id));
  return new Map(
    edges.map((edge) => [
      edge.id,
      allocateId("edge", edge.id, usedIds, idFactory),
    ]),
  );
}

function allocateId(
  kind: CanvasClipboardEntityKind,
  sourceId: string,
  usedIds: Set<string>,
  idFactory: CanvasClipboardIdFactory,
): string {
  for (let attempt = 0; attempt < 1_000; attempt += 1) {
    const candidate = idFactory(kind, sourceId, attempt);
    if (!ENTITY_ID_PATTERN.test(candidate)) {
      throw new Error(`Clipboard ID factory returned invalid ID: ${candidate}`);
    }
    if (usedIds.has(candidate)) continue;
    usedIds.add(candidate);
    return candidate;
  }
  throw new Error(`Unable to allocate a unique ${kind} ID for ${sourceId}`);
}

function insertionTranslation(
  nodes: CanvasNodeDefinition[],
  options: InsertSubgraphOptions,
): CanvasPosition {
  if (options.position) {
    assertValidPosition(options.position, "Clipboard insertion position");
  }
  if (options.offset) {
    assertValidPosition(options.offset, "Clipboard insertion offset");
  }
  if (!options.position) {
    return options.offset ?? DEFAULT_SUBGRAPH_OFFSET;
  }
  const bounds = nodes.reduce(
    (current, node) => ({
      x: Math.min(current.x, node.position.x),
      y: Math.min(current.y, node.position.y),
    }),
    { x: Number.POSITIVE_INFINITY, y: Number.POSITIVE_INFINITY },
  );
  return {
    x: options.position.x - bounds.x,
    y: options.position.y - bounds.y,
  };
}

function remapNode(
  node: CanvasNodeDefinition,
  nodeIdMap: ReadonlyMap<string, string>,
  targetGroupParents: ReadonlyMap<string, string | null>,
  translation: CanvasPosition,
): CanvasNodeDefinition {
  const parentId = node.parent_group_id ?? null;
  const remapped = {
    ...structuredClone(node),
    id: requireMappedId(nodeIdMap, node.id),
    position: {
      x: node.position.x + translation.x,
      y: node.position.y + translation.y,
    },
    parent_group_id:
      (parentId && nodeIdMap.get(parentId)) ||
      (parentId && targetGroupParents.has(parentId) ? parentId : null),
    config: remapNodeConfig(node, nodeIdMap),
  };
  assertValidPosition(remapped.position, `Node ${node.id} position`);
  return remapped;
}

function remapNodeConfig(
  node: CanvasNodeDefinition,
  nodeIdMap: ReadonlyMap<string, string>,
): Record<string, unknown> {
  const config = structuredClone(node.config);
  if (
    node.type === "delivery" &&
    typeof config.thumbnail_source_node_id === "string"
  ) {
    config.thumbnail_source_node_id =
      nodeIdMap.get(config.thumbnail_source_node_id) ?? null;
  }
  return config;
}

function remapEdge(
  edge: CanvasEdgeDefinition,
  nodeIdMap: ReadonlyMap<string, string>,
  edgeIdMap: ReadonlyMap<string, string>,
): CanvasEdgeDefinition {
  const pinned = edge.binding_mode === "pinned";
  return {
    ...structuredClone(edge),
    id: requireMappedId(edgeIdMap, edge.id),
    source_node_id: requireMappedId(nodeIdMap, edge.source_node_id),
    target_node_id: requireMappedId(nodeIdMap, edge.target_node_id),
    binding_mode: pinned ? "follow_active" : edge.binding_mode,
    pinned_execution_id: null,
    pinned_output_index: null,
  };
}

function requireMappedId(
  idMap: ReadonlyMap<string, string>,
  sourceId: string,
): string {
  const mapped = idMap.get(sourceId);
  if (!mapped) throw new Error(`Missing clipboard ID mapping for ${sourceId}`);
  return mapped;
}

function canvasGroupParents(
  graph: CanvasGraph,
): Map<string, string | null> {
  return new Map([
    ...graph.nodes
      .filter((node) => node.type === "frame")
      .map(
        (node) =>
          [node.id, node.parent_group_id ?? null] as const,
      ),
    ...frameParentEntries(graph.frames),
  ]);
}

function frameIds(frames: unknown[]): string[] {
  return frames.flatMap((frame) => {
    if (!frame || typeof frame !== "object") return [];
    const id = (frame as { id?: unknown }).id;
    return typeof id === "string" ? [id] : [];
  });
}

function frameParentEntries(
  frames: unknown[],
): Array<readonly [string, string | null]> {
  return frames.flatMap((frame) => {
    if (!frame || typeof frame !== "object") return [];
    const value = frame as { id?: unknown; parent_frame_id?: unknown };
    if (typeof value.id !== "string") return [];
    return [
      [
        value.id,
        typeof value.parent_frame_id === "string"
          ? value.parent_frame_id
          : null,
      ] as const,
    ];
  });
}

function assertValidClipboardNode(node: CanvasNodeDefinition): void {
  if (!clipboardNodeShapeIsValid(node)) {
    throw new Error(`Canvas clipboard node ${node.id} is invalid`);
  }
  assertValidPosition(node.position, `Node ${node.id} position`);
  if (!clipboardNodeSizeIsValid(node.size)) {
    throw new Error(`Canvas clipboard node ${node.id} has invalid size`);
  }
  if (!clipboardParentIdIsValid(node.parent_group_id)) {
    throw new Error(`Canvas clipboard node ${node.id} has invalid parent ID`);
  }
  const configBytes = canvasJsonByteLength(node.config);
  if (!clipboardConfigSizeIsValid(configBytes)) {
    throw new Error(`Canvas clipboard node ${node.id} config is too large`);
  }
}

function clipboardNodeShapeIsValid(node: CanvasNodeDefinition): boolean {
  return (
    ENTITY_ID_PATTERN.test(node.id) &&
    node.schema_version === 1 &&
    typeof node.title === "string" &&
    Boolean(node.config) &&
    typeof node.config === "object" &&
    !Array.isArray(node.config)
  );
}

function clipboardNodeSizeIsValid(
  size: CanvasNodeDefinition["size"],
): boolean {
  if (!size) return true;
  return (
    Number.isFinite(size.width) &&
    size.width >= 40 &&
    size.width <= 10_000 &&
    Number.isFinite(size.height) &&
    size.height >= 40 &&
    size.height <= 10_000
  );
}

function clipboardParentIdIsValid(
  parentId: CanvasNodeDefinition["parent_group_id"],
): boolean {
  return (
    parentId === null ||
    parentId === undefined ||
    ENTITY_ID_PATTERN.test(parentId)
  );
}

function clipboardConfigSizeIsValid(configBytes: number | null): boolean {
  return (
    configBytes !== null &&
    configBytes <= MAX_CANVAS_NODE_CONFIG_BYTES
  );
}

function assertValidClipboardEdge(edge: CanvasEdgeDefinition): void {
  if (
    !ENTITY_ID_PATTERN.test(edge.id) ||
    !ENTITY_ID_PATTERN.test(edge.source_node_id) ||
    !ENTITY_ID_PATTERN.test(edge.target_node_id) ||
    !HANDLE_PATTERN.test(edge.source_handle) ||
    !HANDLE_PATTERN.test(edge.target_handle) ||
    !CLIPBOARD_DATA_TYPES.has(edge.data_type) ||
    !CLIPBOARD_BINDING_MODES.has(edge.binding_mode)
  ) {
    throw new Error(`Canvas clipboard edge ${edge.id} is invalid`);
  }
}

function assertValidParents(
  nodes: readonly CanvasNodeDefinition[],
  externalGroupParents: ReadonlyMap<string, string | null>,
): void {
  const nodesById = new Map(nodes.map((node) => [node.id, node]));
  const groupParents = collectGroupParents(nodes, externalGroupParents);
  assertKnownNodeParents(nodes, nodesById, externalGroupParents);
  assertValidGroupParentChains(groupParents);
}

function collectGroupParents(
  nodes: readonly CanvasNodeDefinition[],
  externalGroupParents: ReadonlyMap<string, string | null>,
): Map<string, string | null> {
  const groupParents = new Map(externalGroupParents);
  for (const node of nodes) {
    if (node.type === "frame") {
      groupParents.set(node.id, node.parent_group_id ?? null);
    }
  }
  return groupParents;
}

function assertKnownNodeParents(
  nodes: readonly CanvasNodeDefinition[],
  nodesById: ReadonlyMap<string, CanvasNodeDefinition>,
  externalGroupParents: ReadonlyMap<string, string | null>,
): void {
  for (const node of nodes) {
    const parentId = node.parent_group_id ?? null;
    if (!parentId) continue;
    if (!clipboardParentIsKnown(parentId, nodesById, externalGroupParents)) {
      throw new Error(`Canvas clipboard node ${node.id} has unknown parent`);
    }
  }
}

function clipboardParentIsKnown(
  parentId: string,
  nodesById: ReadonlyMap<string, CanvasNodeDefinition>,
  externalGroupParents: ReadonlyMap<string, string | null>,
): boolean {
  const internalParent = nodesById.get(parentId);
  return internalParent
    ? internalParent.type === "frame"
    : externalGroupParents.has(parentId);
}

function assertValidGroupParentChains(
  groupParents: ReadonlyMap<string, string | null>,
): void {
  for (const groupId of groupParents.keys()) {
    assertValidGroupParentChain(groupId, groupParents);
  }
}

function assertValidGroupParentChain(
  groupId: string,
  groupParents: ReadonlyMap<string, string | null>,
): void {
  const seen = new Set<string>();
  let current: string | null = groupId;
  let depth = 0;
  while (current !== null) {
    if (seen.has(current)) {
      throw new Error("Canvas clipboard group nesting contains a cycle");
    }
    seen.add(current);
    const parent: string | null = groupParents.get(current) ?? null;
    if (parent !== null) {
      if (!groupParents.has(parent)) {
        throw new Error(`Canvas clipboard group ${current} has unknown parent`);
      }
      depth += 1;
      if (depth > MAX_CANVAS_GROUP_DEPTH) {
        throw new Error("Canvas clipboard group nesting is too deep");
      }
    }
    current = parent;
  }
}

function assertValidPosition(
  position: CanvasPosition,
  label: string,
): void {
  if (
    !Number.isFinite(position.x) ||
    Math.abs(position.x) > MAX_CANVAS_COORDINATE ||
    !Number.isFinite(position.y) ||
    Math.abs(position.y) > MAX_CANVAS_COORDINATE
  ) {
    throw new Error(`${label} is outside canvas bounds`);
  }
}

function jsonValueDepthIsValid(value: unknown): boolean {
  const seen = new WeakSet<object>();
  const pending: Array<{ value: unknown; depth: number }> = [
    { value, depth: 0 },
  ];
  while (pending.length > 0) {
    const current = pending.pop();
    if (!current || !current.value || typeof current.value !== "object") {
      continue;
    }
    if (current.depth > MAX_CANVAS_CLIPBOARD_VALUE_DEPTH) return false;
    if (seen.has(current.value)) return false;
    seen.add(current.value);
    for (const child of Object.values(current.value)) {
      pending.push({ value: child, depth: current.depth + 1 });
    }
  }
  return true;
}

function utf8ByteLength(value: string): number {
  return new TextEncoder().encode(value).byteLength;
}

const ENTITY_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$/;
const HANDLE_PATTERN = /^[A-Za-z][A-Za-z0-9_.:-]{0,47}$/;
const CLIPBOARD_DATA_TYPES = new Set(["text", "image", "video", "mask"]);
const CLIPBOARD_BINDING_MODES = new Set(["follow_active", "pinned"]);
let fallbackClipboardId = 0;

function defaultClipboardIdFactory(
  kind: CanvasClipboardEntityKind,
): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return `${kind}-${crypto.randomUUID()}`;
  }
  fallbackClipboardId += 1;
  return `${kind}-${Date.now().toString(36)}-${fallbackClipboardId.toString(36)}`;
}
