import type { CanvasNodeExecution, CanvasRun } from "./types";

export interface CanvasPlanInput {
  document_revision: number;
  kind: "selection" | "upstream" | "all";
  target_node_ids: string[];
  reuse_outputs: Record<string, { execution_id: string; output_index: number }>;
  output_indices: Record<string, number>;
  budget_micro: number;
  failure_policy: "continue_independent" | "fail_fast";
  auto_select_on_success: boolean;
}
export interface CanvasPlanReference {
  node_id: string;
  execution_id: string;
  output_index: number;
  asset_kind: "image" | "video";
  asset_id: string;
  source_sha256: string;
}
export interface CanvasPlanStep {
  node_id: string;
  dependencies: string[];
  reuse: CanvasPlanReference | null;
  estimated_cost_micro: number | null;
  effective_model: string | null;
  capability_version: string | null;
  output_index: number;
}
export interface CanvasRunPlan {
  schema_version: number;
  canvas_id: string;
  document_revision: number;
  kind: string;
  target_node_ids: string[];
  graph_hash: string;
  steps: CanvasPlanStep[];
  bindings: Array<[string, CanvasPlanReference]>;
  failure_policy: string;
  budget_micro: number;
  plan_hash: string;
}
export interface CanvasPlanPreview {
  plan: CanvasRunPlan;
  estimated_cost_micro: number | null;
  budget_semantics: "admission_estimate_not_settlement_cap";
}
export interface CanvasPlanRunDetail extends CanvasRun {
  canvas_id: string;
  kind: string;
  executions: Array<CanvasNodeExecution & { attempt: number }>;
  summary: { run_plan?: CanvasRunPlan; document_revision?: number };
}
export type CanvasPlanIntent =
  | { kind: "start"; body: CanvasPlanInput & { plan_hash: string } }
  | { kind: "repair"; run_id: string; body: { execution_ids: string[]; additional_budget_micro: number } };
export interface CanvasPendingPlanIntent {
  key: string;
  canvasId: string;
  intent: CanvasPlanIntent;
}
export interface CanvasPlanReceipt { admitted: boolean; run: CanvasPlanRunDetail | null }
