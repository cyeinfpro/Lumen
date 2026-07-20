import { Check, Layers, RefreshCw, Shirt } from "lucide-react";

import { Button } from "@/components/ui/primitives/Button";
import { ConfirmDialog } from "@/components/ui/primitives/ConfirmDialog";
import type { BackendImageMeta } from "@/lib/apiClient";
import { ImagePreviewModal } from "../components/ImagePreviewModal";
import { ResultImageCard } from "../components/ResultImageCard";
import { RunningState, StageFrame } from "../components/StageFrame";
import { ShowcaseSetupFields } from "./ShowcaseSetupFields";
import type { QualityReviewStageController } from "./useQualityReviewStage";

export function QualityReviewStageView({
  controller,
}: {
  controller: QualityReviewStageController;
}) {
  return (
    <StageFrame
      eyebrow="N°07 — 质量复核"
      title="质检返修"
      subtitle="每张展示图都有质检结论。可文字返修，也可调整场景、比例、分辨率和张数继续追加生成。"
      actions={
        <Button
          variant="outline"
          size="sm"
          loading={controller.reopen.isPending}
          disabled={controller.isGenerating}
          onClick={() => controller.setConfirmReopen(true)}
          leftIcon={<RefreshCw className="h-3.5 w-3.5" />}
        >
          重选模特
        </Button>
      }
    >
      <StageError error={controller.stageError} />
      <ReviewImages controller={controller} />
      <ContinueGenerating controller={controller} />
      <ReviseAndDeliver controller={controller} />
      <ImagePreviewModal
        images={controller.images}
        index={controller.previewIndex}
        onIndexChange={controller.setPreviewIndex}
        onClose={() => controller.setPreviewIndex(-1)}
      />
      <ReviewDialogs controller={controller} />
    </StageFrame>
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

function ReviewImages({
  controller,
}: {
  controller: QualityReviewStageController;
}) {
  return (
    <section className="border-t border-[var(--border)] py-5">
      <div className="mb-3 flex items-center justify-between gap-3">
        <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
          Showcases
        </p>
        <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-3)] tabular-nums">
          <span className="text-[var(--success)]">
            {String(controller.approveCount).padStart(2, "0")}
          </span>
          <span className="mx-1.5 text-[var(--fg-3)]">·</span>
          <span className="text-[var(--danger)]">
            {String(controller.reviseCount).padStart(2, "0")}
          </span>
          <span className="mx-1.5 text-[var(--fg-3)]">·</span>
          <span>{String(controller.images.length).padStart(2, "0")}</span>
        </p>
      </div>
      {controller.images.length === 0 ? (
        <RunningState label="等待展示图完成…" />
      ) : (
        <div className="grid gap-x-4 gap-y-6 md:grid-cols-2 xl:grid-cols-4">
          {controller.images.map((image: BackendImageMeta, index) => (
            <ResultImageCard
              key={image.id}
              image={image}
              report={controller.reportsByImage.get(image.id)}
              selected={controller.selectedImageId === image.id}
              onSelect={() => controller.setSelectedImageId(image.id)}
              onPreview={() => controller.setPreviewIndex(index)}
            />
          ))}
        </div>
      )}
    </section>
  );
}

function ContinueGenerating({
  controller,
}: {
  controller: QualityReviewStageController;
}) {
  const { form } = controller;
  return (
    <section className="border-t border-[var(--border)] py-5">
      <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
        Continue Generating
      </p>
      <ShowcaseSetupFields
        form={form}
        disabled={controller.isGenerating}
        templateLabel="场景模板"
        qualityLabel="分辨率"
      />
      <div className="mt-5 grid grid-cols-1 gap-3 min-[420px]:flex min-[420px]:flex-wrap min-[420px]:items-center">
        <Button
          variant="outline"
          loading={controller.create.isPending}
          disabled={controller.isGenerating}
          onClick={() => controller.setConfirmRegenerate(true)}
          leftIcon={<Shirt className="h-4 w-4" />}
          className="w-full min-[420px]:w-auto"
        >
          继续再生成 {form.outputCount} 张
        </Button>
        <p className="inline-flex min-w-0 flex-wrap items-center gap-2 break-words text-[12px] leading-6 text-[var(--fg-2)]">
          <span className="inline-flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--amber-300)]">
            <Layers className="h-3 w-3" />
            追加 {String(form.outputCount).padStart(2, "0")} 张
          </span>
          <span aria-hidden className="text-[var(--fg-3)]">·</span>
          <span>{form.aspectRatio} 画幅</span>
          <span aria-hidden className="text-[var(--fg-3)]">·</span>
          <span>{form.quality === "4k" ? "4K 终稿" : "2K 高质量"}</span>
          <span aria-hidden className="text-[var(--fg-3)]">·</span>
          <span>GPT-5.5 场景导演</span>
        </p>
      </div>
    </section>
  );
}

function ReviseAndDeliver({
  controller,
}: {
  controller: QualityReviewStageController;
}) {
  return (
    <section className="border-t border-[var(--border)] py-5">
      <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
        Revise Instruction
      </p>
      <input
        value={controller.instruction}
        onChange={(event) => controller.setInstruction(event.target.value)}
        className="mt-3 h-10 w-full border-b border-[var(--border)] bg-transparent px-1 text-[14px] text-[var(--fg-0)] outline-none transition-colors placeholder:text-[var(--fg-3)] focus:border-[var(--amber-400)]"
      />
      <div className="mt-5 grid grid-cols-1 gap-3 min-[420px]:flex min-[420px]:flex-wrap min-[420px]:items-center">
        <Button
          variant="outline"
          loading={controller.revise.isPending}
          disabled={
            controller.isGenerating ||
            !controller.selectedImageId ||
            controller.images.length === 0
          }
          onClick={controller.reviseSelectedImage}
          leftIcon={<RefreshCw className="h-4 w-4" />}
          className="w-full min-[420px]:w-auto"
        >
          文字返修
        </Button>
        <Button
          variant="primary"
          loading={controller.complete.isPending}
          disabled={controller.isGenerating || controller.images.length === 0}
          onClick={() => controller.setConfirmDeliver(true)}
          leftIcon={<Check className="h-4 w-4" />}
          className="w-full min-[420px]:w-auto"
        >
          确认交付
        </Button>
      </div>
    </section>
  );
}

function ReviewDialogs({
  controller,
}: {
  controller: QualityReviewStageController;
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
        title={`继续再生成 ${form.outputCount} 张？`}
        description={`已生成和已质检的图会继续保留，新一轮会按当前选择的场景模板、${form.aspectRatio} 画幅和 ${
          form.quality === "4k" ? "4K 终稿" : "2K 高质量"
        } 追加生成 ${form.outputCount} 张。`}
        confirmText="追加生成"
        confirming={controller.create.isPending}
        onConfirm={controller.confirmGeneration}
      />
      <ConfirmDialog
        open={controller.confirmDeliver}
        onOpenChange={controller.setConfirmDeliver}
        title="确认交付项目？"
        description="项目状态将变为已交付，所有展示图开放下载。如需修改可在交付页继续返修。"
        confirmText="确认交付"
        confirming={controller.complete.isPending}
        onConfirm={controller.confirmDelivery}
      />
    </>
  );
}
