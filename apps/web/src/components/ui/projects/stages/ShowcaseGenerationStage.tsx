"use client";

// 商品融合阶段：基于已确认模特 + 商品原图，生成 4 张电商展示图。
// 关键改进：
// 1) reopen / 重新生成 用 ConfirmDialog 兜底
// 2) 展示图运行中显示带骨架的 placeholder 网格
// 3) 模板/质量在运行态禁用

import { Layers, RefreshCw, Shirt } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/primitives/Button";
import { ConfirmDialog } from "@/components/ui/primitives/ConfirmDialog";
import { toast } from "@/components/ui/primitives/Toast";
import {
  useCreateShowcaseImagesMutation,
  useReopenModelSelectionMutation,
} from "@/lib/queries";
import type { BackendImageMeta, WorkflowRun } from "@/lib/apiClient";
import { ImageGrid, ReferenceBlock } from "../components/ImageGrid";
import { ImagePreviewModal } from "../components/ImagePreviewModal";
import { RunningState, StageFrame } from "../components/StageFrame";
import { SHOT_PLAN_DEFAULT, TEMPLATE_LABELS, type CreateTemplate } from "../types";
import { imageById, showcaseImages, stepOf } from "../utils";

export function ShowcaseGenerationStage({ workflow }: { workflow: WorkflowRun }) {
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
  const step = stepOf(workflow, "showcase_generation");
  const [previewList, setPreviewList] = useState<BackendImageMeta[]>([]);
  const [previewIndex, setPreviewIndex] = useState(-1);
  const [template, setTemplate] = useState<CreateTemplate>("premium_studio");
  const [quality, setQuality] = useState<"high" | "4k">("high");
  const [confirmReopen, setConfirmReopen] = useState(false);
  const [confirmRegenerate, setConfirmRegenerate] = useState(false);

  const hasTasks = Boolean(step?.task_ids?.length);
  const isRunning = step?.status === "running";
  const generated = showcaseImages(workflow);
  const productImages = workflow.product_images;
  const modelImages = workflow.model_candidates
    .filter((candidate) => candidate.status === "selected")
    .map((candidate) => imageById(workflow, candidate.contact_sheet_image_id))
    .filter((image): image is BackendImageMeta => Boolean(image));

  const openPreview = (list: BackendImageMeta[], index: number) => {
    setPreviewList(list);
    setPreviewIndex(index);
  };

  const generateShowcase = () => {
    create.mutate({
      template,
      shot_plan: [...SHOT_PLAN_DEFAULT],
      aspect_ratio: "4:5",
      final_quality: quality,
      output_count: 4,
    });
  };

  return (
    <StageFrame
      title="商品融合"
      subtitle="使用已确认模特和商品图，生成 4 张电商展示图。预计 1-3 分钟。"
      badge={
        isRunning ? (
          <span className="inline-flex items-center gap-1.5 rounded-full border border-[var(--border-amber)] bg-[var(--accent-soft)] px-2 py-0.5 text-[10px] text-[var(--amber-300)]">
            <span className="h-1.5 w-1.5 rounded-full bg-[var(--amber-400)] animate-[lumen-pulse-soft_1800ms_ease-in-out_infinite]" />
            正在生成
          </span>
        ) : null
      }
    >
      <div className="grid gap-4 lg:grid-cols-2">
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
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-2">
        <Button
          variant="secondary"
          loading={reopen.isPending}
          onClick={() => setConfirmReopen(true)}
          leftIcon={<RefreshCw className="h-4 w-4" />}
        >
          返回重选模特
        </Button>
      </div>

      <div className="mt-4 grid gap-3 md:grid-cols-2">
        <label>
          <span className="text-sm text-[var(--fg-1)]">输出模板</span>
          <select
            value={template}
            onChange={(event) => setTemplate(event.target.value as CreateTemplate)}
            disabled={isRunning}
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
            disabled={isRunning}
            className="mt-2 h-10 w-full rounded-md border border-[var(--border)] bg-[var(--bg-1)] px-3 text-sm outline-none disabled:opacity-60"
          >
            <option value="high">2K 高质量</option>
            <option value="4k">4K 终稿</option>
          </select>
        </label>
      </div>

      <div className="mt-4 rounded-md border border-[var(--border)] bg-white/[0.03] p-3 text-sm leading-6 text-[var(--fg-1)]">
        <span className="inline-flex items-center gap-1.5 text-[var(--amber-300)]">
          <Layers className="h-3.5 w-3.5" />
          预计生成 4 张
        </span>
        ，使用 {quality === "4k" ? "4K 终稿" : "2K 高质量"} 模式，等待时间取决于队列与上游速度。
      </div>

      <Button
        className="mt-4"
        variant={hasTasks ? "secondary" : "primary"}
        loading={create.isPending}
        disabled={isRunning}
        onClick={() => (hasTasks ? setConfirmRegenerate(true) : generateShowcase())}
        leftIcon={hasTasks ? <RefreshCw className="h-4 w-4" /> : <Shirt className="h-4 w-4" />}
      >
        {hasTasks ? "按当前模板重新生成" : "开始生成展示图"}
      </Button>

      {hasTasks ? (
        generated.length === 0 ? (
          <RunningState className="mt-4" label="展示图正在生成…" />
        ) : (
          <ImageGrid
            className="mt-4"
            images={generated}
            onPreview={(_image, index) => openPreview(generated, index)}
          />
        )
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
        title="重新生成展示图？"
        description="将丢弃当前 4 张展示图与对应质检结论，按新参数派发新一轮任务。"
        confirmText="确认重新生成"
        confirming={create.isPending}
        onConfirm={async () => {
          generateShowcase();
          setConfirmRegenerate(false);
        }}
      />
    </StageFrame>
  );
}
