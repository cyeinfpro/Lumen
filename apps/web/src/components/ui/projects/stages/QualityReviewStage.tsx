"use client";

// 质检返修阶段（editorial 重构）：
// 1) render-phase reset 同步 selectedImageId 至 images（解决新图返回后选中错位）
// 2) 返修 / 交付 走 toast；交付确认走 ConfirmDialog
// 3) 重选模特再次走 ConfirmDialog（破坏性）
// 4) 视觉：hairline 段落 + mono eyebrow + underline 输入；按钮 outline / primary 收敛。

import { Check, RefreshCw } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/primitives/Button";
import { ConfirmDialog } from "@/components/ui/primitives/ConfirmDialog";
import { toast } from "@/components/ui/primitives/Toast";
import {
  useCompleteWorkflowDeliveryMutation,
  useReopenModelSelectionMutation,
  useReviseWorkflowImageMutation,
} from "@/lib/queries";
import type { BackendImageMeta, WorkflowRun } from "@/lib/apiClient";
import { ImagePreviewModal } from "../components/ImagePreviewModal";
import { ResultImageCard } from "../components/ResultImageCard";
import { RunningState, StageFrame } from "../components/StageFrame";
import { showcaseImages } from "../utils";

export function QualityReviewStage({ workflow }: { workflow: WorkflowRun }) {
  const images = showcaseImages(workflow);
  const reportsByImage = new Map(
    workflow.quality_reports.map((report) => [report.image_id, report]),
  );

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
  const [previewIndex, setPreviewIndex] = useState(-1);
  const [confirmReopen, setConfirmReopen] = useState(false);
  const [confirmDeliver, setConfirmDeliver] = useState(false);

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

  const reviseCount = workflow.quality_reports.filter(
    (report) => report.recommendation === "revise",
  ).length;
  const approveCount = workflow.quality_reports.filter(
    (report) => report.recommendation === "approve",
  ).length;

  return (
    <StageFrame
      eyebrow="N°07 — Quality Review"
      title="质检返修"
      subtitle="每张展示图都有质检结论。可发起一次文字返修，或全部通过后进入交付。"
      actions={
        <Button
          variant="outline"
          size="sm"
          loading={reopen.isPending}
          onClick={() => setConfirmReopen(true)}
          leftIcon={<RefreshCw className="h-3.5 w-3.5" />}
        >
          重选模特
        </Button>
      }
    >
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
          Revise Instruction
        </p>
        <input
          value={instruction}
          onChange={(event) => setInstruction(event.target.value)}
          className="mt-3 h-10 w-full border-b border-[var(--border)] bg-transparent px-1 text-[14px] text-[var(--fg-0)] outline-none transition-colors placeholder:text-[var(--fg-3)] focus:border-[var(--amber-400)]"
        />
        <div className="mt-5 flex flex-wrap items-center gap-3">
          <Button
            variant="outline"
            loading={revise.isPending}
            disabled={!selectedImageId || images.length === 0}
            onClick={() =>
              revise.mutate({
                image_id: selectedImageId,
                instruction,
                scope: "full_image",
              })
            }
            leftIcon={<RefreshCw className="h-4 w-4" />}
          >
            文字返修
          </Button>
          <Button
            variant="primary"
            loading={complete.isPending}
            disabled={images.length === 0}
            onClick={() => setConfirmDeliver(true)}
            leftIcon={<Check className="h-4 w-4" />}
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
