"use client";

// 商品融合阶段（editorial 重构）：
// • 基于已确认模特 + 商品原图，生成 1/2/4/8/16 张电商展示图（默认 4）。
// 1) reopen / 重新生成 用 ConfirmDialog 兜底
// 2) 展示图运行中显示带骨架的 placeholder 网格
// 3) 模板/质量/张数在运行态禁用
// 4) 视觉：hairline 段落 + mono dot status badge + underline select。

import { Check, Layers, RefreshCw, Shirt } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/primitives/Button";
import { ConfirmDialog } from "@/components/ui/primitives/ConfirmDialog";
import { toast } from "@/components/ui/primitives/Toast";
import {
  useCompleteWorkflowDeliveryMutation,
  useCreateShowcaseImagesMutation,
  useReopenModelSelectionMutation,
} from "@/lib/queries";
import type { BackendImageMeta, WorkflowRun } from "@/lib/apiClient";
import { ImageGrid, ReferenceBlock } from "../components/ImageGrid";
import { ImagePreviewModal } from "../components/ImagePreviewModal";
import { RunningState, StageFrame } from "../components/StageFrame";
import {
  ASPECT_RATIO_LABELS,
  OUTPUT_COUNT_LABELS,
  SCENE_ENVIRONMENT_LABELS,
  SCENE_ENVIRONMENT_TEMPLATES,
  SHOT_PLAN_DEFAULT,
  TEMPLATE_LABELS,
  coerceOutputCount,
  type CreateAspectRatio,
  type CreateOutputCount,
  type CreateSceneEnvironment,
  type CreateTemplate,
} from "../types";
import { candidateImages, showcaseImages, stepOf, stringValue } from "../utils";

const OUTPUT_COUNT_SELECT_OPTIONS = OUTPUT_COUNT_LABELS.map(
  ([value, label]) => [String(value), label] as const,
);

export function ShowcaseGenerationStage({ workflow }: { workflow: WorkflowRun }) {
  const step = stepOf(workflow, "showcase_generation");
  const initialTemplate = coerceTemplate(step?.input_json?.template);
  const initialAspectRatio = coerceAspectRatio(step?.input_json?.aspect_ratio);
  const initialQuality = coerceQuality(step?.input_json?.final_quality);
  const initialOutputCount = coerceOutputCount(step?.input_json?.output_count);
  const initialSceneEnvironment = coerceSceneEnvironment(step?.input_json?.scene_environment);
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
  const [template, setTemplate] = useState<CreateTemplate>(initialTemplate);
  const [aspectRatio, setAspectRatio] = useState<CreateAspectRatio>(initialAspectRatio);
  const [quality, setQuality] = useState<"high" | "4k">(initialQuality);
  const [outputCount, setOutputCount] = useState<CreateOutputCount>(initialOutputCount);
  const [sceneEnvironment, setSceneEnvironment] =
    useState<CreateSceneEnvironment>(initialSceneEnvironment);
  const sceneEnvironmentEnabled = SCENE_ENVIRONMENT_TEMPLATES.has(template);
  const currentConfigKey = `${initialTemplate}:${initialAspectRatio}:${initialQuality}:${initialOutputCount}:${initialSceneEnvironment}`;
  const [trackedConfigKey, setTrackedConfigKey] = useState(currentConfigKey);
  const [confirmReopen, setConfirmReopen] = useState(false);
  const [confirmRegenerate, setConfirmRegenerate] = useState(false);
  const [confirmDeliver, setConfirmDeliver] = useState(false);

  const hasTasks = Boolean(step?.task_ids?.length);
  const isRunning = step?.status === "running";
  const stageError = stringValue(step?.output_json?.error_message);
  if (!isRunning && trackedConfigKey !== currentConfigKey) {
    setTrackedConfigKey(currentConfigKey);
    setTemplate(initialTemplate);
    setAspectRatio(initialAspectRatio);
    setQuality(initialQuality);
    setOutputCount(initialOutputCount);
    setSceneEnvironment(initialSceneEnvironment);
  }
  const generated = showcaseImages(workflow);
  const productImages = workflow.product_images;
  const modelImages = workflow.model_candidates
    .filter((candidate) => candidate.status === "selected")
    .flatMap((candidate) => candidateImages(workflow, candidate).slice(0, 1));

  const openPreview = (list: BackendImageMeta[], index: number) => {
    setPreviewList(list);
    setPreviewIndex(index);
  };

  const generateShowcase = () => {
    create.mutate({
      template,
      shot_plan: [...SHOT_PLAN_DEFAULT],
      aspect_ratio: aspectRatio,
      final_quality: quality,
      output_count: outputCount,
      scene_environment: sceneEnvironmentEnabled ? sceneEnvironment : "indoor",
    });
  };

  return (
    <StageFrame
      eyebrow="N°06 — 展示融合"
      title="商品融合"
      subtitle="使用已确认模特和商品图，生成电商展示图。可选 1/2/4/8/16 张，张数越多耗时越长。"
      badge={
        isRunning ? (
          <span className="inline-flex items-center gap-2 font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--amber-300)]">
            <span className="relative flex h-1.5 w-1.5">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-[var(--amber-400)] opacity-60" />
              <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-[var(--amber-400)]" />
            </span>
            Running
          </span>
        ) : null
      }
      actions={
        <Button
          variant="outline"
          size="sm"
          loading={reopen.isPending}
          onClick={() => setConfirmReopen(true)}
          leftIcon={<RefreshCw className="h-3.5 w-3.5" />}
          className="w-full sm:w-auto"
        >
          返回重选模特
        </Button>
      }
    >
      {stageError ? (
        <section className="border-t border-[var(--border)] py-4">
          <p className="border-l-2 border-[var(--danger)] pl-3 text-[13px] leading-6 text-[var(--danger)]">
            {stageError}
          </p>
        </section>
      ) : null}

      <section className="grid gap-x-6 gap-y-2 lg:grid-cols-2">
        <ReferenceBlock
          title="商品原图"
          images={productImages}
          onPreview={(_image, index) => openPreview(productImages, index)}
        />
        <ReferenceBlock
          title="已确认模特"
          images={modelImages}
          onPreview={(_image, index) => openPreview(modelImages, index)}
        />
      </section>

      <section className="border-t border-[var(--border)] py-5">
        <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
          Output Setup
        </p>
        <div
          className={`mt-3 grid gap-x-6 gap-y-4 ${
            sceneEnvironmentEnabled ? "md:grid-cols-5" : "md:grid-cols-4"
          }`}
        >
          <SelectField
            label="输出模板"
            value={template}
            onChange={(value) => setTemplate(value as CreateTemplate)}
            disabled={isRunning}
            options={TEMPLATE_LABELS}
          />
          {sceneEnvironmentEnabled ? (
            <SelectField
              label="室内 / 室外"
              value={sceneEnvironment}
              onChange={(value) => setSceneEnvironment(value as CreateSceneEnvironment)}
              disabled={isRunning}
              options={SCENE_ENVIRONMENT_LABELS}
            />
          ) : null}
          <SelectField
            label="画幅比例"
            value={aspectRatio}
            onChange={(value) => setAspectRatio(value as CreateAspectRatio)}
            disabled={isRunning}
            options={ASPECT_RATIO_LABELS}
          />
          <SelectField
            label="质量模式"
            value={quality}
            onChange={(value) => setQuality(value as "high" | "4k")}
            disabled={isRunning}
            options={[
              ["high", "2K 高质量"],
              ["4k", "4K 终稿"],
            ]}
          />
          <SelectField
            label="张数"
            value={String(outputCount)}
            onChange={(value) => setOutputCount(coerceOutputCount(value))}
            disabled={isRunning}
            options={OUTPUT_COUNT_SELECT_OPTIONS}
          />
        </div>
        <p className="mt-4 inline-flex flex-wrap items-center gap-2 text-[12px] leading-6 text-[var(--fg-2)]">
          <span className="inline-flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--amber-300)]">
            <Layers className="h-3 w-3" />
            {String(outputCount).padStart(2, "0")} 张
          </span>
          <span aria-hidden className="text-[var(--fg-3)]">·</span>
          <span>{aspectRatio} 画幅</span>
          <span aria-hidden className="text-[var(--fg-3)]">·</span>
          <span>{quality === "4k" ? "4K 终稿" : "2K 高质量"}</span>
          <span aria-hidden className="text-[var(--fg-3)]">·</span>
          <span className="text-[var(--fg-3)]">张数越多耗时越长</span>
        </p>
      </section>

      <section className="grid grid-cols-1 gap-3 border-t border-[var(--border)] py-5 min-[420px]:grid-cols-2 sm:flex sm:flex-wrap sm:items-center">
        <Button
          variant={hasTasks ? "outline" : "primary"}
          loading={create.isPending}
          disabled={isRunning}
          onClick={() => (hasTasks ? setConfirmRegenerate(true) : generateShowcase())}
          leftIcon={hasTasks ? <RefreshCw className="h-4 w-4" /> : <Shirt className="h-4 w-4" />}
          className="w-full sm:w-auto"
        >
          {hasTasks ? `按当前模板再生成 ${outputCount} 张` : `开始生成 ${outputCount} 张展示图`}
        </Button>
        {generated.length > 0 ? (
          <Button
            variant="primary"
            loading={complete.isPending}
            disabled={isRunning}
            onClick={() => setConfirmDeliver(true)}
            leftIcon={<Check className="h-4 w-4" />}
            className="w-full sm:w-auto"
          >
            确认交付
          </Button>
        ) : null}
      </section>

      {hasTasks ? (
        <section className="border-t border-[var(--border)] py-5">
          <div className="mb-3 flex items-center justify-between gap-3">
            <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
              Generated
            </p>
            <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-3)] tabular-nums">
              {String(generated.length).padStart(2, "0")} 张
            </p>
          </div>
          {generated.length === 0 ? (
            <RunningState label="展示图正在生成…" />
          ) : (
            <ImageGrid
              images={generated}
              onPreview={(_image, index) => openPreview(generated, index)}
            />
          )}
        </section>
      ) : null}

      <ImagePreviewModal
        images={previewList}
        index={previewIndex}
        onIndexChange={setPreviewIndex}
        onClose={() => setPreviewIndex(-1)}
      />

      <ConfirmDialog
        open={confirmReopen}
        onOpenChange={setConfirmReopen}
        title="返回重选模特？"
        description="将放弃当前展示图与质检结果，回到模特候选阶段。"
        confirmText="返回重选"
        tone="danger"
        confirming={reopen.isPending}
        onConfirm={async () => {
          reopen.mutate();
          setConfirmReopen(false);
        }}
      />

      <ConfirmDialog
        open={confirmRegenerate}
        onOpenChange={setConfirmRegenerate}
        title={`再生成 ${outputCount} 张展示图？`}
        description={`已生成的成品会继续保留，新一轮会按当前选择的模板、${aspectRatio} 画幅和 ${
          quality === "4k" ? "4K 终稿" : "2K 高质量"
        } 模式追加生成 ${outputCount} 张。`}
        confirmText="追加生成"
        confirming={create.isPending}
        onConfirm={async () => {
          generateShowcase();
          setConfirmRegenerate(false);
        }}
      />

      <ConfirmDialog
        open={confirmDeliver}
        onOpenChange={setConfirmDeliver}
        title="确认交付项目？"
        description="项目状态将变为已交付，当前成品图开放下载。"
        confirmText="确认交付"
        confirming={complete.isPending}
        onConfirm={async () => {
          complete.mutate();
          setConfirmDeliver(false);
        }}
      />
    </StageFrame>
  );
}

function SelectField({
  label,
  value,
  onChange,
  disabled,
  options,
}: {
  label: string;
  value: string;
  onChange: (next: string) => void;
  disabled: boolean;
  options: ReadonlyArray<readonly [string, string]>;
}) {
  return (
    <label className="block">
      <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
        {label}
      </span>
      <select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        disabled={disabled}
        className="mt-2 h-10 w-full border-b border-[var(--border)] bg-transparent px-1 text-[14px] text-[var(--fg-0)] outline-none transition-colors focus:border-[var(--amber-400)] disabled:opacity-40"
      >
        {options.map(([optionValue, optionLabel]) => (
          <option key={optionValue} value={optionValue} className="bg-[var(--bg-1)]">
            {optionLabel}
          </option>
        ))}
      </select>
    </label>
  );
}

function coerceTemplate(value: unknown): CreateTemplate {
  return TEMPLATE_LABELS.some(([option]) => option === value)
    ? (value as CreateTemplate)
    : "premium_studio";
}

function coerceAspectRatio(value: unknown): CreateAspectRatio {
  return ASPECT_RATIO_LABELS.some(([option]) => option === value)
    ? (value as CreateAspectRatio)
    : "4:5";
}

function coerceQuality(value: unknown): "high" | "4k" {
  return value === "4k" ? "4k" : "high";
}

function coerceSceneEnvironment(value: unknown): CreateSceneEnvironment {
  return value === "outdoor" ? "outdoor" : "indoor";
}
