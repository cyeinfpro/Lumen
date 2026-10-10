import type { CanvasPlanInput, CanvasPlanIntent, CanvasPlanPreview, CanvasPlanRunDetail } from "./runPlanTypes";

export function parseCanvasPlanBudget(value: string): number {
  if (!/^(0|[1-9]\d*)$/.test(value.trim())) throw new Error("预算须为非负整数 CNY 微单位");
  const result = Number(value);
  if (!Number.isSafeInteger(result)) throw new Error("预算超出安全整数范围");
  return result;
}
export function assertCanvasPlanInput(input: CanvasPlanInput): void {
  parseCanvasPlanBudget(String(input.budget_micro));
  if (!Number.isSafeInteger(input.document_revision) || input.document_revision < 1) throw new Error("画布版本无效");
  if (!["selection", "upstream", "all"].includes(input.kind)) throw new Error("运行范围无效");
  if (input.kind !== "all" && input.target_node_ids.length === 0) throw new Error("请选择运行目标");
  for (const index of Object.values(input.output_indices)) {
    if (!Number.isSafeInteger(index) || index < 0 || index > 9) throw new Error("请选择有效输出序号");
  }
  for (const choice of Object.values(input.reuse_outputs)) {
    if (!choice.execution_id || !Number.isSafeInteger(choice.output_index) || choice.output_index < 0 || choice.output_index > 9) throw new Error("复用须指定执行记录及精确输出");
  }
}
export function assertCanvasPlanIntent(intent: CanvasPlanIntent): void {
  if (intent.kind === "start") {
    assertCanvasPlanInput(intent.body);
    if (!/^[a-f0-9]{64}$/.test(intent.body.plan_hash)) throw new Error("请重新预览执行计划");
  } else {
    parseCanvasPlanBudget(String(intent.body.additional_budget_micro));
    if (!intent.run_id || !intent.body.execution_ids.length || new Set(intent.body.execution_ids).size !== intent.body.execution_ids.length) throw new Error("请选择明确失败的执行记录");
  }
}
export function validateCanvasPlanPreview(value: CanvasPlanPreview, canvasId: string, input: CanvasPlanInput): CanvasPlanPreview {
  const plan = value?.plan;
  validatePreviewScope(plan, canvasId, input);
  if (value.budget_semantics !== "admission_estimate_not_settlement_cap") throw new Error("预览响应无效");
  const cost = value.estimated_cost_micro;
  if (cost === null || !Number.isSafeInteger(cost) || cost < 0 || cost > input.budget_micro) throw new Error("费用未知或超出准入预算");
  if (plan.steps.some(step => !step.node_id || !Array.isArray(step.dependencies) ||
      (!step.reuse && (step.estimated_cost_micro === null || !Number.isSafeInteger(step.estimated_cost_micro) || step.estimated_cost_micro < 0)))) throw new Error("计划包含未知费用");
  return value;
}
function validatePreviewScope(plan: CanvasPlanPreview["plan"], canvasId: string, input: CanvasPlanInput) {
  if (!plan || plan.canvas_id !== canvasId || plan.document_revision !== input.document_revision ||
      !/^[a-f0-9]{64}$/.test(plan.plan_hash) || !Array.isArray(plan.steps) || !Array.isArray(plan.target_node_ids) ||
      plan.budget_micro !== input.budget_micro || plan.kind !== input.kind) throw new Error("预览响应无效，请重新预览");
}
export function canvasRepairCandidates(run: CanvasPlanRunDetail) {
  const latest = new Map<string, CanvasPlanRunDetail["executions"][number]>();
  for (const execution of run.executions) {
    if (!latest.has(execution.node_id) || execution.attempt > latest.get(execution.node_id)!.attempt) latest.set(execution.node_id, execution);
  }
  return [...latest.values()].filter(execution => execution.status === "failed" && execution.outputs.length === 0 &&
    (execution.error_code === "canvas_plan_admission_failed" && !execution.tasks?.length ||
      Boolean(execution.tasks?.length && execution.tasks.every(task =>
        task.recovery?.automatic_resubmit === false &&
        ["failed", "canceled", "expired"].includes(task.recovery.state) &&
        task.recovery.can_generate_new === true))));
}
