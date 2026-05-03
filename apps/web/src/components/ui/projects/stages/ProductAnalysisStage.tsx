"use client";

// 商品理解阶段：
// • 卡片化展示 8 个理解字段
// • 修正主色 / 材质 / 必须保留（受控 override）
// • dirty 检测：未修改时按钮文案改为"沿用 AI 摘要"，避免空提交
// • 失败弹 toast；运行中显示 RunningState

import { Check, Sparkles } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/primitives/Button";
import { toast } from "@/components/ui/primitives/Toast";
import { useApproveProductAnalysisMutation } from "@/lib/queries";
import type { WorkflowRun } from "@/lib/apiClient";
import { RunningState, StageFrame } from "../components/StageFrame";
import { jsonValue, stepOf } from "../utils";

const FIELDS = [
  ["商品品类", "category"],
  ["主色", "color"],
  ["材质", "material_guess"],
  ["版型", "silhouette"],
  ["关键细节", "key_details"],
  ["必须保留", "must_preserve"],
  ["搭配推荐", "styling_recommendations"],
  ["风险", "risks"],
] as const;

export function ProductAnalysisStage({ workflow }: { workflow: WorkflowRun }) {
  const step = stepOf(workflow, "product_analysis");
  const approve = useApproveProductAnalysisMutation(workflow.id, {
    onError: (err) => {
      toast.error("确认商品信息失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      });
    },
    onSuccess: () => toast.success("商品信息已固定为硬约束"),
  });
  const output = step?.output_json ?? {};
  const defaultColor = typeof output.color === "string" ? output.color : "";
  const defaultMaterial =
    typeof output.material_guess === "string" ? output.material_guess : "";
  const defaultMustPreserve = Array.isArray(output.must_preserve)
    ? output.must_preserve.map(String).join("、")
    : "";

  const [colorOverride, setColorOverride] = useState<string | null>(null);
  const [materialOverride, setMaterialOverride] = useState<string | null>(null);
  const [mustPreserveOverride, setMustPreserveOverride] = useState<string | null>(null);

  const color = colorOverride ?? defaultColor;
  const material = materialOverride ?? defaultMaterial;
  const mustPreserve = mustPreserveOverride ?? defaultMustPreserve;

  // 纯派生计算（开销极低）；React Compiler 会自动 memo，不需要手动 useMemo
  const dirty =
    (colorOverride !== null && colorOverride.trim() !== defaultColor.trim()) ||
    (materialOverride !== null && materialOverride.trim() !== defaultMaterial.trim()) ||
    (mustPreserveOverride !== null &&
      mustPreserveOverride.trim() !== defaultMustPreserve.trim());

  const submit = () => {
    approve.mutate({
      color: color.trim(),
      material_guess: material.trim(),
      must_preserve: mustPreserve
        .split(/[、,]/)
        .map((item) => item.trim())
        .filter(Boolean),
    });
  };

  if (step?.status === "running") {
    return (
      <StageFrame title="商品理解" subtitle="确认商品摘要后，后续生成会把它作为硬约束。">
        <RunningState label="正在分析商品图…" />
      </StageFrame>
    );
  }

  return (
    <StageFrame
      title="商品理解"
      subtitle="确认商品摘要后，后续生成会把它作为硬约束。可在右侧三个字段做最后修正。"
    >
      <div className="grid gap-3 md:grid-cols-2">
        {FIELDS.map(([label, key]) => (
          <div
            key={key}
            className="rounded-md border border-[var(--border)] bg-white/[0.03] p-3"
          >
            <p className="text-[11px] tracking-[0.16em] text-[var(--fg-2)]">{label}</p>
            <p className="mt-1 whitespace-pre-wrap text-sm leading-6 text-[var(--fg-0)]">
              {jsonValue(step?.output_json?.[key])}
            </p>
          </div>
        ))}
      </div>

      <div className="mt-4 grid gap-3 md:grid-cols-3">
        <FieldInput
          label="修正主色"
          value={color}
          onChange={setColorOverride}
          placeholder="例如 雾霾蓝"
        />
        <FieldInput
          label="修正材质"
          value={material}
          onChange={setMaterialOverride}
          placeholder="例如 桑蚕丝混纺"
        />
        <FieldInput
          label="必须保留"
          value={mustPreserve}
          onChange={setMustPreserveOverride}
          placeholder="例如 领口刺绣、纽扣样式"
        />
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-2">
        <Button
          variant="primary"
          loading={approve.isPending}
          onClick={submit}
          leftIcon={dirty ? <Check className="h-4 w-4" /> : <Sparkles className="h-4 w-4" />}
        >
          {dirty ? "确认修正后的商品信息" : "沿用 AI 摘要"}
        </Button>
        {dirty ? (
          <button
            type="button"
            onClick={() => {
              setColorOverride(null);
              setMaterialOverride(null);
              setMustPreserveOverride(null);
            }}
            className="text-xs text-[var(--fg-2)] underline-offset-2 hover:text-[var(--fg-0)] hover:underline"
          >
            重置修改
          </button>
        ) : null}
      </div>
    </StageFrame>
  );
}

function FieldInput({
  label,
  value,
  onChange,
  placeholder,
}: {
  label: string;
  value: string;
  onChange: (next: string) => void;
  placeholder?: string;
}) {
  return (
    <label className="block">
      <span className="text-sm text-[var(--fg-1)]">{label}</span>
      <input
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        className="mt-2 h-10 w-full rounded-md border border-[var(--border)] bg-[var(--bg-1)] px-3 text-sm text-[var(--fg-0)] outline-none transition-colors focus:border-[var(--border-amber)]"
      />
    </label>
  );
}
