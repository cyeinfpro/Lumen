import { useState } from "react";

import { toast } from "@/components/ui/primitives/Toast";
import type { WorkflowRun } from "@/lib/apiClient";
import { useApproveProductAnalysisMutation } from "@/lib/queries";
import { stepOf } from "../utils";

export function useProductAnalysisStageController(workflow: WorkflowRun) {
  const step = stepOf(workflow, "product_analysis");
  const defaults = productAnalysisDefaults(step?.output_json);
  const [mustPreserveOverride, setMustPreserveOverride] = useState<string | null>(
    null,
  );
  const [accessoriesOverride, setAccessoriesOverride] = useState<string | null>(
    null,
  );
  const [backgroundOverride, setBackgroundOverride] = useState<string | null>(
    null,
  );
  const approve = useApproveProductAnalysisMutation(workflow.id, {
    onError: (err) => {
      toast.error("确认商品约束失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      });
    },
    onSuccess: () => toast.success("商品约束已固定"),
  });

  const mustPreserve = mustPreserveOverride ?? defaults.mustPreserve;
  const accessories = accessoriesOverride ?? defaults.accessories;
  const background = backgroundOverride ?? defaults.background;
  const dirty = productAnalysisDirty(defaults, {
    accessories: accessoriesOverride,
    background: backgroundOverride,
    mustPreserve: mustPreserveOverride,
  });

  const submit = () => {
    approve.mutate({
      must_preserve: splitEditableList(mustPreserve),
      styling_recommendations: splitEditableList(accessories),
      background_recommendation: background.trim(),
    });
  };
  const reset = () => {
    setMustPreserveOverride(null);
    setAccessoriesOverride(null);
    setBackgroundOverride(null);
  };

  return {
    accessories,
    approve,
    background,
    dirty,
    mustPreserve,
    reset,
    setAccessoriesOverride,
    setBackgroundOverride,
    setMustPreserveOverride,
    step,
    submit,
  };
}

export type ProductAnalysisStageController = ReturnType<
  typeof useProductAnalysisStageController
>;

interface ProductAnalysisDefaults {
  mustPreserve: string;
  accessories: string;
  background: string;
}

function productAnalysisDefaults(
  output: Record<string, unknown> | undefined,
): ProductAnalysisDefaults {
  return {
    mustPreserve: arrayText(output?.must_preserve),
    accessories: arrayOrStringText(output?.styling_recommendations),
    background:
      typeof output?.background_recommendation === "string"
        ? output.background_recommendation
        : "",
  };
}

function productAnalysisDirty(
  defaults: ProductAnalysisDefaults,
  overrides: Record<keyof ProductAnalysisDefaults, string | null>,
): boolean {
  return (
    changedOverride(overrides.mustPreserve, defaults.mustPreserve) ||
    changedOverride(overrides.accessories, defaults.accessories) ||
    changedOverride(overrides.background, defaults.background)
  );
}

function changedOverride(override: string | null, fallback: string): boolean {
  return override !== null && override.trim() !== fallback.trim();
}

function arrayText(value: unknown): string {
  return Array.isArray(value) ? value.map(String).join("、") : "";
}

function arrayOrStringText(value: unknown): string {
  if (Array.isArray(value)) return value.map(String).join("、");
  return typeof value === "string" ? value : "";
}

function splitEditableList(value: string): string[] {
  return value
    .split(/[、,，]/)
    .map((item) => item.trim())
    .filter(Boolean);
}
