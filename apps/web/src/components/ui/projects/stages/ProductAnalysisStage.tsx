"use client";

import type { WorkflowRun } from "@/lib/apiClient";
import { ProductAnalysisStageView } from "./ProductAnalysisStageView";
import { useProductAnalysisStageController } from "./useProductAnalysisStage";

export function ProductAnalysisStage({ workflow }: { workflow: WorkflowRun }) {
  const controller = useProductAnalysisStageController(workflow);
  return <ProductAnalysisStageView controller={controller} />;
}
