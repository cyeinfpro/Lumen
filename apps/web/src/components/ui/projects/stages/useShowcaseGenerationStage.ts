import { useState } from "react";

import { toast } from "@/components/ui/primitives/Toast";
import {
  useCompleteWorkflowDeliveryMutation,
  useCreateShowcaseImagesMutation,
  useReopenModelSelectionMutation,
} from "@/lib/queries";
import type { BackendImageMeta, WorkflowRun } from "@/lib/apiClient";
import { candidateImages, showcaseImages, stepOf, stringValue } from "../utils";
import {
  buildShowcaseRequest,
  useShowcaseStageForm,
} from "./showcaseStageForm";

export function useShowcaseGenerationStageController(workflow: WorkflowRun) {
  const step = stepOf(workflow, "showcase_generation");
  const isRunning = step?.status === "running";
  const form = useShowcaseStageForm(step?.input_json, isRunning);
  const create = useCreateShowcaseImagesMutation(workflow.id, {
    onError: (err) =>
      toast.error("生成展示图失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      }),
    onSuccess: () => toast.success("展示图任务已派发"),
  });
  const reopen = useReopenModelSelectionMutation(workflow.id, {
    onError: (err) =>
      toast.error("返回重选模特失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      }),
    onSuccess: () => toast.success("已返回模特候选阶段"),
  });
  const complete = useCompleteWorkflowDeliveryMutation(workflow.id, {
    onError: (err) =>
      toast.error("交付失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      }),
    onSuccess: () => toast.success("项目已进入交付状态"),
  });
  const [previewList, setPreviewList] = useState<BackendImageMeta[]>([]);
  const [previewIndex, setPreviewIndex] = useState(-1);
  const [confirmReopen, setConfirmReopen] = useState(false);
  const [confirmRegenerate, setConfirmRegenerate] = useState(false);
  const [confirmDeliver, setConfirmDeliver] = useState(false);

  const hasTasks = Boolean(step?.task_ids?.length);
  const generated = showcaseImages(workflow);
  const modelImages = workflow.model_candidates
    .filter((candidate) => candidate.status === "selected")
    .flatMap((candidate) => candidateImages(workflow, candidate).slice(0, 1));

  const openPreview = (list: BackendImageMeta[], index: number) => {
    setPreviewList(list);
    setPreviewIndex(index);
  };
  const generateShowcase = () => {
    create.mutate(
      buildShowcaseRequest(form, {
        forceIndoorForUnsupportedTemplate: true,
      }),
    );
  };
  const requestGeneration = () => {
    if (hasTasks) setConfirmRegenerate(true);
    else generateShowcase();
  };
  const confirmRegeneration = async () => {
    generateShowcase();
    setConfirmRegenerate(false);
  };
  const confirmModelReopen = async () => {
    reopen.mutate();
    setConfirmReopen(false);
  };
  const confirmDelivery = async () => {
    complete.mutate();
    setConfirmDeliver(false);
  };

  return {
    complete,
    confirmDeliver,
    confirmDelivery,
    confirmModelReopen,
    confirmRegenerate,
    confirmRegeneration,
    confirmReopen,
    create,
    form,
    generated,
    hasGenerationStarted: hasTasks || isRunning,
    hasTasks,
    isRunning,
    modelImages,
    openPreview,
    previewIndex,
    previewList,
    productImages: workflow.product_images,
    reopen,
    requestGeneration,
    setConfirmDeliver,
    setConfirmRegenerate,
    setConfirmReopen,
    setPreviewIndex,
    stageError: stringValue(step?.output_json?.error_message),
    step,
  };
}

export type ShowcaseGenerationStageController = ReturnType<
  typeof useShowcaseGenerationStageController
>;
