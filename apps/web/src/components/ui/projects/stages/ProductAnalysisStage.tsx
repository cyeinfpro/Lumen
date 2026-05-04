"use client";

// 商品约束阶段：
// • 商品识别只服务最终生成提示词：商品还原点、推荐配饰、推荐背景
// • 允许用户修正这三类内容，避免把识别阶段做成复杂商品档案
// • dirty 检测：未修改时按钮文案改为"沿用 AI 建议"，避免空提交
// • 失败弹 toast；运行中显示 RunningState

import { Check, Sparkles } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/primitives/Button";
import { toast } from "@/components/ui/primitives/Toast";
import { useApproveProductAnalysisMutation } from "@/lib/queries";
import type { WorkflowRun } from "@/lib/apiClient";
import { RunningState, StageFrame } from "../components/StageFrame";
import { jsonValue, stepOf } from "../utils";

const CORE_FIELDS = [
  ["商品还原点", "must_preserve"],
  ["推荐配饰", "styling_recommendations"],
  ["推荐背景", "background_recommendation"],
] as const;

const SUMMARY_FIELDS = [
  ["商品品类", "category"],
  ["主色", "color"],
  ["材质", "material_guess"],
  ["版型", "silhouette"],
  ["关键细节", "key_details"],
  ["风险", "risks"],
] as const;

export function ProductAnalysisStage({ workflow }: { workflow: WorkflowRun }) {
  const step = stepOf(workflow, "product_analysis");
  const approve = useApproveProductAnalysisMutation(workflow.id, {
    onError: (err) => {
      toast.error("确认商品约束失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      });
    },
    onSuccess: () => toast.success("商品约束已固定"),
  });
  const output = step?.output_json ?? {};
  const defaultMustPreserve = Array.isArray(output.must_preserve)
    ? output.must_preserve.map(String).join("、")
    : "";
  const defaultAccessories = Array.isArray(output.styling_recommendations)
    ? output.styling_recommendations.map(String).join("、")
    : typeof output.styling_recommendations === "string"
      ? output.styling_recommendations
      : "";
  const defaultBackground =
    typeof output.background_recommendation === "string"
      ? output.background_recommendation
      : "";

  const [mustPreserveOverride, setMustPreserveOverride] = useState<string | null>(null);
  const [accessoriesOverride, setAccessoriesOverride] = useState<string | null>(null);
  const [backgroundOverride, setBackgroundOverride] = useState<string | null>(null);

  const mustPreserve = mustPreserveOverride ?? defaultMustPreserve;
  const accessories = accessoriesOverride ?? defaultAccessories;
  const background = backgroundOverride ?? defaultBackground;

  // 纯派生计算（开销极低）；React Compiler 会自动 memo，不需要手动 useMemo
  const dirty =
    (mustPreserveOverride !== null &&
      mustPreserveOverride.trim() !== defaultMustPreserve.trim()) ||
    (accessoriesOverride !== null &&
      accessoriesOverride.trim() !== defaultAccessories.trim()) ||
    (backgroundOverride !== null &&
      backgroundOverride.trim() !== defaultBackground.trim());

  const submit = () => {
    approve.mutate({
      must_preserve: mustPreserve
        .split(/[、,，]/)
        .map((item) => item.trim())
        .filter(Boolean),
      styling_recommendations: accessories
        .split(/[、,，]/)
        .map((item) => item.trim())
        .filter(Boolean),
      background_recommendation: background.trim(),
    });
  };

  if (step?.status === "running") {
    return (
      <StageFrame
        title="商品约束"
        subtitle="正在从商品图提取服装还原点、推荐配饰和匹配背景。"
      >
        <RunningState label="正在分析商品约束…" />
      </StageFrame>
    );
  }

  return (
    <StageFrame
      title="商品约束"
      subtitle="商品识别只负责三件事：锁定服装还原点、给出低存在感配饰、推荐匹配背景。"
    >
      <div className="grid gap-3 md:grid-cols-2">
        {CORE_FIELDS.map(([label, key]) => (
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

      <div className="mt-4 grid gap-3">
        <FieldInput
          label="商品还原点"
          value={mustPreserve}
          onChange={setMustPreserveOverride}
          placeholder="例如 颜色、版型、领口刺绣、纽扣样式"
        />
        <FieldInput
          label="推荐配饰"
          value={accessories}
          onChange={setAccessoriesOverride}
          placeholder="例如 简洁耳饰、浅色鞋子、小号包袋"
        />
        <FieldInput
          label="推荐背景"
          value={background}
          onChange={setBackgroundOverride}
          placeholder="例如 简洁明亮的城市街区或高级棚拍背景"
        />
      </div>

      <details className="mt-4 rounded-md border border-[var(--border)] bg-white/[0.025] p-3">
        <summary className="cursor-pointer text-sm text-[var(--fg-1)]">
          查看识别摘要
        </summary>
        <div className="mt-3 grid gap-3 md:grid-cols-2">
          {SUMMARY_FIELDS.map(([label, key]) => (
            <div key={key} className="rounded-md border border-[var(--border)] bg-white/[0.03] p-3">
              <p className="text-[11px] tracking-[0.16em] text-[var(--fg-2)]">{label}</p>
              <p className="mt-1 whitespace-pre-wrap text-sm leading-6 text-[var(--fg-0)]">
                {jsonValue(step?.output_json?.[key])}
              </p>
            </div>
          ))}
        </div>
      </details>

      <div className="mt-4 flex flex-wrap items-center gap-2">
        <Button
          variant="primary"
          loading={approve.isPending}
          onClick={submit}
          leftIcon={dirty ? <Check className="h-4 w-4" /> : <Sparkles className="h-4 w-4" />}
        >
          {dirty ? "确认修正后的商品约束" : "沿用 AI 建议"}
        </Button>
        {dirty ? (
          <button
            type="button"
            onClick={() => {
              setMustPreserveOverride(null);
              setAccessoriesOverride(null);
              setBackgroundOverride(null);
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
