"use client";

import type { WorkflowRun } from "@/lib/apiClient";
import { ShowcaseGenerationStageView } from "./ShowcaseGenerationStageView";
import { useShowcaseGenerationStageController } from "./useShowcaseGenerationStage";

export function ShowcaseGenerationStage({ workflow }: { workflow: WorkflowRun }) {
  const controller = useShowcaseGenerationStageController(workflow);
  return (
    <ShowcaseGenerationStageView
      workflow={workflow}
      controller={controller}
    />
  );
}
