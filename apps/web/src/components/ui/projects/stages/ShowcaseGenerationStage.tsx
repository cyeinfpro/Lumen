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
import { ShowcaseTaskProgress } from "../components/ShowcaseTaskProgress";
import { RunningState, StageFrame } from "../components/StageFrame";
import {
  coerceContinuityAnchor,
  coerceSceneStrategy,
  coerceSceneVariety,
} from "../coercers";
import {
  ASPECT_RATIO_LABELS,
  OUTPUT_COUNT_LABELS,
  CONTINUITY_ANCHOR_LABELS,
  SCENE_ENVIRONMENT_LABELS,
  SCENE_ENVIRONMENT_TEMPLATES,
  SCENE_STRATEGY_LABELS,
  SCENE_VARIETY_LABELS,
  SHOT_PLAN_DEFAULT,
  TEMPLATE_LABELS,
  coerceOutputCount,
  type CreateAspectRatio,
  type CreateContinuityAnchor,
  type CreateOutputCount,
  type CreateSceneEnvironment,
  type CreateSceneStrategy,
  type CreateSceneVariety,
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
  const initialSceneStrategy = coerceSceneStrategy(step?.input_json?.scene_strategy);
  const initialSceneVariety = coerceSceneVariety(step?.input_json?.scene_variety);
  const initialContinuityAnchor = coerceContinuityAnchor(
    step?.input_json?.continuity_anchor,
  );
  const initialAllowPet =
    typeof step?.input_json?.allow_pet === "boolean"
      ? step.input_json.allow_pet
      : initialContinuityAnchor === "pet";
  const initialAllowBackgroundPeople =
    typeof step?.input_json?.allow_background_people === "boolean"
      ? step.input_json.allow_background_people
      : true;
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
  const [sceneStrategy, setSceneStrategy] =
    useState<CreateSceneStrategy>(initialSceneStrategy);
  const [sceneVariety, setSceneVariety] = useState<CreateSceneVariety>(initialSceneVariety);
  const [continuityAnchor, setContinuityAnchor] =
    useState<CreateContinuityAnchor>(initialContinuityAnchor);
  const [allowPet, setAllowPet] = useState(initialAllowPet);
  const [allowBackgroundPeople, setAllowBackgroundPeople] = useState(
    initialAllowBackgroundPeople,
  );
  const sceneEnvironmentEnabled = SCENE_ENVIRONMENT_TEMPLATES.has(template);
  // currentConfigKey 故意只用 initial*：仅在 step.input_json 改变时 reset 本地表单，
  // 避免用户改了 dropdown 又被 render-phase reset 覆盖。
  const currentConfigKey = `${initialTemplate}:${initialAspectRatio}:${initialQuality}:${initialOutputCount}:${initialSceneEnvironment}:${initialSceneStrategy}:${initialSceneVariety}:${initialContinuityAnchor}:${initialAllowPet}:${initialAllowBackgroundPeople}`;
  const [trackedConfigKey, setTrackedConfigKey] = useState(currentConfigKey);
  const [confirmReopen, setConfirmReopen] = useState(false);
  const [confirmRegenerate, setConfirmRegenerate] = useState(false);
  const [confirmDeliver, setConfirmDeliver] = useState(false);

  const isRunning = step?.status === "running";
  const hasTasks = Boolean(step?.task_ids?.length);
  const hasGenerationStarted = hasTasks || isRunning;
  const stageError = stringValue(step?.output_json?.error_message);
  if (!isRunning && trackedConfigKey !== currentConfigKey) {
    setTrackedConfigKey(currentConfigKey);
    setTemplate(initialTemplate);
    setAspectRatio(initialAspectRatio);
    setQuality(initialQuality);
    setOutputCount(initialOutputCount);
    setSceneEnvironment(initialSceneEnvironment);
    setSceneStrategy(initialSceneStrategy);
    setSceneVariety(initialSceneVariety);
    setContinuityAnchor(initialContinuityAnchor);
    setAllowPet(initialAllowPet);
    setAllowBackgroundPeople(initialAllowBackgroundPeople);
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
      scene_strategy: sceneStrategy,
      scene_variety: sceneVariety,
      scene_planner: "gpt55_preflight",
      continuity_anchor: continuityAnchor,
      allow_pet: allowPet,
      allow_background_people: allowBackgroundPeople,
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
        <div className="mt-4 grid gap-x-6 gap-y-4 md:grid-cols-3">
          <SelectField
            label="场景风格"
            value={sceneStrategy}
            onChange={(value) => setSceneStrategy(value as CreateSceneStrategy)}
            disabled={isRunning}
            options={SCENE_STRATEGY_LABELS}
          />
          <SelectField
            label="丰富度"
            value={sceneVariety}
            onChange={(value) => setSceneVariety(value as CreateSceneVariety)}
            disabled={isRunning}
            options={SCENE_VARIETY_LABELS}
          />
          <SelectField
            label="连续元素"
            value={continuityAnchor}
            onChange={(value) => {
              const next = value as CreateContinuityAnchor;
              setContinuityAnchor(next);
              if (next === "pet") setAllowPet(true);
            }}
            disabled={isRunning}
            options={CONTINUITY_ANCHOR_LABELS}
          />
        </div>
        <div className="mt-4 grid gap-x-6 gap-y-3 md:grid-cols-2">
          <CheckboxField
            label="允许宠物"
            checked={allowPet}
            onChange={(next) => {
              setAllowPet(next);
              if (!next && continuityAnchor === "pet") setContinuityAnchor("accessory");
            }}
            disabled={isRunning}
          />
          <CheckboxField
            label="允许远处路人"
            checked={allowBackgroundPeople}
            onChange={setAllowBackgroundPeople}
            disabled={isRunning}
          />
        </div>
        <p className="mt-4 inline-flex min-w-0 flex-wrap items-center gap-2 break-words text-[12px] leading-6 text-[var(--fg-2)]">
          <span className="inline-flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--amber-300)]">
            <Layers className="h-3 w-3" />
            {String(outputCount).padStart(2, "0")} 张
          </span>
          <span aria-hidden className="text-[var(--fg-3)]">·</span>
          <span>{aspectRatio} 画幅</span>
          <span aria-hidden className="text-[var(--fg-3)]">·</span>
          <span>{quality === "4k" ? "4K 终稿" : "2K 高质量"}</span>
          <span aria-hidden className="text-[var(--fg-3)]">·</span>
          <span>{sceneStrategy === "natural_series" ? "GPT-5.5 自然导演" : "GPT-5.5 场景导演"}</span>
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
          {isRunning
            ? "展示图任务运行中"
            : hasTasks
              ? `按当前模板再生成 ${outputCount} 张`
              : `开始生成 ${outputCount} 张展示图`}
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

      {hasGenerationStarted && step ? (
        <ShowcaseTaskProgress workflow={workflow} step={step} images={generated} />
      ) : null}

      {hasGenerationStarted ? (
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
    <label className="block min-w-0">
      <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
        {label}
      </span>
      <select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        disabled={disabled}
        className="mt-2 h-10 w-full min-w-0 border-b border-[var(--border)] bg-transparent px-1 text-[14px] text-[var(--fg-0)] outline-none transition-colors focus:border-[var(--amber-400)] disabled:opacity-40"
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

function CheckboxField({
  label,
  checked,
  onChange,
  disabled,
}: {
  label: string;
  checked: boolean;
  onChange: (next: boolean) => void;
  disabled: boolean;
}) {
  return (
    <label className="inline-flex min-h-10 items-center gap-2 text-[13px] text-[var(--fg-1)]">
      <input
        type="checkbox"
        checked={checked}
        onChange={(event) => onChange(event.target.checked)}
        disabled={disabled}
        className="h-4 w-4 accent-[var(--amber-400)] disabled:opacity-40"
      />
      <span className="min-w-0 break-words">{label}</span>
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
