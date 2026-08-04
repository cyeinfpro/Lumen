"use client";

// 海报文案切分阶段：
// 1) AI 切分结果（main_title / subtitle / selling_points / cta / price / tone / info_density）展示
// 2) 编辑表单：每个字段可手动覆盖；dirty 时按钮文案变为"确认修正"
// 3) 提交 corrections 后推进到 master_generation

import { Check, Sparkles } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/primitives/Button";
import { Input } from "@/components/ui/primitives/Input";
import { Textarea } from "@/components/ui/primitives/Textarea";
import { toast } from "@/components/ui/primitives/Toast";
import { useApproveCopyAnalysisMutation } from "@/lib/queries";
import type { WorkflowRun } from "@/lib/apiClient";
import { RunningState, StageFrame } from "../components/StageFrame";
import { stepOf } from "../utils";

const COPY_FIELDS: ReadonlyArray<readonly [string, string, "input" | "textarea"]> = [
  ["主标题", "main_title", "input"],
  ["副标题", "subtitle", "input"],
  ["卖点", "selling_points", "textarea"],
  ["行动召唤 (CTA)", "cta", "input"],
  ["价格", "price", "input"],
  ["语气", "tone", "textarea"],
  ["信息密度", "info_density", "input"],
];

function stringFromValue(value: unknown): string {
  if (Array.isArray(value)) {
    return value.filter((v) => typeof v === "string" && v).join("、");
  }
  if (typeof value === "string") return value;
  return "";
}

export function PosterCopyAnalysisStage({ workflow }: { workflow: WorkflowRun }) {
  const step = stepOf(workflow, "copy_analysis");
  const approve = useApproveCopyAnalysisMutation(workflow.id, {
    onError: (err) =>
      toast.error("确认文案切分失败", {
        description: err instanceof Error ? err.message : "稍后重试",
      }),
    onSuccess: () => toast.success("文案切分已确认"),
  });

  const output = (step?.output_json ?? {}) as Record<string, unknown>;
  const initialValues: Record<string, string> = {
    main_title: stringFromValue(output.main_title),
    subtitle: stringFromValue(output.subtitle),
    selling_points: stringFromValue(output.selling_points),
    cta: stringFromValue(output.cta),
    price: stringFromValue(output.price),
    tone: stringFromValue(output.tone),
    info_density: stringFromValue(output.info_density),
  };
  const [overrides, setOverrides] = useState<Record<string, string | null>>({});

  const valueOf = (key: string): string => {
    const ov = overrides[key];
    return ov === null || ov === undefined ? initialValues[key] : ov;
  };
  const dirty = Object.keys(overrides).some(
    (key) => overrides[key] !== null && (overrides[key] ?? "").trim() !== (initialValues[key] ?? "").trim(),
  );

  const submit = () => {
    const corrections: Record<string, unknown> = {};
    if (dirty) {
      for (const [, key] of COPY_FIELDS) {
        const current = valueOf(key).trim();
        const original = (initialValues[key] || "").trim();
        if (current !== original) {
          if (key === "selling_points") {
            corrections.selling_points = current
              .split(/[\n、,，;；]/)
              .map((item) => item.trim())
              .filter(Boolean);
          } else {
            corrections[key] = current;
          }
        }
      }
    }
    approve.mutate({ corrections });
  };

  if (step?.status === "running" || step?.status === "waiting_input") {
    return (
      <StageFrame
        eyebrow="N°03 — 文案切分"
        title="文案切分"
        subtitle="原始文案切分中，整理主标题、副标题、正文和行动召唤。"
      >
        <RunningState label="文案切分中" />
      </StageFrame>
    );
  }

  return (
    <StageFrame
      eyebrow="N°03 — 文案切分"
      title="文案切分"
      subtitle="AI 已将原始文案拆为结构化字段，可以直接确认或手动微调每条文本。"
    >
      <section className="border-t border-[var(--border)] py-4">
        <p className="type-caption text-[var(--fg-2)]">AI 识别</p>
        <div className="mt-3 grid gap-x-6 gap-y-3 md:grid-cols-2">
          {COPY_FIELDS.map(([label, key]) => {
            const original = initialValues[key];
            return (
              <div key={`ai-${key}`} className="min-w-0">
                <p className="type-caption text-[var(--fg-3)]">
                  {label}
                </p>
                <p className="mt-1.5 whitespace-pre-wrap break-words type-body-sm leading-6 text-[var(--fg-0)]">
                  {original || "未识别"}
                </p>
              </div>
            );
          })}
        </div>
      </section>

      <section className="border-t border-[var(--border)] py-4">
        <p className="type-caption text-[var(--fg-2)]">可编辑字段</p>
        <div className="mt-3 grid gap-4 md:grid-cols-2">
          {COPY_FIELDS.map(([label, key, kind]) => {
            const value = valueOf(key);
            const onChange = (next: string) =>
              setOverrides((prev) => ({ ...prev, [key]: next }));
            if (kind === "textarea") {
              return (
                <Textarea
                  key={`edit-${key}`}
                  label={label}
                  value={value}
                  onChange={(event) => onChange(event.target.value)}
                  rows={3}
                  wrapperClassName="md:col-span-2"
                  placeholder={`覆盖 AI 给出的${label}`}
                />
              );
            }
            return (
              <Input
                key={`edit-${key}`}
                label={label}
                value={value}
                onChange={(event) => onChange(event.target.value)}
                placeholder={`覆盖 AI 给出的${label}`}
              />
            );
          })}
        </div>
      </section>

      <div className="mt-6 grid grid-cols-1 gap-3 border-t border-[var(--border)] pt-5 min-[420px]:flex min-[420px]:flex-wrap min-[420px]:items-center">
        <Button
          variant="primary"
          loading={approve.isPending}
          onClick={submit}
          leftIcon={dirty ? <Check className="h-4 w-4" /> : <Sparkles className="h-4 w-4" />}
          className="w-full min-[420px]:w-auto"
        >
          {dirty ? "确认修正" : "沿用切分"}
        </Button>
        {dirty ? (
          <button
            type="button"
            onClick={() => setOverrides({})}
            className="type-control cursor-pointer text-[var(--fg-2)] underline-offset-4 transition-colors hover:text-[var(--fg-0)] hover:underline"
          >
            重置
          </button>
        ) : null}
      </div>
    </StageFrame>
  );
}
