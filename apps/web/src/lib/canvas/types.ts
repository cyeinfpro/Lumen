export type CanvasNodeType =
  | "prompt"
  | "prompt_merge"
  | "image_asset"
  | "mask_asset"
  | "video_asset"
  | "image_generate"
  | "image_edit"
  | "image_inpaint"
  | "image_upscale"
  | "video_generate"
  | "video_text_generate"
  | "video_image_generate"
  | "video_reference_generate"
  | "note"
  | "frame"
  | "delivery";

export type CanvasDataType = "text" | "image" | "video" | "mask";
export type CanvasBindingMode = "follow_active" | "pinned";
export type CanvasEdgeRole =
  | "reference"
  | "subject"
  | "product"
  | "style"
  | "edit_target"
  | "background"
  | "other";
export type CanvasToolMode = "hand" | "select" | "connect";
export type CanvasSaveState = "idle" | "dirty" | "saving" | "saved" | "conflict" | "error";
export type CanvasExecutionStatus =
  | "pending"
  | "ready"
  | "queued"
  | "running"
  | "reconciling"
  | "canceling"
  | "succeeded"
  | "partial_failed"
  | "failed"
  | "blocked"
  | "canceled"
  | "skipped"
  | "reused";

export type CanvasPosition = { x: number; y: number };
export type CanvasSize = { width: number; height: number };

export interface CanvasNodeUI {
  collapsed?: boolean;
  color_tag?: string | null;
  preset_id?: string | null;
}

export interface CanvasDocumentSettings {
  snap_to_grid: boolean;
  grid_size: number;
}

export interface CanvasNodeDefinition {
  id: string;
  type: CanvasNodeType;
  schema_version: number;
  title: string;
  position: CanvasPosition;
  size?: CanvasSize | null;
  parent_group_id?: string | null;
  config: Record<string, unknown>;
  ui: CanvasNodeUI;
}

export interface CanvasEdgeDefinition {
  id: string;
  source_node_id: string;
  source_handle: string;
  target_node_id: string;
  target_handle: string;
  data_type: CanvasDataType;
  binding_mode: CanvasBindingMode;
  pinned_execution_id?: string | null;
  pinned_output_index?: number | null;
  role?: CanvasEdgeRole | null;
  order?: number | null;
}

export type CanvasNodeAppearanceUpdate = Partial<
  Pick<CanvasNodeDefinition, "title" | "parent_group_id" | "ui">
>;

export type CanvasEdgeDetailsUpdate = Partial<
  Pick<
    CanvasEdgeDefinition,
    | "binding_mode"
    | "pinned_execution_id"
    | "pinned_output_index"
    | "role"
    | "order"
  >
>;

export interface CanvasGraph {
  schema_version: 1;
  nodes: CanvasNodeDefinition[];
  edges: CanvasEdgeDefinition[];
  frames: unknown[];
  settings: CanvasDocumentSettings;
}

export interface CanvasOutput {
  type: "image" | "video";
  image_id?: string | null;
  video_id?: string | null;
  url?: string | null;
  preview_url?: string | null;
  poster_url?: string | null;
  width?: number | null;
  height?: number | null;
  label?: string | null;
  generation_id?: string | null;
  video_generation_id?: string | null;
}

export interface CanvasExecutionTaskDetail {
  id: string;
  kind: "generation" | "completion" | "video_generation" | string;
  status: string;
  progress_stage: string;
  progress_pct?: number | null;
  generation_id?: string | null;
  completion_id?: string | null;
  video_generation_id?: string | null;
  model?: string | null;
  provider_name?: string | null;
  provider_kind?: string | null;
  action?: string | null;
  duration_s?: number | null;
  resolution?: string | null;
  aspect_ratio?: string | null;
  size_requested?: string | null;
  generate_audio?: boolean | null;
  attempt?: number | null;
  elapsed_ms?: number | null;
  error_code?: string | null;
  error_message?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  started_at?: string | null;
  submit_started_at?: string | null;
  submitted_at?: string | null;
  finished_at?: string | null;
}

export interface CanvasNodeExecution {
  id: string;
  run_id?: string | null;
  node_id: string;
  node_type: CanvasNodeType | string;
  status: CanvasExecutionStatus;
  outputs: CanvasOutput[];
  error_code?: string | null;
  error_message?: string | null;
  tasks?: CanvasExecutionTaskDetail[];
  created_at?: string | null;
  updated_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
}

export interface CanvasNodeSelection {
  node_id: string;
  execution_id: string | null;
  output_index: number;
  revision?: number;
  locked?: boolean;
}

export interface CanvasRun {
  id: string;
  status: CanvasExecutionStatus | "planning" | "paused";
  target_node_ids?: string[];
  last_event_seq?: number;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface CanvasDocument {
  id: string;
  title: string;
  description?: string | null;
  revision: number;
  graph_schema_version?: number;
  graph: CanvasGraph;
  thumbnail_image_id?: string | null;
  thumbnail_url?: string | null;
  created_at: string;
  updated_at: string;
  selections: CanvasNodeSelection[];
  recent_executions: CanvasNodeExecution[];
  active_runs: CanvasRun[];
}

export interface CanvasListItem {
  id: string;
  title: string;
  description?: string | null;
  revision: number;
  node_count: number;
  edge_count: number;
  image_output_count: number;
  video_output_count: number;
  running_count: number;
  thumbnail_image_id?: string | null;
  thumbnail_url?: string | null;
  has_conflict?: boolean;
  has_failure?: boolean;
  created_at: string;
  updated_at: string;
}

export interface CanvasListResponse {
  items: CanvasListItem[];
  next_cursor?: string | null;
}

export type CanvasOperation =
  | { op: "add_node"; operation_schema_version: 1; node: CanvasNodeDefinition }
  | {
      op: "update_node_config";
      operation_schema_version: 1;
      node_id: string;
      config: Record<string, unknown>;
    }
  | {
      op: "update_node_meta";
      operation_schema_version: 1;
      node_id: string;
      title?: string;
      parent_group_id?: string | null;
      ui?: CanvasNodeUI;
    }
  | {
      op: "move_nodes";
      operation_schema_version: 1;
      items: Array<{ node_id: string; x: number; y: number }>;
    }
  | {
      op: "resize_node";
      operation_schema_version: 1;
      node_id: string;
      size: CanvasSize;
    }
  | {
      op: "remove_nodes";
      operation_schema_version: 1;
      node_ids: string[];
      edge_ids: string[];
    }
  | { op: "add_edge"; operation_schema_version: 1; edge: CanvasEdgeDefinition }
  | {
      op: "update_edge";
      operation_schema_version: 1;
      edge_id: string;
      binding_mode?: CanvasBindingMode;
      pinned_execution_id?: string | null;
      pinned_output_index?: number | null;
      role?: CanvasEdgeRole | null;
      order?: number | null;
    }
  | {
      op: "remove_edges";
      operation_schema_version: 1;
      edge_ids: string[];
    }
  | {
      op: "update_document_settings";
      operation_schema_version: 1;
      settings: CanvasDocumentSettings;
    };

export interface CanvasHistoryEntry {
  graph: CanvasGraph;
  label: string;
}

export interface ConnectionDraft {
  sourceNodeId: string;
  sourceHandle: string;
  dataType: CanvasDataType;
}
