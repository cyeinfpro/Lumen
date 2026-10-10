import { isCanvasExecutableNodeType } from "@/lib/canvas/registry";
import type { CanvasGraph } from "@/lib/canvas/types";
import type { CanvasPlanInput } from "@/lib/canvas/runPlanTypes";
import { parseCanvasPlanBudget } from "@/lib/canvas/runPlanValidation";

export interface CanvasPlanForm {
  kind: CanvasPlanInput["kind"];
  budget: string;
  failurePolicy: CanvasPlanInput["failure_policy"];
  outputIndices: Record<string, number>;
  reuseOutputs: CanvasPlanInput["reuse_outputs"];
  autoSelect: boolean;
}
export const initialCanvasPlanForm: CanvasPlanForm = {
  kind: "selection", budget: "", failurePolicy: "continue_independent",
  outputIndices: {}, reuseOutputs: {}, autoSelect: true,
};
export function canvasPlanCandidateNodes(graph: CanvasGraph, selected: string[], kind: CanvasPlanInput["kind"], reuse: CanvasPlanInput["reuse_outputs"] = {}) {
  const included = new Set(kind === "all" ? graph.nodes.map(node => node.id) : selected);
  if (kind === "upstream") {
    const incoming = new Map<string, string[]>();
    for (const edge of graph.edges) {
      if (edge.binding_mode === "pinned") continue;
      incoming.set(edge.target_node_id, [...(incoming.get(edge.target_node_id) ?? []), edge.source_node_id]);
    }
    const pending = [...included];
    while (pending.length) {
      const target = pending.pop()!;
      if (reuse[target]) continue;
      for (const source of incoming.get(target) ?? []) {
        if (!included.has(source)) { included.add(source); pending.push(source); }
      }
    }
  }
  const visible = canvasRunnableIds(graph);
  return graph.nodes.filter(node => included.has(node.id) && visible.has(node.id));
}
export function canvasPlanFormInput(form: CanvasPlanForm, graph: CanvasGraph, selected: string[], revision: number): CanvasPlanInput {
  const candidates = new Set(canvasPlanCandidateNodes(graph, selected, form.kind, form.reuseOutputs).map(node => node.id));
  const executableTargets = graph.nodes.filter(node => selected.includes(node.id) && isCanvasExecutableNodeType(node.type)).map(node => node.id).sort();
  return {
    document_revision: revision, kind: form.kind,
    target_node_ids: form.kind === "all" ? [] : executableTargets,
    budget_micro: parseCanvasPlanBudget(form.budget), failure_policy: form.failurePolicy,
    auto_select_on_success: form.autoSelect,
    output_indices: Object.fromEntries(Object.entries(form.outputIndices).filter(([id]) => candidates.has(id) && !form.reuseOutputs[id])),
    reuse_outputs: Object.fromEntries(Object.entries(form.reuseOutputs).filter(([id]) => candidates.has(id))),
  };
}

// Batch scopes include hidden frames and all saved layout/graph revisions.
export function canvasPlanGraphKey(graph: CanvasGraph): string { return JSON.stringify(graph); }

function canvasRunnableIds(graph: CanvasGraph) {
  const parents = new Map(graph.nodes.map(node => [node.id, node.parent_group_id]));
  const hidden = new Set(graph.nodes.filter(node => node.type === "frame" && node.config.hidden_in_run === true).map(node => node.id));
  for (const value of graph.frames) {
    if (!value || typeof value !== "object") continue;
    const frame = value as { id?: unknown; parent_frame_id?: unknown; hidden_in_run?: unknown };
    if (typeof frame.id !== "string") continue;
    parents.set(frame.id, typeof frame.parent_frame_id === "string" ? frame.parent_frame_id : null);
    if (frame.hidden_in_run === true) hidden.add(frame.id);
  }
  return new Set(graph.nodes.filter(node => isCanvasExecutableNodeType(node.type) && visibleParentChain(node.id, parents, hidden)).map(node => node.id));
}
function visibleParentChain(id: string, parents: Map<string, string | null | undefined>, hidden: Set<string>): boolean {
  const visited = new Set<string>();
  let parent = parents.get(id);
  while (parent) {
    if (hidden.has(parent) || visited.has(parent)) return false;
    visited.add(parent); parent = parents.get(parent);
  }
  return true;
}
