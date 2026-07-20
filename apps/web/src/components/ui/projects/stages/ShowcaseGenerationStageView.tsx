import { Check, Layers, RefreshCw, Shirt } from "lucide-react";

import { Button } from "@/components/ui/primitives/Button";
import type { WorkflowRun } from "@/lib/apiClient";
import { ConfirmDialog } from "@/components/ui/primitives/ConfirmDialog";
import { ImageGrid, ReferenceBlock } from "../components/ImageGrid";
import { ImagePreviewModal } from "../components/ImagePreviewModal";
import { ShowcaseTaskProgress } from "../components/ShowcaseTaskProgress";
import { RunningState, StageFrame } from "../components/StageFrame";
import { ShowcaseSetupFields } from "./ShowcaseSetupFields";
import { sceneEnvironmentEnabled } from "./showcaseStageForm";
import type { ShowcaseGenerationStageController } from "./useShowcaseGenerationStage";

export function ShowcaseGenerationStageView({
  workflow,
  controller,
}: {
  workflow: WorkflowRun;
  controller: ShowcaseGenerationStageController;
}) {
  return (
    <StageFrame
      eyebrow="N°06 — 展示融合"
      title="商品融合"
      subtitle="使用已确认模特和商品图，生成电商展示图。可选 1/2/4/8/16 张，张数越多耗时越长。"
      badge={controller.isRunning ? <RunningBadge /> : null}
      actions={
        <Button
          variant="outline"
          size="sm"
          loading={controller.reopen.isPending}
          onClick={() => controller.setConfirmReopen(true)}
          leftIcon={<RefreshCw className="h-3.5 w-3.5" />}
          className="w-full sm:w-auto"
        >
          返回重选模特
        </Button>
      }
    >
      <StageError error={controller.stageError} />
      <ReferenceImages controller={controller} />
      <OutputSetup controller={controller} />
      <GenerationActions controller={controller} />
      <TaskProgress workflow={workflow} controller={controller} />
      <GeneratedImages controller={controller} />
      <ImagePreviewModal
        images={controller.previewList}
        index={controller.previewIndex}
        onIndexChange={controller.setPreviewIndex}
        onClose={() => controller.setPreviewIndex(-1)}
      />
      <StageDialogs controller={controller} />
    </StageFrame>
  );
}

function RunningBadge() {
  return (
    <span className="inline-flex items-center gap-2 font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--amber-300)]">
      <span className="relative flex h-1.5 w-1.5">
        <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-[var(--amber-400)] opacity-60" />
        <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-[var(--amber-400)]" />
      </span>
      Running
    </span>
  );
}

function StageError({ error }: { error: string | null }) {
  if (!error) return null;
  return (
    <section className="border-t border-[var(--border)] py-4">
      <p className="border-l-2 border-[var(--danger)] pl-3 text-[13px] leading-6 text-[var(--danger)]">
        {error}
      </p>
    </section>
  );
}

function ReferenceImages({
  controller,
}: {
  controller: ShowcaseGenerationStageController;
}) {
  return (
    <section className="grid gap-x-6 gap-y-2 lg:grid-cols-2">
      <ReferenceBlock
        title="商品原图"
        images={controller.productImages}
        onPreview={(_image, index) =>
          controller.openPreview(controller.productImages, index)
        }
      />
      <ReferenceBlock
        title="已确认模特"
        images={controller.modelImages}
        onPreview={(_image, index) =>
          controller.openPreview(controller.modelImages, index)
        }
      />
    </section>
  );
}

function OutputSetup({
  controller,
}: {
  controller: ShowcaseGenerationStageController;
}) {
  const { form } = controller;
  return (
    <section className="border-t border-[var(--border)] py-5">
      <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
        Output Setup
      </p>
      <ShowcaseSetupFields
        form={form}
        disabled={controller.isRunning}
        showSceneEnvironment={sceneEnvironmentEnabled(form.template)}
      />
      <p className="mt-4 inline-flex min-w-0 flex-wrap items-center gap-2 break-words text-[12px] leading-6 text-[var(--fg-2)]">
        <span className="inline-flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--amber-300)]">
          <Layers className="h-3 w-3" />
          {String(form.outputCount).padStart(2, "0")} 张
        </span>
        <span aria-hidden className="text-[var(--fg-3)]">·</span>
        <span>{form.aspectRatio} 画幅</span>
        <span aria-hidden className="text-[var(--fg-3)]">·</span>
        <span>{form.quality === "4k" ? "4K 终稿" : "2K 高质量"}</span>
        <span aria-hidden className="text-[var(--fg-3)]">·</span>
        <span>
          {form.sceneStrategy === "natural_series"
            ? "GPT-5.5 自然导演"
            : "GPT-5.5 场景导演"}
        </span>
        <span aria-hidden className="text-[var(--fg-3)]">·</span>
        <span className="text-[var(--fg-3)]">张数越多耗时越长</span>
      </p>
    </section>
  );
}

function GenerationActions({
  controller,
}: {
  controller: ShowcaseGenerationStageController;
}) {
  return (
    <section className="grid grid-cols-1 gap-3 border-t border-[var(--border)] py-5 min-[420px]:grid-cols-2 sm:flex sm:flex-wrap sm:items-center">
      <Button
        variant={controller.hasTasks ? "outline" : "primary"}
        loading={controller.create.isPending}
        disabled={controller.isRunning}
        onClick={controller.requestGeneration}
        leftIcon={
          controller.hasTasks ? (
            <RefreshCw className="h-4 w-4" />
          ) : (
            <Shirt className="h-4 w-4" />
          )
        }
        className="w-full sm:w-auto"
      >
        {generationButtonLabel(controller)}
      </Button>
      {controller.generated.length > 0 ? (
        <Button
          variant="primary"
          loading={controller.complete.isPending}
          disabled={controller.isRunning}
          onClick={() => controller.setConfirmDeliver(true)}
          leftIcon={<Check className="h-4 w-4" />}
          className="w-full sm:w-auto"
        >
          确认交付
        </Button>
      ) : null}
    </section>
  );
}

function generationButtonLabel(
  controller: ShowcaseGenerationStageController,
): string {
  if (controller.isRunning) return "展示图任务运行中";
  if (controller.hasTasks) {
    return `按当前模板再生成 ${controller.form.outputCount} 张`;
  }
  return `开始生成 ${controller.form.outputCount} 张展示图`;
}

function TaskProgress({
  workflow,
  controller,
}: {
  workflow: WorkflowRun;
  controller: ShowcaseGenerationStageController;
}) {
  if (!controller.hasGenerationStarted || !controller.step) return null;
  return (
    <ShowcaseTaskProgress
      workflow={workflow}
      step={controller.step}
      images={controller.generated}
    />
  );
}

function GeneratedImages({
  controller,
}: {
  controller: ShowcaseGenerationStageController;
}) {
  if (!controller.hasGenerationStarted) return null;
  return (
    <section className="border-t border-[var(--border)] py-5">
      <div className="mb-3 flex items-center justify-between gap-3">
        <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
          Generated
        </p>
        <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-3)] tabular-nums">
          {String(controller.generated.length).padStart(2, "0")} 张
        </p>
      </div>
      {controller.generated.length === 0 ? (
        <RunningState label="展示图正在生成…" />
      ) : (
        <ImageGrid
          images={controller.generated}
          onPreview={(_image, index) =>
            controller.openPreview(controller.generated, index)
          }
        />
      )}
    </section>
  );
}

function StageDialogs({
  controller,
}: {
  controller: ShowcaseGenerationStageController;
}) {
  const { form } = controller;
  return (
    <>
      <ConfirmDialog
        open={controller.confirmReopen}
        onOpenChange={controller.setConfirmReopen}
        title="返回重选模特？"
        description="将放弃当前展示图与质检结果，回到模特候选阶段。"
        confirmText="返回重选"
        tone="danger"
        confirming={controller.reopen.isPending}
        onConfirm={controller.confirmModelReopen}
      />
      <ConfirmDialog
        open={controller.confirmRegenerate}
        onOpenChange={controller.setConfirmRegenerate}
        title={`再生成 ${form.outputCount} 张展示图？`}
        description={`已生成的成品会继续保留，新一轮会按当前选择的模板、${form.aspectRatio} 画幅和 ${
          form.quality === "4k" ? "4K 终稿" : "2K 高质量"
        } 模式追加生成 ${form.outputCount} 张。`}
        confirmText="追加生成"
        confirming={controller.create.isPending}
        onConfirm={controller.confirmRegeneration}
      />
      <ConfirmDialog
        open={controller.confirmDeliver}
        onOpenChange={controller.setConfirmDeliver}
        title="确认交付项目？"
        description="项目状态将变为已交付，当前成品图开放下载。"
        confirmText="确认交付"
        confirming={controller.complete.isPending}
        onConfirm={controller.confirmDelivery}
      />
    </>
  );
}
