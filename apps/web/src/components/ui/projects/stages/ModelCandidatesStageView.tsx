import { Library, Shirt, Sparkles } from "lucide-react";

import { Button } from "@/components/ui/primitives/Button";
import { ConfirmDialog } from "@/components/ui/primitives/ConfirmDialog";
import type { WorkflowRun } from "@/lib/apiClient";
import { CandidateCard } from "../components/CandidateCard";
import { ImagePreviewModal } from "../components/ImagePreviewModal";
import { ModelLibraryDialog } from "../components/ModelLibraryDialog";
import { SaveCandidateDialog } from "../components/SaveCandidateDialog";
import {
  SelectableImageGrid,
  SelectableImageGridLoading,
} from "../components/SelectableImageGrid";
import { RunningState, StageFrame } from "../components/StageFrame";
import { ShowcaseSetupFields } from "./ShowcaseSetupFields";
import type { ModelCandidatesStageController } from "./useModelCandidatesStage";

export function ModelCandidatesStageView({
  workflow,
  controller,
}: {
  workflow: WorkflowRun;
  controller: ModelCandidatesStageController;
}) {
  return (
    <StageFrame
      eyebrow="N°04 — 模特候选"
      title="模特候选"
      subtitle="每套候选是同一个合成模特的四视图概念图。确认模特后继续生成并选择配饰四宫格。"
      actions={<StageActions controller={controller} />}
    >
      <StageError error={controller.stageError} />
      <CandidatesSection workflow={workflow} controller={controller} />
      <AdjustmentsSection controller={controller} />
      <AccessorySection controller={controller} />
      <ShowcaseSetupSection controller={controller} />
      <ImagePreviewModal
        images={controller.previewList}
        index={controller.previewIndex}
        onIndexChange={controller.setPreviewIndex}
        onClose={() => controller.setPreviewIndex(-1)}
      />
      <StageDialogs workflow={workflow} controller={controller} />
    </StageFrame>
  );
}

function StageActions({
  controller,
}: {
  controller: ModelCandidatesStageController;
}) {
  return (
    <div className="grid grid-cols-1 gap-2 min-[420px]:grid-cols-2 sm:flex sm:flex-row">
      <Button
        variant="outline"
        size="sm"
        disabled={Boolean(controller.selectedCandidate)}
        onClick={() => controller.setLibraryOpen(true)}
        leftIcon={<Library className="h-3.5 w-3.5" />}
        className="w-full sm:w-auto"
      >
        打开模特库
      </Button>
      <Button
        variant="outline"
        size="sm"
        loading={controller.createCandidates.isPending}
        disabled={
          Boolean(controller.selectedCandidate) ||
          controller.candidateGenerationRunning
        }
        onClick={controller.regenerateCandidates}
        leftIcon={<Sparkles className="h-3.5 w-3.5" />}
        className="w-full sm:w-auto"
      >
        再生成候选
      </Button>
    </div>
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

function CandidatesSection({
  workflow,
  controller,
}: {
  workflow: WorkflowRun;
  controller: ModelCandidatesStageController;
}) {
  return (
    <section className="border-t border-[var(--border)] py-5">
      <div className="mb-3 flex items-center justify-between gap-3">
        <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
          候选方案
        </p>
        <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-3)] tabular-nums">
          {String(controller.candidates.length).padStart(2, "0")} 张
        </p>
      </div>
      {controller.candidates.length === 0 ? (
        <RunningState label="等待创建模特候选" />
      ) : (
        <div className="grid gap-x-5 gap-y-8 md:grid-cols-2 xl:grid-cols-3">
          {controller.candidates.map((candidate) => (
            <CandidateCard
              key={candidate.id}
              workflow={workflow}
              candidate={candidate}
              approving={controller.approve.isPending}
              locallySelected={
                controller.chosenCandidate?.id === candidate.id &&
                candidate.status !== "selected"
              }
              onPreview={controller.openPreview}
              onChoose={() => controller.setChosenCandidateId(candidate.id)}
              onApprove={controller.approveChosenCandidate}
              onSaveToLibrary={() =>
                controller.setSavingCandidateId(candidate.id)
              }
              savingToLibrary={controller.savingCandidateId === candidate.id}
            />
          ))}
        </div>
      )}
    </section>
  );
}

function AdjustmentsSection({
  controller,
}: {
  controller: ModelCandidatesStageController;
}) {
  return (
    <section className="border-t border-[var(--border)] py-4">
      <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
        Adjustments
      </p>
      <div className="mt-3 grid gap-3 md:grid-cols-[minmax(0,1fr)_auto] md:items-end">
        <input
          value={controller.adjustments}
          onChange={(event) => controller.setAdjustments(event.target.value)}
          placeholder="发型再自然一点，保留脸和身材比例"
          className="h-10 w-full border-b border-[var(--border)] bg-transparent px-1 text-[14px] text-[var(--fg-0)] outline-none transition-colors placeholder:text-[var(--fg-3)] focus:border-[var(--amber-400)]"
        />
        <Button
          variant="primary"
          loading={controller.approve.isPending}
          disabled={
            !controller.chosenCandidate ||
            Boolean(controller.selectedCandidate)
          }
          onClick={controller.approveChosenCandidate}
          className="w-full md:w-auto"
        >
          确认模特并继续
        </Button>
      </div>
      <p className="mt-3 min-w-0 break-words font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-3)]">
        配饰方向 ·{" "}
        <span className="text-[var(--fg-1)] normal-case tracking-normal">
          {controller.accessoryPlan.enabled
            ? controller.accessoryItems.join("、") || "自动推荐"
            : "已关闭"}
        </span>
      </p>
    </section>
  );
}

function AccessorySection({
  controller,
}: {
  controller: ModelCandidatesStageController;
}) {
  if (!controller.accessoryPlan.enabled) return null;
  return (
    <section className="border-t border-[var(--border)] py-4">
      <div className="mb-3 flex items-center justify-between gap-3">
        <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
          Accessory Quad
        </p>
        {controller.accessoryImages.length > 0 ? (
          <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-3)] tabular-nums">
            {String(controller.accessoryImages.length).padStart(2, "0")} 张
          </p>
        ) : null}
      </div>
      <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_auto] md:items-end">
        <input
          value={controller.accessoryPrompt}
          onChange={(event) =>
            controller.setAccessoryPrompt(event.target.value)
          }
          placeholder={
            controller.accessoryItems.join("、") ||
            "例如：简洁耳饰、浅色鞋子、小号包袋"
          }
          className="h-10 w-full border-b border-[var(--border)] bg-transparent px-1 text-[14px] text-[var(--fg-0)] outline-none transition-colors placeholder:text-[var(--fg-3)] focus:border-[var(--amber-400)]"
        />
        <Button
          variant="outline"
          loading={controller.createAccessoryPreviews.isPending}
          disabled={
            !controller.selectedCandidate ||
            controller.accessoryPreviewRunning
          }
          onClick={controller.generateAccessoryPreview}
          className="w-full md:w-auto"
        >
          {accessoryButtonLabel(controller)}
        </Button>
      </div>
      <div className="mt-4">
        <AccessoryGrid controller={controller} />
      </div>
    </section>
  );
}

function accessoryButtonLabel(
  controller: ModelCandidatesStageController,
): string {
  if (controller.accessoryPreviewRunning) return "生成中";
  if (controller.accessoryImages.length > 0) return "再生成";
  return "生成四宫格";
}

function AccessoryGrid({
  controller,
}: {
  controller: ModelCandidatesStageController;
}) {
  if (controller.accessoryPreviewRunning) {
    return <SelectableImageGridLoading count={1} label="配饰四宫格生成中" />;
  }
  if (controller.accessoryImages.length > 0) {
    return (
      <SelectableImageGrid
        images={controller.accessoryImages}
        selectedImageId={controller.selectedAccessoryImageId}
        saving={controller.saveAccessorySelection.isPending}
        onSelect={controller.selectAccessoryImage}
        onPreview={(image, index) =>
          controller.openPreview(image, controller.accessoryImages, index)
        }
      />
    );
  }
  return (
    <RunningState
      label={
        controller.selectedCandidate
          ? "配饰四宫格尚未生成，点击上方按钮开始生成"
          : "确认模特后可生成配饰四宫格"
      }
    />
  );
}

function ShowcaseSetupSection({
  controller,
}: {
  controller: ModelCandidatesStageController;
}) {
  if (!controller.selectedCandidate) return null;
  return (
    <section className="border-t border-[var(--border)] py-5">
      <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
        Showcase Setup
      </p>
      <ShowcaseSetupFields
        form={controller.form}
        disabled={
          controller.createShowcase.isPending ||
          controller.isShowcaseRunning
        }
      />
      <p className="mt-2 text-[12px] text-[var(--fg-3)]">张数越多耗时越长</p>
      <Button
        className="mt-5 w-full sm:w-auto"
        variant="primary"
        loading={controller.createShowcase.isPending}
        disabled={controller.isShowcaseRunning}
        onClick={controller.requestShowcaseGeneration}
        leftIcon={<Shirt className="h-4 w-4" />}
      >
        {controller.showcaseHasTasks
          ? "按当前方案再生成一批"
          : "开始生成展示图"}
      </Button>
    </section>
  );
}

function StageDialogs({
  workflow,
  controller,
}: {
  workflow: WorkflowRun;
  controller: ModelCandidatesStageController;
}) {
  return (
    <>
      <ModelLibraryDialog
        key={`${workflow.id}:${controller.defaultAgeSegment}:candidate-stage`}
        open={controller.libraryOpen}
        workflow={workflow}
        defaultAgeSegment={controller.defaultAgeSegment}
        onClose={() => controller.setLibraryOpen(false)}
        generatingCandidates={controller.createCandidates.isPending}
        selectionAccessoryPlan={controller.accessoryPlan}
        selectionStylePrompt={controller.modelStylePrompt}
        onGenerateCandidates={controller.generateCandidatesFromLibrary}
      />
      <SaveCandidateDialog
        key={controller.savingCandidateId ?? "closed"}
        workflow={workflow}
        candidate={
          controller.candidates.find(
            (candidate) => candidate.id === controller.savingCandidateId,
          ) ?? null
        }
        open={Boolean(controller.savingCandidateId)}
        onOpenChange={(open) => {
          if (!open) controller.setSavingCandidateId(null);
        }}
      />
      <ConfirmDialog
        open={controller.confirmRegenerate}
        onOpenChange={controller.setConfirmRegenerate}
        title="再生成一批展示图？"
        description={`已生成的成品会继续保留，新一轮会按当前选择的模板、${controller.form.aspectRatio} 画幅和 ${
          controller.form.quality === "4k" ? "4K 终稿" : "2K 高质量"
        } 模式追加生成 ${controller.form.outputCount} 张。`}
        confirmText="追加生成"
        tone="default"
        confirming={controller.createShowcase.isPending}
        onConfirm={controller.confirmShowcaseGeneration}
      />
    </>
  );
}
