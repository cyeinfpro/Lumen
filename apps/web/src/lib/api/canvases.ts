import { apiFetch, apiFetchNoContent } from "./http";
import { normalizeCanvasGraph } from "../canvas/graph";
import type {
  CanvasDocument,
  CanvasExecutionTaskDetail,
  CanvasGraph,
  CanvasListItem,
  CanvasListResponse,
  CanvasNodeExecution,
  CanvasNodeSelection,
  CanvasOperation,
  CanvasRun,
} from "../canvas/types";

type UnknownRecord = Record<string, unknown>;

export interface ListCanvasesOptions {
  cursor?: string;
  limit?: number;
  q?: string;
}

export interface CreateCanvasInput {
  title: string;
  description?: string;
  template?: string;
  graph?: CanvasGraph;
}

export interface ApplyCanvasMutationsInput {
  base_revision: number;
  client_id: string;
  mutation_id: string;
  operations: CanvasOperation[];
}

export interface ApplyCanvasMutationsOutput {
  revision: number;
  updated_at?: string;
}

export function listCanvases(
  options: ListCanvasesOptions = {},
): Promise<CanvasListResponse> {
  const query = new URLSearchParams();
  if (options.cursor) query.set("cursor", options.cursor);
  if (options.limit) query.set("limit", String(options.limit));
  if (options.q) query.set("q", options.q);
  const suffix = query.size > 0 ? `?${query.toString()}` : "";
  return apiFetch<unknown>(`/canvases${suffix}`).then(normalizeCanvasList);
}

export function createCanvas(input: CreateCanvasInput): Promise<CanvasDocument> {
  return apiFetch<unknown>("/canvases", {
    method: "POST",
    body: JSON.stringify(input),
  }).then(normalizeCanvasDocument);
}

export function getCanvas(canvasId: string): Promise<CanvasDocument> {
  return apiFetch<unknown>(`/canvases/${encodeURIComponent(canvasId)}`).then(
    normalizeCanvasDocument,
  );
}

export function patchCanvas(
  canvasId: string,
  input: { title?: string; description?: string },
): Promise<CanvasDocument> {
  return apiFetch<unknown>(`/canvases/${encodeURIComponent(canvasId)}`, {
    method: "PATCH",
    body: JSON.stringify(input),
  }).then(normalizeCanvasDocument);
}

export function deleteCanvas(canvasId: string): Promise<void> {
  return apiFetchNoContent(`/canvases/${encodeURIComponent(canvasId)}`, {
    method: "DELETE",
  });
}

export function duplicateCanvas(canvasId: string): Promise<CanvasDocument> {
  return apiFetch<unknown>(
    `/canvases/${encodeURIComponent(canvasId)}/duplicate`,
    { method: "POST" },
  ).then(normalizeCanvasDocument);
}

export function applyCanvasMutations(
  canvasId: string,
  input: ApplyCanvasMutationsInput,
): Promise<ApplyCanvasMutationsOutput> {
  return apiFetch<ApplyCanvasMutationsOutput>(
    `/canvases/${encodeURIComponent(canvasId)}/mutations`,
    {
      method: "POST",
      headers: { "Idempotency-Key": input.mutation_id },
      body: JSON.stringify(input),
    },
  );
}

export function executeCanvasNode(
  canvasId: string,
  nodeId: string,
  documentRevision: number,
): Promise<{ run?: CanvasRun; execution?: CanvasNodeExecution }> {
  const idempotencyKey = randomId();
  return apiFetch(
    `/canvases/${encodeURIComponent(canvasId)}/nodes/${encodeURIComponent(nodeId)}/execute`,
    {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify({
        document_revision: documentRevision,
        idempotency_key: idempotencyKey,
        auto_select_on_success: true,
      }),
    },
  );
}

export function selectCanvasExecutionOutput(
  canvasId: string,
  executionId: string,
  outputIndex: number,
  selectionRevision?: number,
): Promise<CanvasNodeSelection> {
  return apiFetch(
    `/canvases/${encodeURIComponent(canvasId)}/executions/${encodeURIComponent(executionId)}/select`,
    {
      method: "POST",
      body: JSON.stringify({
        output_index: outputIndex,
        selection_revision: selectionRevision,
      }),
    },
  );
}

function normalizeCanvasList(value: unknown): CanvasListResponse {
  const raw = asRecord(value);
  const itemsValue = Array.isArray(raw.items)
    ? raw.items
    : Array.isArray(value)
      ? value
      : [];
  return {
    items: itemsValue.map(normalizeCanvasListItem),
    next_cursor: text(raw.next_cursor),
  };
}

function normalizeCanvasListItem(value: unknown): CanvasListItem {
  const raw = asRecord(value);
  const graph = normalizeCanvasGraph(raw.graph ?? raw.graph_json ?? raw.graph_jsonb);
  const outputs = array(raw.recent_executions).flatMap((entry) =>
    normalizeExecution(entry).outputs,
  );
  return {
    id: text(raw.id) ?? "",
    title: text(raw.title) ?? "未命名画布",
    description: text(raw.description),
    revision: number(raw.revision, 1),
    node_count: number(raw.node_count, graph.nodes.length),
    edge_count: number(raw.edge_count, graph.edges.length),
    image_output_count: number(
      raw.image_output_count,
      outputs.filter((output) => output.type === "image").length,
    ),
    video_output_count: number(
      raw.video_output_count,
      outputs.filter((output) => output.type === "video").length,
    ),
    running_count: number(raw.running_count, 0),
    thumbnail_image_id: text(raw.thumbnail_image_id),
    thumbnail_url: text(raw.thumbnail_url),
    has_conflict: raw.has_conflict === true,
    has_failure: raw.has_failure === true,
    created_at: text(raw.created_at) ?? new Date(0).toISOString(),
    updated_at: text(raw.updated_at) ?? new Date(0).toISOString(),
  };
}

function normalizeCanvasDocument(value: unknown): CanvasDocument {
  const raw = asRecord(value);
  const graphValue =
    raw.graph ?? raw.graph_json ?? raw.graph_jsonb ?? asRecord(raw.document).graph;
  const projections = asRecord(raw.projections);
  const selections = array(raw.selections ?? projections.selections).map(
    normalizeSelection,
  );
  const executions = array(
    raw.recent_executions ?? raw.executions ?? projections.recent_executions,
  ).map(normalizeExecution);
  const activeRuns = array(raw.active_runs ?? raw.runs ?? projections.active_runs).map(
    normalizeRun,
  );
  return {
    id: text(raw.id) ?? "",
    title: text(raw.title) ?? "未命名画布",
    description: text(raw.description),
    revision: number(raw.revision, 1),
    graph_schema_version: number(raw.graph_schema_version, 1),
    graph: normalizeCanvasGraph(graphValue),
    thumbnail_image_id: text(raw.thumbnail_image_id),
    thumbnail_url: text(raw.thumbnail_url),
    created_at: text(raw.created_at) ?? new Date(0).toISOString(),
    updated_at: text(raw.updated_at) ?? new Date(0).toISOString(),
    selections,
    recent_executions: executions,
    active_runs: activeRuns,
  };
}

function normalizeExecution(value: unknown): CanvasNodeExecution {
  const raw = asRecord(value);
  return {
    id: text(raw.id) ?? "",
    run_id: text(raw.run_id),
    node_id: text(raw.node_id) ?? "",
    node_type: text(raw.node_type) ?? "unknown",
    status: (text(raw.status) ?? "pending") as CanvasNodeExecution["status"],
    outputs: array(raw.outputs ?? raw.outputs_jsonb).map((output) => {
      const item = asRecord(output);
      const imageId = text(item.image_id);
      const videoId = text(item.video_id);
      return {
        type: (text(item.type) ?? (videoId ? "video" : "image")) as "image" | "video",
        image_id: imageId,
        video_id: videoId,
        url: text(item.url),
        preview_url: text(item.preview_url),
        poster_url: text(item.poster_url),
        width: optionalNumber(item.width),
        height: optionalNumber(item.height),
        label: text(item.label),
        generation_id: text(item.generation_id),
        video_generation_id: text(item.video_generation_id),
      };
    }),
    error_code: text(raw.error_code),
    error_message: text(raw.error_message),
    tasks: array(raw.tasks).map(normalizeExecutionTask),
    created_at: text(raw.created_at),
    updated_at: text(raw.updated_at),
    started_at: text(raw.started_at),
    finished_at: text(raw.finished_at),
  };
}

function normalizeExecutionTask(value: unknown): CanvasExecutionTaskDetail {
  const raw = asRecord(value);
  return {
    id: text(raw.id) ?? "",
    kind: text(raw.kind) ?? "generation",
    status: text(raw.status) ?? "queued",
    progress_stage: text(raw.progress_stage) ?? text(raw.status) ?? "queued",
    progress_pct: optionalNumber(raw.progress_pct),
    generation_id: text(raw.generation_id),
    completion_id: text(raw.completion_id),
    video_generation_id: text(raw.video_generation_id),
    model: text(raw.model),
    provider_name: text(raw.provider_name),
    provider_kind: text(raw.provider_kind),
    action: text(raw.action),
    duration_s: optionalNumber(raw.duration_s),
    resolution: text(raw.resolution),
    aspect_ratio: text(raw.aspect_ratio),
    size_requested: text(raw.size_requested),
    generate_audio:
      typeof raw.generate_audio === "boolean" ? raw.generate_audio : null,
    attempt: optionalNumber(raw.attempt),
    elapsed_ms: optionalNumber(raw.elapsed_ms),
    error_code: text(raw.error_code),
    error_message: text(raw.error_message),
    created_at: text(raw.created_at),
    updated_at: text(raw.updated_at),
    started_at: text(raw.started_at),
    submit_started_at: text(raw.submit_started_at),
    submitted_at: text(raw.submitted_at),
    finished_at: text(raw.finished_at),
  };
}

function normalizeSelection(value: unknown): CanvasNodeSelection {
  const raw = asRecord(value);
  return {
    node_id: text(raw.node_id) ?? "",
    execution_id: text(raw.execution_id),
    output_index: number(raw.output_index, 0),
    revision: optionalNumber(raw.revision) ?? undefined,
    locked: raw.locked === true,
  };
}

function normalizeRun(value: unknown): CanvasRun {
  const raw = asRecord(value);
  return {
    id: text(raw.id) ?? "",
    status: (text(raw.status) ?? "queued") as CanvasRun["status"],
    target_node_ids: array(raw.target_node_ids).map((item) => String(item)),
    last_event_seq: optionalNumber(raw.last_event_seq) ?? undefined,
    created_at: text(raw.created_at),
    updated_at: text(raw.updated_at),
  };
}

function asRecord(value: unknown): UnknownRecord {
  return value && typeof value === "object" ? (value as UnknownRecord) : {};
}

function array(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function text(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function number(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function optionalNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function randomId(): string {
  return typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : `canvas-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}
