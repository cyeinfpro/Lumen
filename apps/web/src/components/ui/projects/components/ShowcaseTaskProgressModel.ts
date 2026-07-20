import type {
  BackendGeneration,
  BackendImageMeta,
  WorkflowRun,
  WorkflowStep,
} from "@/lib/apiClient";

export type ProgressState = "done" | "active" | "pending" | "failed";

export interface ShowcaseProgressTask {
  id: string;
  index: number;
  generation?: BackendGeneration;
}

export interface ShowcaseProgressMilestone {
  label: string;
  detail: string;
  state: ProgressState;
}

export interface ShowcaseProgressModel {
  canceledCount: number;
  failedCount: number;
  historyTaskCount: number;
  milestones: ShowcaseProgressMilestone[];
  percent: number;
  phase: string;
  plannedCount: number;
  preflightDisplay: string;
  progressCount: number;
  runningCount: number;
  tasks: ShowcaseProgressTask[];
}

const PREFLIGHT_LABEL: Record<string, string> = {
  queued: "等待场景规划",
  running: "规划镜头与提示词",
  dispatched: "图像任务已派发",
  failed: "场景规划失败",
};

const PREFLIGHT_PHASE_LABEL: Record<string, string> = {
  director: "GPT-5.5 规划并扩写",
  composer: "GPT-5.5 扩写提示词",
  review: "GPT-5.5 风险复核",
  fallback: "规则兜底",
  dispatching: "派发生图任务",
};

export function buildShowcaseProgressModel(
  workflow: WorkflowRun,
  step: WorkflowStep,
  images: BackendImageMeta[],
): ShowcaseProgressModel {
  const selection = resolveTaskSelection(step);
  const tasks = buildTasks(workflow, selection.currentTaskIds);
  const counts = taskStatusCounts(tasks);
  const progressCount = resolveProgressCount({
    images,
    plannedCount: selection.plannedCount,
    requestedCount: selection.requestedCount,
    step,
    succeededCount: counts.succeededCount,
  });
  const preflight = readPreflight(step);
  const preflightDisplay = resolvePreflightDisplay(preflight);
  const phaseInput = {
    ...counts,
    plannedCount: selection.plannedCount,
    preflightDisplay,
    preflightStatus: preflight.status,
    progressCount,
    stepStatus: step.status,
    taskCount: tasks.length,
  };

  return {
    canceledCount: counts.canceledCount,
    failedCount: counts.failedCount,
    historyTaskCount: selection.historyTaskCount,
    milestones: buildMilestones(phaseInput),
    percent: progressPercent({
      plannedCount: selection.plannedCount,
      preflight,
      progressCount,
      stepStatus: step.status,
      taskCount: tasks.length,
    }),
    phase: resolvePhase(phaseInput),
    plannedCount: selection.plannedCount,
    preflightDisplay,
    progressCount,
    runningCount: counts.runningCount,
    tasks,
  };
}

function resolveTaskSelection(step: WorkflowStep) {
  const taskIds = step.task_ids ?? [];
  const requestedCount =
    numberValue(step.input_json?.active_output_count) ??
    numberValue(step.input_json?.output_count);
  const explicitTaskIds = stringArray(step.input_json?.active_task_ids);
  const currentTaskIds = currentTaskIdsFor(
    taskIds,
    explicitTaskIds,
    requestedCount,
  );
  return {
    currentTaskIds,
    historyTaskCount: Math.max(0, taskIds.length - currentTaskIds.length),
    plannedCount: Math.max(
      explicitTaskIds.length,
      requestedCount ?? 0,
      currentTaskIds.length,
    ),
    requestedCount,
  };
}

function currentTaskIdsFor(
  taskIds: string[],
  explicitTaskIds: string[],
  requestedCount: number | null,
): string[] {
  if (explicitTaskIds.length > 0) return explicitTaskIds;
  if (requestedCount && taskIds.length > requestedCount) {
    return taskIds.slice(-requestedCount);
  }
  return taskIds;
}

function buildTasks(
  workflow: WorkflowRun,
  taskIds: string[],
): ShowcaseProgressTask[] {
  const generationsById = new Map(
    workflow.generations.map((task) => [task.id, task]),
  );
  const bonusByParentId = new Map(
    workflow.generations
      .filter((task) => task.is_dual_race_bonus && task.parent_generation_id)
      .map((task) => [task.parent_generation_id as string, task]),
  );
  return taskIds.map((taskId, index) => ({
    id: taskId,
    index,
    generation: effectiveGeneration(
      generationsById.get(taskId),
      bonusByParentId.get(taskId),
    ),
  }));
}

function taskStatusCounts(tasks: ShowcaseProgressTask[]) {
  let runningCount = 0;
  let succeededCount = 0;
  let failedCount = 0;
  let canceledCount = 0;
  for (const task of tasks) {
    const status = task.generation?.status;
    if (status === "queued" || status === "running") runningCount += 1;
    if (status === "succeeded") succeededCount += 1;
    if (status === "failed") failedCount += 1;
    if (status === "canceled") canceledCount += 1;
  }
  return { canceledCount, failedCount, runningCount, succeededCount };
}

function resolveProgressCount({
  images,
  plannedCount,
  requestedCount,
  step,
  succeededCount,
}: {
  images: BackendImageMeta[];
  plannedCount: number;
  requestedCount: number | null;
  step: WorkflowStep;
  succeededCount: number;
}): number {
  const targetImageCount = numberValue(step.input_json?.target_image_count);
  const explicitBaseline = numberValue(
    step.input_json?.baseline_image_count,
  );
  const baselineImageCount =
    explicitBaseline ??
    inferredBaselineImageCount(targetImageCount, requestedCount);
  const currentImageCount = Math.max(0, images.length - baselineImageCount);
  return Math.max(
    Math.min(plannedCount, currentImageCount),
    Math.min(plannedCount, succeededCount),
  );
}

function inferredBaselineImageCount(
  targetImageCount: number | null,
  requestedCount: number | null,
): number {
  if (targetImageCount === null || requestedCount === null) return 0;
  return Math.max(0, targetImageCount - requestedCount);
}

interface PreflightModel {
  current: number | null;
  detail: string | null;
  phase: string | null;
  status: string | null;
  total: number | null;
}

function readPreflight(step: WorkflowStep): PreflightModel {
  return {
    current: numberValue(step.input_json?.preflight_phase_current),
    detail: stringValue(step.input_json?.preflight_phase_detail),
    phase: stringValue(step.input_json?.preflight_phase),
    status: stringValue(step.input_json?.preflight_status),
    total: numberValue(step.input_json?.preflight_phase_total),
  };
}

function resolvePreflightDisplay(preflight: PreflightModel): string {
  if (preflight.detail) {
    const label = PREFLIGHT_PHASE_LABEL[preflight.phase ?? ""] ?? "规划中";
    return `${label} · ${preflight.detail}`;
  }
  return PREFLIGHT_LABEL[preflight.status ?? "queued"] ?? "等待场景规划";
}

interface PhaseInput {
  canceledCount: number;
  failedCount: number;
  plannedCount: number;
  preflightDisplay: string;
  preflightStatus: string | null;
  progressCount: number;
  runningCount: number;
  stepStatus: string;
  taskCount: number;
}

function buildMilestones(input: PhaseInput): ShowcaseProgressMilestone[] {
  const preflightFailed =
    input.preflightStatus === "failed" || input.stepStatus === "failed";
  const dispatchDone =
    input.taskCount > 0 || input.stepStatus === "completed";
  const outputDone =
    input.plannedCount > 0 && input.progressCount >= input.plannedCount;
  return [
    {
      label: "Submitted",
      detail: "生成请求已提交",
      state: "done",
    },
    {
      label: "Planning",
      detail: input.preflightDisplay,
      state: planningState(preflightFailed, dispatchDone),
    },
    {
      label: "Queue",
      detail: input.taskCount > 0 ? `${input.taskCount} 条任务` : "等待派发",
      state: queueState(preflightFailed, input),
    },
    {
      label: "Outputs",
      detail: `${input.progressCount}/${input.plannedCount} 张`,
      state: outputState(input, outputDone),
    },
  ];
}

function planningState(
  preflightFailed: boolean,
  dispatchDone: boolean,
): ProgressState {
  if (preflightFailed) return "failed";
  return dispatchDone ? "done" : "active";
}

function queueState(
  preflightFailed: boolean,
  input: PhaseInput,
): ProgressState {
  if (preflightFailed) return "failed";
  if (input.taskCount === 0) return "pending";
  return input.runningCount > 0 ? "active" : "done";
}

function outputState(
  input: PhaseInput,
  outputDone: boolean,
): ProgressState {
  const terminalProblems = input.failedCount + input.canceledCount;
  if (terminalProblems > 0 && input.runningCount === 0 && !outputDone) {
    return "failed";
  }
  if (outputDone) return "done";
  return input.taskCount > 0 ? "active" : "pending";
}

function resolvePhase(input: PhaseInput): string {
  if (input.stepStatus === "failed" || input.preflightStatus === "failed") {
    return "生成任务失败";
  }
  if (
    input.plannedCount > 0 &&
    input.progressCount >= input.plannedCount
  ) {
    return "本轮成品图已完成";
  }
  if (input.taskCount === 0) return input.preflightDisplay;
  if (input.runningCount > 0) return "图像任务正在生成";
  if (input.failedCount > 0) return "部分图像任务失败";
  if (input.canceledCount > 0) return "部分图像任务已取消";
  return "等待任务状态同步";
}

function progressPercent({
  plannedCount,
  preflight,
  progressCount,
  stepStatus,
  taskCount,
}: {
  plannedCount: number;
  preflight: PreflightModel;
  progressCount: number;
  stepStatus: string;
  taskCount: number;
}): number {
  if (stepStatus === "failed" || preflight.status === "failed") return 100;
  if (plannedCount > 0 && progressCount >= plannedCount) return 100;
  if (taskCount === 0) return preflightProgressPercent(preflight);
  const taskProgress = plannedCount > 0 ? progressCount / plannedCount : 0;
  return Math.max(25, Math.min(98, Math.round(25 + taskProgress * 70)));
}

function preflightProgressPercent(preflight: PreflightModel): number {
  const phaseProgress = fractionalPhaseProgress(preflight);
  if (phaseProgress !== null) return phaseProgress;
  if (preflight.phase === "dispatching") return 45;
  if (preflight.phase === "fallback") return 16;
  return preflight.status === "running" ? 18 : 8;
}

function fractionalPhaseProgress(preflight: PreflightModel): number | null {
  if (!preflight.total || preflight.current === null) return null;
  if (preflight.phase === "composer") {
    return clampProgress(18, 42, preflight.current / preflight.total);
  }
  if (preflight.phase === "review") {
    return clampProgress(42, 55, preflight.current / preflight.total);
  }
  return null;
}

function clampProgress(start: number, end: number, ratio: number): number {
  return Math.max(
    start,
    Math.min(end, Math.round(start + ratio * (end - start))),
  );
}

function effectiveGeneration(
  base?: BackendGeneration,
  bonus?: BackendGeneration,
): BackendGeneration | undefined {
  if (!bonus) return base;
  if (!base) return bonus;
  if (base.status === "succeeded") return base;
  if (bonus.status === "succeeded") return bonus;
  if (isTerminal(base) && isRunning(bonus)) return bonus;
  if (isTerminal(base) && isTerminal(bonus)) return bonus;
  return base;
}

function isTerminal(generation: BackendGeneration): boolean {
  return generation.status === "failed" || generation.status === "canceled";
}

function isRunning(generation: BackendGeneration): boolean {
  return generation.status === "queued" || generation.status === "running";
}

function numberValue(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter(
        (item): item is string =>
          typeof item === "string" && item.length > 0,
      )
    : [];
}
