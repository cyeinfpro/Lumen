"use client";

// 模特候选 + 方案确认 阶段（editorial 重构）。
// 关键改进：
// 1) 确认模特后，可生成带配饰的模特四宫格参考图；这里选择饰品参考图
// 2) 表单值持久化在 useState（页面跳转后仍保留输入）
// 3) showcase 重生成走 ConfirmDialog 兜底（已有 task 时点击=重新生成）
// 4) 模板/质量切换在生成中禁用
// 5) 视觉：hairline 段落 + mono eyebrow + underline 输入 + dot toggle，去除嵌套卡。

import { Shirt } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/primitives/Button";
import { ConfirmDialog } from "@/components/ui/primitives/ConfirmDialog";
import { toast } from "@/components/ui/primitives/Toast";
import {
  useApproveModelCandidateMutation,
  useCreateAccessoryPreviewsMutation,
  useCreateShowcaseImagesMutation,
  useSaveAccessorySelectionMutation,
} from "@/lib/queries";
import type { BackendImageMeta, WorkflowRun } from "@/lib/apiClient";
import { CandidateCard } from "../components/CandidateCard";
import { ImagePreviewModal } from "../components/ImagePreviewModal";
import { SaveCandidateDialog } from "../components/SaveCandidateDialog";
import {
  SelectableImageGrid,
  SelectableImageGridLoading,
} from "../components/SelectableImageGrid";
import { RunningState, StageFrame } from "../components/StageFrame";
import {
  ASPECT_RATIO_LABELS,
  SHOT_PLAN_DEFAULT,
  TEMPLATE_LABELS,
  type CreateAspectRatio,
  type CreateTemplate,
} from "../types";
import { imageById, stepOf, stringArray, stringValue } from "../utils";

export function ModelCandidatesStage({ workflow }: { workflow: WorkflowRun }) {
  const approve = useApproveModelCandidateMutation(workflow.id, {
    onError: (err) =>
      toast.error("确认模特失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      }),
  });
  const saveAccessorySelection = useSaveAccessorySelectionMutation(workflow.id, {
    onError: (err) =>
      toast.error("保存配饰四宫格选择失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      }),
  });
  const createShowcase = useCreateShowcaseImagesMutation(workflow.id, {
    onError: (err) =>
      toast.error("生成展示图失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      }),
    onSuccess: () => toast.success("展示图任务已派发"),
  });
  const createAccessoryPreviews = useCreateAccessoryPreviewsMutation(workflow.id, {
    onError: (err) =>
      toast.error("生成配饰四宫格失败", {
        description: err instanceof Error ? err.message : "请先确认模特后再重新生成配饰四宫格",
      }),
    onSuccess: () => toast.success("配饰四宫格任务已派发"),
  });

  const showcaseStep = stepOf(workflow, "showcase_generation");
  const initialTemplate = coerceTemplate(showcaseStep?.input_json?.template);
  const initialAspectRatio = coerceAspectRatio(showcaseStep?.input_json?.aspect_ratio);
  const initialQuality = coerceQuality(showcaseStep?.input_json?.final_quality);

  const [adjustments, setAdjustments] = useState("");
  const [template, setTemplate] = useState<CreateTemplate>(initialTemplate);
  const [aspectRatio, setAspectRatio] = useState<CreateAspectRatio>(initialAspectRatio);
  const [quality, setQuality] = useState<"high" | "4k">(initialQuality);
  const currentConfigKey = `${initialTemplate}:${initialAspectRatio}:${initialQuality}`;
  const [trackedConfigKey, setTrackedConfigKey] = useState(currentConfigKey);
  const [accessoryPrompt, setAccessoryPrompt] = useState("");
  const [previewList, setPreviewList] = useState<BackendImageMeta[]>([]);
  const [previewIndex, setPreviewIndex] = useState(-1);
  const [confirmRegenerate, setConfirmRegenerate] = useState(false);
  const [chosenCandidateId, setChosenCandidateId] = useState<string | null>(null);
  const [savingCandidateId, setSavingCandidateId] = useState<string | null>(null);

  const candidates = workflow.model_candidates;
  const approvalStep = stepOf(workflow, "model_approval");
  const selectedCandidate = candidates.find((candidate) => candidate.status === "selected");
  const chosenCandidate =
    selectedCandidate ?? candidates.find((candidate) => candidate.id === chosenCandidateId);
  const isShowcaseRunning = showcaseStep?.status === "running";
  if (!isShowcaseRunning && trackedConfigKey !== currentConfigKey) {
    setTrackedConfigKey(currentConfigKey);
    setTemplate(initialTemplate);
    setAspectRatio(initialAspectRatio);
    setQuality(initialQuality);
  }
  const accessoryPlan = approvalStep?.input_json?.accessory_plan;
  const accessoryEnabled =
    typeof accessoryPlan === "object" &&
    accessoryPlan !== null &&
    "enabled" in accessoryPlan &&
    accessoryPlan.enabled === false
      ? false
      : true;
  const accessoryItems =
    typeof accessoryPlan === "object" && accessoryPlan !== null && "items" in accessoryPlan
      ? stringArray(accessoryPlan.items)
      : [];

  const persistedAccessoryId =
    stringValue(approvalStep?.input_json?.selected_accessory_image_id) ??
    stringValue(approvalStep?.output_json?.selected_accessory_image_id);
  // render-phase reset：后端返回的最新值变化时同步本地选中态，
  // 避免轮询写回旧值。useState + 比对模式比 useEffect 少一次渲染。
  const [selectedAccessoryImageId, setSelectedAccessoryImageId] = useState<string | null>(
    persistedAccessoryId,
  );
  const [trackedPersisted, setTrackedPersisted] = useState(persistedAccessoryId);
  if (trackedPersisted !== persistedAccessoryId) {
    setTrackedPersisted(persistedAccessoryId);
    setSelectedAccessoryImageId(persistedAccessoryId);
  }

  const accessoryImages = (approvalStep?.image_ids ?? [])
    .map((imageId) => imageById(workflow, imageId))
    .filter((image): image is BackendImageMeta => Boolean(image));
  const accessoryPreviewRunning =
    createAccessoryPreviews.isPending ||
    (approvalStep?.status === "running" && (approvalStep.task_ids?.length ?? 0) > 0);

  const openPreview = (image: BackendImageMeta, list: BackendImageMeta[], index: number) => {
    setPreviewList(list);
    setPreviewIndex(index);
  };

  const triggerCreateShowcase = () => {
    createShowcase.mutate({
      template,
      shot_plan: [...SHOT_PLAN_DEFAULT],
      aspect_ratio: aspectRatio,
      final_quality: quality,
      output_count: 4,
    });
  };

  const onClickGenerateShowcase = () => {
    if (showcaseStep?.task_ids?.length) {
      setConfirmRegenerate(true);
    } else {
      triggerCreateShowcase();
    }
  };

  const generateAccessoryPreview = () => {
    if (!selectedCandidate) return;
    createAccessoryPreviews.mutate({
      candidate_id: selectedCandidate.id,
      accessory_plan: {
        enabled: accessoryEnabled,
        items: accessoryItems,
        strength: "subtle",
      },
      style_prompt: accessoryPrompt,
    });
  };

  const approveChosenCandidate = () => {
    if (!chosenCandidate) return;
    approve.mutate({
      candidate_id: chosenCandidate.id,
      adjustments,
      accessory_plan: {
        enabled: accessoryEnabled,
        items: accessoryItems,
        strength: "subtle",
      },
      selected_accessory_image_id: selectedAccessoryImageId,
    });
  };

  return (
    <StageFrame
      eyebrow="N°04 — Model Candidates"
      title="模特候选"
      subtitle="每套候选是同一个合成模特的四视图概念图。确认模特后继续生成并选择配饰四宫格。"
    >
      <section className="border-t border-[var(--border)] py-5">
        <div className="mb-3 flex items-center justify-between gap-3">
          <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
            Candidates
          </p>
          <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-3)] tabular-nums">
            {String(candidates.length).padStart(2, "0")} / 03
          </p>
        </div>
        {candidates.length === 0 ? (
          <RunningState label="等待创建模特候选" />
        ) : (
          <div className="grid gap-x-5 gap-y-8 md:grid-cols-2 xl:grid-cols-3">
            {candidates.map((candidate) => (
              <CandidateCard
                key={candidate.id}
                workflow={workflow}
                candidate={candidate}
                approving={approve.isPending}
                locallySelected={
                  chosenCandidate?.id === candidate.id && candidate.status !== "selected"
                }
                onPreview={(image, list, index) => openPreview(image, list, index)}
                onChoose={() => setChosenCandidateId(candidate.id)}
                onApprove={approveChosenCandidate}
                onSaveToLibrary={() => setSavingCandidateId(candidate.id)}
                savingToLibrary={savingCandidateId === candidate.id}
              />
            ))}
          </div>
        )}
      </section>

      <section className="border-t border-[var(--border)] py-4">
        <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
          Adjustments
        </p>
        <div className="mt-3 grid gap-3 md:grid-cols-[minmax(0,1fr)_auto] md:items-end">
          <input
            value={adjustments}
            onChange={(event) => setAdjustments(event.target.value)}
            placeholder="发型再自然一点，保留脸和身材比例"
            className="h-10 w-full border-b border-[var(--border)] bg-transparent px-1 text-[14px] text-[var(--fg-0)] outline-none transition-colors placeholder:text-[var(--fg-3)] focus:border-[var(--amber-400)]"
          />
          <Button
            variant="primary"
            loading={approve.isPending}
            disabled={!chosenCandidate || Boolean(selectedCandidate)}
            onClick={approveChosenCandidate}
          >
            确认模特并继续
          </Button>
        </div>
        <p className="mt-3 font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-3)]">
          配饰方向 ·{" "}
          <span className="text-[var(--fg-1)] normal-case tracking-normal">
            {accessoryEnabled ? accessoryItems.join("、") || "自动推荐" : "已关闭"}
          </span>
        </p>
      </section>

      {accessoryEnabled ? (
        <section className="border-t border-[var(--border)] py-4">
          <div className="mb-3 flex items-center justify-between gap-3">
            <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
              Accessory Quad
            </p>
            {accessoryImages.length > 0 ? (
              <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-3)] tabular-nums">
                {String(accessoryImages.length).padStart(2, "0")} shots
              </p>
            ) : null}
          </div>
          <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_auto] md:items-end">
            <input
              value={accessoryPrompt}
              onChange={(event) => setAccessoryPrompt(event.target.value)}
              placeholder={accessoryItems.join("、") || "例如：简洁耳饰、浅色鞋子、小号包袋"}
              className="h-10 w-full border-b border-[var(--border)] bg-transparent px-1 text-[14px] text-[var(--fg-0)] outline-none transition-colors placeholder:text-[var(--fg-3)] focus:border-[var(--amber-400)]"
            />
            <Button
              variant="outline"
              loading={createAccessoryPreviews.isPending}
              disabled={!selectedCandidate || accessoryPreviewRunning}
              onClick={generateAccessoryPreview}
            >
              {accessoryPreviewRunning
                ? "生成中"
                : accessoryImages.length > 0
                  ? "再生成"
                  : "生成四宫格"}
            </Button>
          </div>
          <div className="mt-4">
            {accessoryPreviewRunning ? (
              <SelectableImageGridLoading count={1} label="配饰四宫格生成中" />
            ) : accessoryImages.length > 0 ? (
              <SelectableImageGrid
                images={accessoryImages}
                selectedImageId={selectedAccessoryImageId}
                saving={saveAccessorySelection.isPending}
                onSelect={(imageId) => {
                  setSelectedAccessoryImageId(imageId);
                  saveAccessorySelection.mutate({
                    selected_accessory_image_id: imageId,
                  });
                }}
                onPreview={(image, index) => openPreview(image, accessoryImages, index)}
              />
            ) : selectedCandidate ? (
              <RunningState label="配饰四宫格尚未生成，点击上方按钮开始生成" />
            ) : (
              <RunningState label="确认模特后可生成配饰四宫格" />
            )}
          </div>
        </section>
      ) : null}

      {selectedCandidate ? (
        <section className="border-t border-[var(--border)] py-5">
          <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
            Showcase Setup
          </p>
          <div className="mt-3 grid gap-x-6 gap-y-4 md:grid-cols-3">
            <SelectField
              label="输出模板"
              value={template}
              onChange={(value) => setTemplate(value as CreateTemplate)}
              disabled={createShowcase.isPending || isShowcaseRunning}
              options={TEMPLATE_LABELS}
            />
            <SelectField
              label="画幅比例"
              value={aspectRatio}
              onChange={(value) => setAspectRatio(value as CreateAspectRatio)}
              disabled={createShowcase.isPending || isShowcaseRunning}
              options={ASPECT_RATIO_LABELS}
            />
            <SelectField
              label="质量模式"
              value={quality}
              onChange={(value) => setQuality(value as "high" | "4k")}
              disabled={createShowcase.isPending || isShowcaseRunning}
              options={[
                ["high", "2K 高质量"],
                ["4k", "4K 终稿"],
              ]}
            />
          </div>
          <Button
            className="mt-5"
            variant="primary"
            loading={createShowcase.isPending}
            disabled={isShowcaseRunning}
            onClick={onClickGenerateShowcase}
            leftIcon={<Shirt className="h-4 w-4" />}
          >
            {showcaseStep?.task_ids?.length ? "按当前方案再生成一批" : "开始生成展示图"}
          </Button>
        </section>
      ) : null}

      <ImagePreviewModal
        images={previewList}
        index={previewIndex}
        onIndexChange={setPreviewIndex}
        onClose={() => setPreviewIndex(-1)}
      />
      {/* key 让 dialog 在 open / candidate 变化时 re-mount，state 自动重置；
          这是 React 19 推荐的派生 state 做法（替代 effect 中 setState）。 */}
      <SaveCandidateDialog
        key={savingCandidateId ?? "closed"}
        workflow={workflow}
        candidate={candidates.find((candidate) => candidate.id === savingCandidateId) ?? null}
        open={Boolean(savingCandidateId)}
        onOpenChange={(open) => {
          if (!open) setSavingCandidateId(null);
        }}
      />

      <ConfirmDialog
        open={confirmRegenerate}
        onOpenChange={setConfirmRegenerate}
        title="再生成一批展示图？"
        description={`已生成的成品会继续保留，新一轮会按当前选择的模板、${aspectRatio} 画幅和 ${
          quality === "4k" ? "4K 终稿" : "2K 高质量"
        } 模式追加生成 4 张。`}
        confirmText="追加生成"
        tone="default"
        confirming={createShowcase.isPending}
        onConfirm={async () => {
          triggerCreateShowcase();
          setConfirmRegenerate(false);
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
