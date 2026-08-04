import { apiFetch } from "./http";
import {
  semanticJsonPostRequest,
  withSemanticPostIdempotency,
} from "./semanticIdempotency";
import type { WorkflowRun } from "./workflows";

export type PosterAspectRatio =
  "1:1" | "9:16" | "16:9" | "3:4" | "4:3" | "2:3" | "3:2" | "4:5";
export type PosterRevisionScope = "background" | "inpaint" | "style";

export interface PosterBrandAssetsIn {
  logo_image_id?: string | null;
  product_image_id?: string | null;
  primary_color?: string | null;
  font_family?: string | null;
}

export interface PosterDesignWorkflowCreateIn {
  conversation_id?: string | null;
  copy_text: string;
  style_id: string;
  target_aspects?: PosterAspectRatio[];
  brand_assets?: PosterBrandAssetsIn;
  quality_mode?: "standard" | "premium";
  title?: string | null;
}

export interface PosterDesignWorkflowCreateOut {
  workflow_run_id: string;
  status: string;
  current_step: string;
}

export interface CopyAnalysisCorrections {
  main_title?: string | null;
  subtitle?: string | null;
  selling_points?: string[] | null;
  cta?: string | null;
  price?: string | null;
  tone?: string | null;
  info_density?: "high" | "medium" | "low" | string | null;
  [key: string]: unknown;
}

export interface CopyAnalysisApproveIn {
  corrections: CopyAnalysisCorrections;
}

export interface PosterMastersCreateIn {
  candidate_count?: number;
  size_mode?: "auto" | "fixed";
  size?: string | null;
}

export interface PosterMasterApproveIn {
  adjustments?: string;
}

export interface PosterRendersCreateIn {
  aspects: PosterAspectRatio[];
  use_master_as_reference?: boolean;
  quality_mode?: "standard" | "premium";
}

export interface PosterReviseIn {
  scope: PosterRevisionScope;
  instruction: string;
  mask_image_id?: string | null;
}

export interface PosterInpaintIn {
  instruction: string;
  mask_image_id: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object";
}

function validatePosterWorkflowCreateResponse(
  value: PosterDesignWorkflowCreateOut,
): PosterDesignWorkflowCreateOut {
  if (
    !isRecord(value) ||
    typeof value.workflow_run_id !== "string" ||
    value.workflow_run_id.length === 0
  ) {
    throw new TypeError("malformed poster workflow response");
  }
  return value;
}

function validatePosterTaskResponse(
  value: WorkflowRun,
  stepKey: string,
  renderId?: string,
): WorkflowRun {
  if (
    !isRecord(value) ||
    typeof value.id !== "string" ||
    value.id.length === 0 ||
    !Array.isArray(value.steps)
  ) {
    throw new TypeError("malformed poster task response");
  }
  const step = value.steps.find((item) => item.step_key === stepKey);
  const input = isRecord(step?.input_json) ? step.input_json : {};
  const taskIds =
    stepKey === "multi_size_generation"
      ? input.active_task_ids
      : step?.task_ids;
  if (
    !Array.isArray(taskIds) ||
    !taskIds.some((item) => typeof item === "string" && item.length > 0)
  ) {
    throw new TypeError("malformed poster task response");
  }
  if (renderId && input.active_render_id !== renderId) {
    throw new TypeError("malformed poster task response");
  }
  return value;
}

function validatePosterRenderBatchResponse(
  value: WorkflowRun,
  requestedAspects: PosterAspectRatio[],
): WorkflowRun {
  try {
    return validatePosterTaskResponse(value, "multi_size_generation");
  } catch (error) {
    if (!(error instanceof TypeError) || !Array.isArray(value.poster_renders)) {
      throw error;
    }
  }
  const completedAspects = new Set(
    value.poster_renders
      .filter(
        (render) =>
          typeof render.image_id === "string" && render.image_id.length > 0,
      )
      .map((render) => render.aspect_ratio),
  );
  if (
    requestedAspects.length === 0 ||
    !requestedAspects.every((aspect) => completedAspects.has(aspect))
  ) {
    throw new TypeError("malformed poster task response");
  }
  return value;
}

function semanticPosterPost<T>(
  path: string,
  scope: Record<string, string>,
  payload: unknown,
  validate: (value: T) => T,
): Promise<T> {
  return withSemanticPostIdempotency(scope, payload, (idempotencyKey) =>
    apiFetch<T>(
      path,
      semanticJsonPostRequest(payload, idempotencyKey),
    ).then(validate),
  );
}

export function createPosterDesignWorkflow(
  body: PosterDesignWorkflowCreateIn,
): Promise<PosterDesignWorkflowCreateOut> {
  const payload = {
    target_aspects: ["1:1", "9:16", "16:9", "3:4"] as PosterAspectRatio[],
    quality_mode: "premium" as const,
    ...body,
  };
  return semanticPosterPost<PosterDesignWorkflowCreateOut>(
    "/workflows/poster-design",
    { operation: "workflow.poster.create" },
    payload,
    validatePosterWorkflowCreateResponse,
  );
}

export function approveCopyAnalysis(
  workflowId: string,
  body: CopyAnalysisApproveIn = { corrections: {} },
): Promise<WorkflowRun> {
  return apiFetch<WorkflowRun>(
    `/workflows/${workflowId}/steps/copy-analysis/approve`,
    {
      method: "POST",
      body: JSON.stringify({ corrections: body.corrections ?? {} }),
    },
  );
}

export function createPosterMasters(
  workflowId: string,
  body: PosterMastersCreateIn = {},
): Promise<WorkflowRun> {
  const payload = {
    candidate_count: 4,
    size_mode: "fixed" as const,
    ...body,
  };
  return semanticPosterPost<WorkflowRun>(
    `/workflows/${workflowId}/masters`,
    { operation: "workflow.poster.masters.create", workflowId },
    payload,
    (value) => validatePosterTaskResponse(value, "master_generation"),
  );
}

export function approvePosterMaster(
  workflowId: string,
  masterId: string,
  body: PosterMasterApproveIn = {},
): Promise<WorkflowRun> {
  return apiFetch<WorkflowRun>(
    `/workflows/${workflowId}/masters/${masterId}/approve`,
    {
      method: "POST",
      body: JSON.stringify({ adjustments: "", ...body }),
    },
  );
}

export function createPosterRenders(
  workflowId: string,
  body: PosterRendersCreateIn,
): Promise<WorkflowRun> {
  const payload = {
    use_master_as_reference: true,
    quality_mode: "premium" as const,
    ...body,
  };
  return semanticPosterPost<WorkflowRun>(
    `/workflows/${workflowId}/renders`,
    { operation: "workflow.poster.renders.create", workflowId },
    payload,
    (value) => validatePosterRenderBatchResponse(value, payload.aspects),
  );
}

export function revisePosterRender(
  workflowId: string,
  renderId: string,
  body: PosterReviseIn,
): Promise<WorkflowRun> {
  return semanticPosterPost<WorkflowRun>(
    `/workflows/${workflowId}/renders/${renderId}/revise`,
    {
      operation: "workflow.poster.render.revise",
      workflowId,
      renderId,
    },
    body,
    (value) =>
      validatePosterTaskResponse(value, "multi_size_generation", renderId),
  );
}

export function inpaintPosterRender(
  workflowId: string,
  renderId: string,
  body: PosterInpaintIn,
): Promise<WorkflowRun> {
  return semanticPosterPost<WorkflowRun>(
    `/workflows/${workflowId}/renders/${renderId}/inpaint`,
    {
      operation: "workflow.poster.render.inpaint",
      workflowId,
      renderId,
    },
    body,
    (value) =>
      validatePosterTaskResponse(value, "multi_size_generation", renderId),
  );
}
