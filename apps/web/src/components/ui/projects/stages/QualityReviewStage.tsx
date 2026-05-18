"use client";

// 质检返修阶段（editorial 重构）：
// 1) render-phase reset 同步 selectedImageId 至 images（解决新图返回后选中错位）
// 2) 质检阶段可继续调整场景 / 比例 / 分辨率 / 张数并追加生成
// 3) 返修 / 交付 走 toast；交付确认走 ConfirmDialog
// 4) 重选模特再次走 ConfirmDialog（破坏性）
// 5) 视觉：hairline 段落 + mono eyebrow + underline 输入；按钮 outline / primary 收敛。

import { Check, Layers, RefreshCw, Shirt } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/primitives/Button";
import { ConfirmDialog } from "@/components/ui/primitives/ConfirmDialog";
import { toast } from "@/components/ui/primitives/Toast";
import {
  useCompleteWorkflowDeliveryMutation,
  useCreateShowcaseImagesMutation,
  useReopenModelSelectionMutation,
  useReviseWorkflowImageMutation,
} from "@/lib/queries";
import type { BackendImageMeta, WorkflowRun } from "@/lib/apiClient";
import { ImagePreviewModal } from "../components/ImagePreviewModal";
import { ResultImageCard } from "../components/ResultImageCard";
import { RunningState, StageFrame } from "../components/StageFrame";
import {
  coerceContinuityAnchor,
  coerceSceneStrategy,
  coerceSceneVariety,
} from "../coercers";
import {
  ASPECT_RATIO_LABELS,
  CONTINUITY_ANCHOR_LABELS,
  OUTPUT_COUNT_LABELS,
  SCENE_STRATEGY_LABELS,
  SCENE_VARIETY_LABELS,
  SHOT_PLAN_DEFAULT,
  TEMPLATE_LABELS,
  type CreateAspectRatio,
  type CreateContinuityAnchor,
  type CreateOutputCount,
  type CreateSceneEnvironment,
  type CreateSceneStrategy,
  type CreateSceneVariety,
  type CreateTemplate,
} from "../types";
import { showcaseImages, stepOf, stringValue } from "../utils";

const OUTPUT_COUNT_SELECT_OPTIONS = OUTPUT_COUNT_LABELS.map(
  ([value, label]) => [String(value), label] as const,
);

export function QualityReviewStage({ workflow }: { workflow: WorkflowRun }) {
  const images = showcaseImages(workflow);
  const showcaseStep = stepOf(workflow, "showcase_generation");
  const qualityStep = stepOf(workflow, "quality_review");
  const stageError =
    stringValue(qualityStep?.output_json?.error_message) ??
    stringValue(showcaseStep?.output_json?.error_message);
  const reportsByImage = new Map(
    workflow.quality_reports.map((report) => [report.image_id, report]),
  );
  const initialTemplate = coerceTemplate(showcaseStep?.input_json?.template);
  const initialAspectRatio = coerceAspectRatio(showcaseStep?.input_json?.aspect_ratio);
  const initialQuality = coerceQuality(showcaseStep?.input_json?.final_quality);
  const initialOutputCount = coerceOutputCount(showcaseStep?.input_json?.output_count);
  const initialSceneEnvironment = coerceSceneEnvironment(
    showcaseStep?.input_json?.scene_environment,
  );
  const initialSceneStrategy = coerceSceneStrategy(showcaseStep?.input_json?.scene_strategy);
  const initialSceneVariety = coerceSceneVariety(showcaseStep?.input_json?.scene_variety);
  const initialContinuityAnchor = coerceContinuityAnchor(
    showcaseStep?.input_json?.continuity_anchor,
  );
  const initialAllowPet =
    typeof showcaseStep?.input_json?.allow_pet === "boolean"
      ? showcaseStep.input_json.allow_pet
      : initialContinuityAnchor === "pet";
  const initialAllowBackgroundPeople =
    typeof showcaseStep?.input_json?.allow_background_people === "boolean"
      ? showcaseStep.input_json.allow_background_people
      : true;

  // render-phase reset：images 重新返回（重生成 / 返修后）旧 selectedImageId 失效时
  // 同步到第一张。直接 if + setState，比 effect 少一次 commit。
  const [selectedImageId, setSelectedImageId] = useState<string>(images[0]?.id ?? "");
  const validSelectedId =
    !images.length
      ? ""
      : images.some((image) => image.id === selectedImageId)
        ? selectedImageId
        : images[0].id;
  if (validSelectedId !== selectedImageId) {
    setSelectedImageId(validSelectedId);
  }

  const [instruction, setInstruction] = useState(
    "衣服颜色更接近商品图，领口不要变窄，保留模特脸",
  );
  const [template, setTemplate] = useState<CreateTemplate>(initialTemplate);
  const [aspectRatio, setAspectRatio] = useState<CreateAspectRatio>(initialAspectRatio);
  const [quality, setQuality] = useState<"high" | "4k">(initialQuality);
  const [outputCount, setOutputCount] = useState<CreateOutputCount>(initialOutputCount);
  const [sceneStrategy, setSceneStrategy] =
    useState<CreateSceneStrategy>(initialSceneStrategy);
  const [sceneVariety, setSceneVariety] = useState<CreateSceneVariety>(initialSceneVariety);
  const [continuityAnchor, setContinuityAnchor] =
    useState<CreateContinuityAnchor>(initialContinuityAnchor);
  const [allowPet, setAllowPet] = useState(initialAllowPet);
  const [allowBackgroundPeople, setAllowBackgroundPeople] = useState(
    initialAllowBackgroundPeople,
  );
  // currentConfigKey 故意只用 initial*：仅在 step.input_json 改变时 reset 本地表单，
  // 避免用户改了 dropdown 又被 render-phase reset 覆盖。
  const currentConfigKey = `${initialTemplate}:${initialAspectRatio}:${initialQuality}:${initialOutputCount}:${initialSceneStrategy}:${initialSceneVariety}:${initialContinuityAnchor}:${initialAllowPet}:${initialAllowBackgroundPeople}`;
  const [trackedConfigKey, setTrackedConfigKey] = useState(currentConfigKey);
  const [previewIndex, setPreviewIndex] = useState(-1);
  const [confirmReopen, setConfirmReopen] = useState(false);
  const [confirmRegenerate, setConfirmRegenerate] = useState(false);
  const [confirmDeliver, setConfirmDeliver] = useState(false);

  const create = useCreateShowcaseImagesMutation(workflow.id, {
    onError: (err) =>
      toast.error("继续生成失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      }),
    onSuccess: () => toast.success(`已追加派发 ${outputCount} 张展示图`),
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

  const isShowcaseRunning = showcaseStep?.status === "running";
  if (!isShowcaseRunning && trackedConfigKey !== currentConfigKey) {
    setTrackedConfigKey(currentConfigKey);
    setTemplate(initialTemplate);
    setAspectRatio(initialAspectRatio);
    setQuality(initialQuality);
    setOutputCount(initialOutputCount);
    setSceneStrategy(initialSceneStrategy);
    setSceneVariety(initialSceneVariety);
    setContinuityAnchor(initialContinuityAnchor);
    setAllowPet(initialAllowPet);
    setAllowBackgroundPeople(initialAllowBackgroundPeople);
  }

  const reviseCount = workflow.quality_reports.filter(
    (report) => report.recommendation === "revise",
  ).length;
  const approveCount = workflow.quality_reports.filter(
    (report) => report.recommendation === "approve",
  ).length;
  const isGenerating = create.isPending || isShowcaseRunning;

  const generateShowcase = () => {
    create.mutate({
      template,
      shot_plan: [...SHOT_PLAN_DEFAULT],
      aspect_ratio: aspectRatio,
      final_quality: quality,
      output_count: outputCount,
      scene_environment: initialSceneEnvironment,
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
      eyebrow="N°07 — 质量复核"
      title="质检返修"
      subtitle="每张展示图都有质检结论。可文字返修，也可调整场景、比例、分辨率和张数继续追加生成。"
      actions={
        <Button
          variant="outline"
          size="sm"
          loading={reopen.isPending}
          disabled={isGenerating}
          onClick={() => setConfirmReopen(true)}
          leftIcon={<RefreshCw className="h-3.5 w-3.5" />}
        >
          重选模特
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

      <section className="border-t border-[var(--border)] py-5">
        <div className="mb-3 flex items-center justify-between gap-3">
          <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
            Showcases
          </p>
          <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-3)] tabular-nums">
            <span className="text-[var(--success)]">{String(approveCount).padStart(2, "0")}</span>
            <span className="mx-1.5 text-[var(--fg-3)]">·</span>
            <span className="text-[var(--danger)]">{String(reviseCount).padStart(2, "0")}</span>
            <span className="mx-1.5 text-[var(--fg-3)]">·</span>
            <span>{String(images.length).padStart(2, "0")}</span>
          </p>
        </div>
        {images.length === 0 ? (
          <RunningState label="等待展示图完成…" />
        ) : (
          <div className="grid gap-x-4 gap-y-6 md:grid-cols-2 xl:grid-cols-4">
            {images.map((image: BackendImageMeta, index) => (
              <ResultImageCard
                key={image.id}
                image={image}
                report={reportsByImage.get(image.id)}
                selected={selectedImageId === image.id}
                onSelect={() => setSelectedImageId(image.id)}
                onPreview={() => setPreviewIndex(index)}
              />
            ))}
          </div>
        )}
      </section>

      <section className="border-t border-[var(--border)] py-5">
        <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
          Continue Generating
        </p>
        <div className="mt-3 grid gap-x-6 gap-y-4 md:grid-cols-4">
          <SelectField
            label="场景模板"
            value={template}
            onChange={(value) => setTemplate(value as CreateTemplate)}
            disabled={isGenerating}
            options={TEMPLATE_LABELS}
          />
          <SelectField
            label="画幅比例"
            value={aspectRatio}
            onChange={(value) => setAspectRatio(value as CreateAspectRatio)}
            disabled={isGenerating}
            options={ASPECT_RATIO_LABELS}
          />
          <SelectField
            label="分辨率"
            value={quality}
            onChange={(value) => setQuality(value as "high" | "4k")}
            disabled={isGenerating}
            options={[
              ["high", "2K 高质量"],
              ["4k", "4K 终稿"],
            ]}
          />
          <SelectField
            label="张数"
            value={String(outputCount)}
            onChange={(value) => setOutputCount(coerceOutputCount(value))}
            disabled={isGenerating}
            options={OUTPUT_COUNT_SELECT_OPTIONS}
          />
        </div>
        <div className="mt-4 grid gap-x-6 gap-y-4 md:grid-cols-3">
          <SelectField
            label="场景风格"
            value={sceneStrategy}
            onChange={(value) => setSceneStrategy(value as CreateSceneStrategy)}
            disabled={isGenerating}
            options={SCENE_STRATEGY_LABELS}
          />
          <SelectField
            label="丰富度"
            value={sceneVariety}
            onChange={(value) => setSceneVariety(value as CreateSceneVariety)}
            disabled={isGenerating}
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
            disabled={isGenerating}
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
            disabled={isGenerating}
          />
          <CheckboxField
            label="允许远处路人"
            checked={allowBackgroundPeople}
            onChange={setAllowBackgroundPeople}
            disabled={isGenerating}
          />
        </div>
        <div className="mt-5 grid grid-cols-1 gap-3 min-[420px]:flex min-[420px]:flex-wrap min-[420px]:items-center">
          <Button
            variant="outline"
            loading={create.isPending}
            disabled={isGenerating}
            onClick={() => setConfirmRegenerate(true)}
            leftIcon={<Shirt className="h-4 w-4" />}
            className="w-full min-[420px]:w-auto"
          >
            继续再生成 {outputCount} 张
          </Button>
          <p className="inline-flex min-w-0 flex-wrap items-center gap-2 break-words text-[12px] leading-6 text-[var(--fg-2)]">
            <span className="inline-flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--amber-300)]">
              <Layers className="h-3 w-3" />
              追加 {String(outputCount).padStart(2, "0")} 张
            </span>
            <span aria-hidden className="text-[var(--fg-3)]">·</span>
            <span>{aspectRatio} 画幅</span>
            <span aria-hidden className="text-[var(--fg-3)]">·</span>
            <span>{quality === "4k" ? "4K 终稿" : "2K 高质量"}</span>
            <span aria-hidden className="text-[var(--fg-3)]">·</span>
            <span>GPT-5.5 场景导演</span>
          </p>
        </div>
      </section>

      <section className="border-t border-[var(--border)] py-5">
        <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
          Revise Instruction
        </p>
        <input
          value={instruction}
          onChange={(event) => setInstruction(event.target.value)}
          className="mt-3 h-10 w-full border-b border-[var(--border)] bg-transparent px-1 text-[14px] text-[var(--fg-0)] outline-none transition-colors placeholder:text-[var(--fg-3)] focus:border-[var(--amber-400)]"
        />
        <div className="mt-5 grid grid-cols-1 gap-3 min-[420px]:flex min-[420px]:flex-wrap min-[420px]:items-center">
          <Button
            variant="outline"
            loading={revise.isPending}
            disabled={isGenerating || !selectedImageId || images.length === 0}
            onClick={() =>
              revise.mutate({
                image_id: selectedImageId,
                instruction,
                scope: "full_image",
              })
            }
            leftIcon={<RefreshCw className="h-4 w-4" />}
            className="w-full min-[420px]:w-auto"
          >
            文字返修
          </Button>
          <Button
            variant="primary"
            loading={complete.isPending}
            disabled={isGenerating || images.length === 0}
            onClick={() => setConfirmDeliver(true)}
            leftIcon={<Check className="h-4 w-4" />}
            className="w-full min-[420px]:w-auto"
          >
            确认交付
          </Button>
        </div>
      </section>

      <ImagePreviewModal
        images={images}
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
        title={`继续再生成 ${outputCount} 张？`}
        description={`已生成和已质检的图会继续保留，新一轮会按当前选择的场景模板、${aspectRatio} 画幅和 ${
          quality === "4k" ? "4K 终稿" : "2K 高质量"
        } 追加生成 ${outputCount} 张。`}
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
        description="项目状态将变为已交付，所有展示图开放下载。如需修改可在交付页继续返修。"
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

function coerceOutputCount(value: unknown): CreateOutputCount {
  const numberValue = typeof value === "number" ? value : Number(value);
  return OUTPUT_COUNT_LABELS.some(([option]) => option === numberValue)
    ? (numberValue as CreateOutputCount)
    : 4;
}

function coerceSceneEnvironment(value: unknown): CreateSceneEnvironment {
  return value === "outdoor" ? "outdoor" : "indoor";
}
