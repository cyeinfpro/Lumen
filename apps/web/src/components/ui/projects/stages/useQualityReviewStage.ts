import { useState } from "react";

import { toast } from "@/components/ui/primitives/Toast";
import type { BackendImageMeta, WorkflowRun } from "@/lib/apiClient";
import {
  useCompleteWorkflowDeliveryMutation,
  useCreateShowcaseImagesMutation,
  useReopenModelSelectionMutation,
  useReviseWorkflowImageMutation,
} from "@/lib/queries";
import { showcaseImages, stepOf, stringValue } from "../utils";
import {
  buildShowcaseRequest,
  useShowcaseStageForm,
} from "./showcaseStageForm";

export function useQualityReviewStageController(workflow: WorkflowRun) {
  const images = showcaseImages(workflow);
  const showcaseStep = stepOf(workflow, "showcase_generation");
  const qualityStep = stepOf(workflow, "quality_review");
  const isShowcaseRunning = showcaseStep?.status === "running";
  const form = useShowcaseStageForm(
    showcaseStep?.input_json,
    isShowcaseRunning,
  );
  const [selectedImageId, setSelectedImageId] =
    useSyncedSelectedImageId(images);
  const [instruction, setInstruction] = useState(
    "衣服颜色更接近商品图，领口不要变窄，保留模特脸",
  );
  const [previewIndex, setPreviewIndex] = useState(-1);
  const [confirmReopen, setConfirmReopen] = useState(false);
  const [confirmRegenerate, setConfirmRegenerate] = useState(false);
  const [confirmDeliver, setConfirmDeliver] = useState(false);

  const create = useCreateShowcaseImagesMutation(workflow.id, {
    onError: (err) =>
      toast.error("继续生成失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      }),
    onSuccess: () => toast.success(`已追加派发 ${form.outputCount} 张展示图`),
  });
  const revise = useReviseWorkflowImageMutation(workflow.id, {
    onError: (err) =>
      toast.error("文字返修失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      }),
    onSuccess: () => toast.success("返修任务已派发"),
  });
  const complete = useCompleteWorkflowDeliveryMutation(workflow.id, {
    onError: (err) =>
      toast.error("交付失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      }),
    onSuccess: () => toast.success("项目已进入交付状态"),
  });
  const reopen = useReopenModelSelectionMutation(workflow.id, {
    onError: (err) =>
      toast.error("返回重选模特失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      }),
    onSuccess: () => toast.success("已返回模特候选阶段"),
  });

  const reportsByImage = new Map(
    workflow.quality_reports.map((report) => [report.image_id, report]),
  );
  const counts = qualityRecommendationCounts(workflow);
  const isGenerating = create.isPending || isShowcaseRunning;

  const generateShowcase = () => {
    create.mutate(buildShowcaseRequest(form));
  };
  const reviseSelectedImage = () => {
    revise.mutate({
      image_id: selectedImageId,
      instruction,
      scope: "full_image",
    });
  };
  const confirmModelReopen = async () => {
    reopen.mutate();
    setConfirmReopen(false);
  };
  const confirmGeneration = async () => {
    generateShowcase();
    setConfirmRegenerate(false);
  };
  const confirmDelivery = async () => {
    complete.mutate();
    setConfirmDeliver(false);
  };

  return {
    ...counts,
    complete,
    confirmDeliver,
    confirmDelivery,
    confirmGeneration,
    confirmModelReopen,
    confirmRegenerate,
    confirmReopen,
    create,
    form,
    images,
    instruction,
    isGenerating,
    previewIndex,
    reopen,
    reportsByImage,
    revise,
    reviseSelectedImage,
    selectedImageId,
    setConfirmDeliver,
    setConfirmRegenerate,
    setConfirmReopen,
    setInstruction,
    setPreviewIndex,
    setSelectedImageId,
    stageError:
      stringValue(qualityStep?.output_json?.error_message) ??
      stringValue(showcaseStep?.output_json?.error_message),
  };
}

export type QualityReviewStageController = ReturnType<
  typeof useQualityReviewStageController
>;

function useSyncedSelectedImageId(
  images: BackendImageMeta[],
): [string, (value: string) => void] {
  const [selectedImageId, setSelectedImageId] = useState(images[0]?.id ?? "");
  const validSelectedId = resolveSelectedImageId(images, selectedImageId);
  if (validSelectedId !== selectedImageId) {
    setSelectedImageId(validSelectedId);
  }
  return [validSelectedId, setSelectedImageId];
}

function resolveSelectedImageId(
  images: BackendImageMeta[],
  selectedImageId: string,
): string {
  if (images.length === 0) return "";
  if (images.some((image) => image.id === selectedImageId)) {
    return selectedImageId;
  }
  return images[0].id;
}

function qualityRecommendationCounts(workflow: WorkflowRun) {
  let reviseCount = 0;
  let approveCount = 0;
  for (const report of workflow.quality_reports) {
    if (report.recommendation === "revise") reviseCount += 1;
    if (report.recommendation === "approve") approveCount += 1;
  }
  return { approveCount, reviseCount };
}
