import { apiFetch } from "./http";
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

export function createPosterDesignWorkflow(
  body: PosterDesignWorkflowCreateIn,
): Promise<PosterDesignWorkflowCreateOut> {
  return apiFetch<PosterDesignWorkflowCreateOut>("/workflows/poster-design", {
    method: "POST",
    body: JSON.stringify({
      target_aspects: ["1:1", "9:16", "16:9", "3:4"],
      quality_mode: "premium",
      ...body,
    }),
  });
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
  return apiFetch<WorkflowRun>(`/workflows/${workflowId}/masters`, {
    method: "POST",
    body: JSON.stringify({
      candidate_count: 4,
      size_mode: "fixed",
      ...body,
    }),
  });
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
  return apiFetch<WorkflowRun>(`/workflows/${workflowId}/renders`, {
    method: "POST",
    body: JSON.stringify({
      use_master_as_reference: true,
      quality_mode: "premium",
      ...body,
    }),
  });
}

export function revisePosterRender(
  workflowId: string,
  renderId: string,
  body: PosterReviseIn,
): Promise<WorkflowRun> {
  return apiFetch<WorkflowRun>(
    `/workflows/${workflowId}/renders/${renderId}/revise`,
    {
      method: "POST",
      body: JSON.stringify(body),
    },
  );
}

export function inpaintPosterRender(
  workflowId: string,
  renderId: string,
  body: PosterInpaintIn,
): Promise<WorkflowRun> {
  return apiFetch<WorkflowRun>(
    `/workflows/${workflowId}/renders/${renderId}/inpaint`,
    {
      method: "POST",
      body: JSON.stringify(body),
    },
  );
}
