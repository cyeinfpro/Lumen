import { Check, Sparkles } from "lucide-react";

import { Button } from "@/components/ui/primitives/Button";
import { RunningState, StageFrame } from "../components/StageFrame";
import { jsonValue } from "../utils";
import type { ProductAnalysisStageController } from "./useProductAnalysisStage";

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

export function ProductAnalysisStageView({
  controller,
}: {
  controller: ProductAnalysisStageController;
}) {
  if (controller.step?.status === "running") {
    return <ProductAnalysisRunning />;
  }
  return <ProductAnalysisReady controller={controller} />;
}

function ProductAnalysisRunning() {
  return (
    <StageFrame
      eyebrow="N°02 — 商品约束"
      title="商品约束"
      subtitle="正在从商品图提取服装还原点、推荐配饰和匹配背景。"
    >
      <RunningState label="正在分析商品约束…" />
    </StageFrame>
  );
}

function ProductAnalysisReady({
  controller,
}: {
  controller: ProductAnalysisStageController;
}) {
  return (
    <StageFrame
      eyebrow="N°02 — 商品约束"
      title="商品约束"
      subtitle="商品识别只负责三件事：锁定服装还原点、给出低存在感配饰、推荐匹配背景。"
    >
      <AnalysisFields controller={controller} />
      <EditableFields controller={controller} />
      <AnalysisSummary controller={controller} />
      <div className="mt-6 grid grid-cols-1 gap-3 border-t border-[var(--border)] pt-5 min-[420px]:flex min-[420px]:flex-wrap min-[420px]:items-center">
        <Button
          variant="primary"
          loading={controller.approve.isPending}
          onClick={controller.submit}
          leftIcon={
            controller.dirty ? (
              <Check className="h-4 w-4" />
            ) : (
              <Sparkles className="h-4 w-4" />
            )
          }
          className="w-full min-[420px]:w-auto"
        >
          {controller.dirty
            ? "确认修正后的商品约束"
            : "沿用 AI 建议"}
        </Button>
        {controller.dirty ? (
          <button
            type="button"
            onClick={controller.reset}
            className="cursor-pointer font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)] underline-offset-4 transition-colors hover:text-[var(--fg-0)] hover:underline"
          >
            Reset
          </button>
        ) : null}
      </div>
    </StageFrame>
  );
}

function AnalysisFields({
  controller,
}: {
  controller: ProductAnalysisStageController;
}) {
  return (
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
            <p className="mt-1.5 whitespace-pre-wrap break-words text-[13px] leading-6 text-[var(--fg-0)]">
              {jsonValue(controller.step?.output_json?.[key])}
            </p>
          </div>
        ))}
      </div>
    </section>
  );
}

function EditableFields({
  controller,
}: {
  controller: ProductAnalysisStageController;
}) {
  return (
    <section className="border-t border-[var(--border)] py-4">
      <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
        Editable
      </p>
      <div className="mt-3 grid gap-4">
        <FieldInput
          label="商品还原点"
          value={controller.mustPreserve}
          onChange={controller.setMustPreserveOverride}
          placeholder="例如 颜色、版型、领口刺绣、纽扣样式"
        />
        <FieldInput
          label="推荐配饰"
          value={controller.accessories}
          onChange={controller.setAccessoriesOverride}
          placeholder="例如 简洁耳饰、浅色鞋子、小号包袋"
        />
        <FieldInput
          label="推荐背景"
          value={controller.background}
          onChange={controller.setBackgroundOverride}
          placeholder="例如 简洁明亮的城市街区或高级棚拍背景"
        />
      </div>
    </section>
  );
}

function AnalysisSummary({
  controller,
}: {
  controller: ProductAnalysisStageController;
}) {
  return (
    <details className="group border-t border-[var(--border)] py-4">
      <summary className="flex cursor-pointer list-none items-center justify-between gap-2 font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)] transition-colors hover:text-[var(--fg-0)]">
        <span>查看识别摘要</span>
        <span
          aria-hidden
          className="text-[var(--fg-3)] transition-transform group-open:rotate-180"
        >
          ▾
        </span>
      </summary>
      <div className="mt-4 grid gap-x-6 gap-y-4 md:grid-cols-3">
        {SUMMARY_FIELDS.map(([label, key]) => (
          <div key={key} className="min-w-0">
            <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-3)]">
              {label}
            </p>
            <p className="mt-1.5 whitespace-pre-wrap break-words text-[13px] leading-6 text-[var(--fg-1)]">
              {jsonValue(controller.step?.output_json?.[key])}
            </p>
          </div>
        ))}
      </div>
    </details>
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
    <label className="block min-w-0">
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
