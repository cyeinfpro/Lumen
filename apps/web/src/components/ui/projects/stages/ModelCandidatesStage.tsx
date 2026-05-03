"use client";

// 模特候选 + 方案确认 阶段。
// 关键改进：
// 1) 选定候选后再决定饰品方案；饰品图选中态由 SelectableImageGrid 维持
// 2) 表单值持久化在 useState（页面跳转后仍保留输入）
// 3) showcase 重生成走 ConfirmDialog 兜底（已有 task 时点击=重新生成）
// 4) 模板/质量切换在生成中禁用

import { Shirt, Sparkles } from "lucide-react";
import { useMemo, useState } from "react";

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
import { SelectableImageGrid } from "../components/SelectableImageGrid";
import { RunningState, StageFrame } from "../components/StageFrame";
import { SHOT_PLAN_DEFAULT, TEMPLATE_LABELS, type CreateTemplate } from "../types";
import { accessorySuggestionText, imageById, stepOf, stringValue } from "../utils";

export function ModelCandidatesStage({ workflow }: { workflow: WorkflowRun }) {
  const approve = useApproveModelCandidateMutation(workflow.id, {
    onError: (err) =>
      toast.error("确认模特失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      }),
  });
  const accessoryPreview = useCreateAccessoryPreviewsMutation(workflow.id, {
    onError: (err) =>
      toast.error("生成饰品预览失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      }),
    onSuccess: () => toast.success("饰品预览已派发"),
  });
  const saveAccessorySelection = useSaveAccessorySelectionMutation(workflow.id, {
    onError: (err) =>
      toast.error("保存饰品选择失败", {
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

  const [adjustments, setAdjustments] = useState("");
  const [accessoryEnabled, setAccessoryEnabled] = useState(true);
  const suggestedAccessories = accessorySuggestionText(workflow);
  const [accessories, setAccessories] = useState(
    suggestedAccessories || "简洁鞋子、小巧发饰、轻量包袋",
  );
  const [template, setTemplate] = useState<CreateTemplate>("premium_studio");
  const [quality, setQuality] = useState<"high" | "4k">("high");
  const [previewList, setPreviewList] = useState<BackendImageMeta[]>([]);
  const [previewIndex, setPreviewIndex] = useState(-1);
  const [confirmRegenerate, setConfirmRegenerate] = useState(false);

  const candidates = workflow.model_candidates;
  const approvalStep = stepOf(workflow, "model_approval");
  const showcaseStep = stepOf(workflow, "showcase_generation");
  const selectedCandidate = candidates.find((candidate) => candidate.status === "selected");

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

  const accessoryImages = useMemo(
    () =>
      (approvalStep?.image_ids ?? [])
        .map((imageId) => imageById(workflow, imageId))
        .filter((image): image is BackendImageMeta => Boolean(image)),
    [approvalStep?.image_ids, workflow],
  );

  const openPreview = (image: BackendImageMeta, list: BackendImageMeta[], index: number) => {
    setPreviewList(list);
    setPreviewIndex(index);
  };

  const triggerCreateShowcase = () => {
    createShowcase.mutate({
      template,
      shot_plan: [...SHOT_PLAN_DEFAULT],
      aspect_ratio: "4:5",
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

  return (
    <StageFrame
      title="模特候选"
      subtitle="每套候选是同一个合成模特的四宫格概念图。先确认模特，再决定饰品方案与最终模板。"
    >
      {candidates.length === 0 ? (
        <RunningState label="等待创建模特候选" />
      ) : (
        <div className="grid gap-3 xl:grid-cols-3">
          {candidates.map((candidate) => (
            <CandidateCard
              key={candidate.id}
              workflow={workflow}
              candidate={candidate}
              approving={approve.isPending}
              onPreview={(image, list, index) => openPreview(image, list, index)}
              onApprove={() =>
                approve.mutate({
                  candidate_id: candidate.id,
                  adjustments,
                  accessory_plan: {
                    enabled: accessoryEnabled,
                    items: accessories
                      .split(/[,，、]/)
                      .map((item) => item.trim())
                      .filter(Boolean),
                    strength: "subtle",
                  },
                  selected_accessory_image_id: selectedAccessoryImageId,
                })
              }
            />
          ))}
        </div>
      )}

      <div className="mt-4 grid gap-3 md:grid-cols-2">
        <label className="block">
          <span className="text-sm text-[var(--fg-1)]">一次文字微调</span>
          <input
            value={adjustments}
            onChange={(event) => setAdjustments(event.target.value)}
            placeholder="发型再自然一点，保留脸和身材比例"
            className="mt-2 h-10 w-full rounded-md border border-[var(--border)] bg-[var(--bg-1)] px-3 text-sm outline-none transition-colors focus:border-[var(--border-amber)]"
          />
        </label>
        <label className="block">
          <span className="text-sm text-[var(--fg-1)]">饰品方案</span>
          <div className="mt-2 flex gap-2">
            <button
              type="button"
              onClick={() => setAccessoryEnabled((value) => !value)}
              className={[
                "h-10 rounded-md border px-3 text-sm transition-colors",
                accessoryEnabled
                  ? "border-[var(--border-amber)] bg-[var(--accent-soft)] text-[var(--amber-300)]"
                  : "border-[var(--border)] bg-[var(--bg-1)] text-[var(--fg-1)]",
              ].join(" ")}
            >
              {accessoryEnabled ? "开启" : "关闭"}
            </button>
            <input
              value={accessories}
              onChange={(event) => setAccessories(event.target.value)}
              disabled={!accessoryEnabled}
              className="h-10 min-w-0 flex-1 rounded-md border border-[var(--border)] bg-[var(--bg-1)] px-3 text-sm outline-none transition-colors focus:border-[var(--border-amber)] disabled:opacity-50"
            />
          </div>
        </label>
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-2">
        <Button
          variant="secondary"
          loading={accessoryPreview.isPending}
          disabled={!selectedCandidate?.contact_sheet_image_id}
          onClick={() => {
            if (!selectedCandidate) return;
            accessoryPreview.mutate({
              candidate_id: selectedCandidate.id,
              accessory_plan: {
                enabled: accessoryEnabled,
                items: accessoryEnabled
                  ? accessories
                      .split(/[,，、]/)
                      .map((item) => item.trim())
                      .filter(Boolean)
                  : [],
                strength: "subtle",
              },
              style_prompt: adjustments,
            });
          }}
          leftIcon={<Sparkles className="h-4 w-4" />}
        >
          {selectedCandidate ? "生成饰品预览" : "先确认模特后生成饰品预览"}
        </Button>
      </div>

      {accessoryImages.length > 0 ? (
        <div className="mt-4">
          <p className="mb-2 text-sm text-[var(--fg-1)]">饰品预览</p>
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
        </div>
      ) : null}

      {selectedCandidate ? (
        <div className="mt-4 rounded-md border border-[var(--border)] bg-white/[0.03] p-3">
          <div className="grid gap-3 md:grid-cols-2">
            <label>
              <span className="text-sm text-[var(--fg-1)]">输出模板</span>
              <select
                value={template}
                onChange={(event) => setTemplate(event.target.value as CreateTemplate)}
                disabled={createShowcase.isPending || showcaseStep?.status === "running"}
                className="mt-2 h-10 w-full rounded-md border border-[var(--border)] bg-[var(--bg-1)] px-3 text-sm outline-none disabled:opacity-60"
              >
                {TEMPLATE_LABELS.map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span className="text-sm text-[var(--fg-1)]">质量模式</span>
              <select
                value={quality}
                onChange={(event) => setQuality(event.target.value as "high" | "4k")}
                disabled={createShowcase.isPending || showcaseStep?.status === "running"}
                className="mt-2 h-10 w-full rounded-md border border-[var(--border)] bg-[var(--bg-1)] px-3 text-sm outline-none disabled:opacity-60"
              >
                <option value="high">2K 高质量</option>
                <option value="4k">4K 终稿</option>
              </select>
            </label>
          </div>
          <Button
            className="mt-3"
            variant="primary"
            loading={createShowcase.isPending}
            disabled={showcaseStep?.status === "running"}
            onClick={onClickGenerateShowcase}
            leftIcon={<Shirt className="h-4 w-4" />}
          >
            {showcaseStep?.task_ids?.length ? "按当前方案重新生成展示图" : "开始生成展示图"}
          </Button>
        </div>
      ) : null}

      <ImagePreviewModal
        images={previewList}
        index={previewIndex}
        onIndexChange={setPreviewIndex}
        onClose={() => setPreviewIndex(-1)}
      />

      <ConfirmDialog
        open={confirmRegenerate}
        onOpenChange={setConfirmRegenerate}
        title="重新生成展示图？"
        description="这将丢弃当前 4 张展示图与对应的质检结论，按新模板重新派发任务。"
        confirmText="重新生成"
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
