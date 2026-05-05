"use client";

// 商品约束阶段（editorial 重构）：
// • 商品识别只服务最终生成提示词：商品还原点、推荐配饰、推荐背景
// • 允许用户修正这三类内容，避免把识别阶段做成复杂商品档案
// • dirty 检测：未修改时按钮文案改为"沿用 AI 建议"，避免空提交
// • hairline 分隔取代嵌套卡片；mono eyebrow + serif italic 标题；底部 underline 输入

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
        eyebrow="N°02 — Product Constraints"
        title="商品约束"
        subtitle="正在从商品图提取服装还原点、推荐配饰和匹配背景。"
      >
        <RunningState label="正在分析商品约束…" />
      </StageFrame>
    );
  }

  return (
    <StageFrame
      eyebrow="N°02 — Product Constraints"
      title="商品约束"
      subtitle="商品识别只负责三件事：锁定服装还原点、给出低存在感配饰、推荐匹配背景。"
    >
      <section className="border-t border-[var(--border)] py-4">
        <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
          AI Reading
        </p>
        <div className="mt-3 grid gap-x-6 gap-y-4 md:grid-cols-3">
          {CORE_FIELDS.map(([label, key]) => (
            <div key={key} className="min-w-0">
              <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-3)]">
                {label}
              </p>
              <p className="mt-1.5 whitespace-pre-wrap text-[13px] leading-6 text-[var(--fg-0)]">
                {jsonValue(step?.output_json?.[key])}
              </p>
            </div>
          ))}
        </div>
      </section>

      <section className="border-t border-[var(--border)] py-4">
        <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
          Editable
        </p>
        <div className="mt-3 grid gap-4">
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
      </section>

      <details className="group border-t border-[var(--border)] py-4">
        <summary className="flex cursor-pointer list-none items-center justify-between gap-2 font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)] transition-colors hover:text-[var(--fg-0)]">
          <span>查看识别摘要</span>
          <span aria-hidden className="text-[var(--fg-3)] transition-transform group-open:rotate-180">
            ▾
          </span>
        </summary>
        <div className="mt-4 grid gap-x-6 gap-y-4 md:grid-cols-3">
          {SUMMARY_FIELDS.map(([label, key]) => (
            <div key={key} className="min-w-0">
              <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-3)]">
                {label}
              </p>
              <p className="mt-1.5 whitespace-pre-wrap text-[13px] leading-6 text-[var(--fg-1)]">
                {jsonValue(step?.output_json?.[key])}
              </p>
            </div>
          ))}
        </div>
      </details>

      <div className="mt-6 flex flex-wrap items-center gap-3 border-t border-[var(--border)] pt-5">
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
            className="cursor-pointer font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)] underline-offset-4 transition-colors hover:text-[var(--fg-0)] hover:underline"
          >
            Reset
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
      <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
        {label}
      </span>
      <input
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        className="mt-2 h-10 w-full border-b border-[var(--border)] bg-transparent px-1 text-[14px] text-[var(--fg-0)] outline-none transition-colors placeholder:text-[var(--fg-3)] focus:border-[var(--amber-400)]"
      />
    </label>
  );
}
