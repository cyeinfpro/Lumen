"use client";

import type { WorkflowRun } from "@/lib/apiClient";
import { ModelCandidatesStageView } from "./ModelCandidatesStageView";
import { useModelCandidatesStageController } from "./useModelCandidatesStage";

export function ModelCandidatesStage({ workflow }: { workflow: WorkflowRun }) {
  const controller = useModelCandidatesStageController(workflow);
  return (
    <ModelCandidatesStageView workflow={workflow} controller={controller} />
  );
}
