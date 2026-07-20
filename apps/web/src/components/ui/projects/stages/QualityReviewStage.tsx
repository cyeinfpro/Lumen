"use client";

import type { WorkflowRun } from "@/lib/apiClient";
import { QualityReviewStageView } from "./QualityReviewStageView";
import { useQualityReviewStageController } from "./useQualityReviewStage";

export function QualityReviewStage({ workflow }: { workflow: WorkflowRun }) {
  const controller = useQualityReviewStageController(workflow);
  return <QualityReviewStageView controller={controller} />;
}
