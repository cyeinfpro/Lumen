"use client";

// 海报多尺寸成品阶段：
// - 未触发：参数（aspects chip 多选）+ "生成 N 张成品" 按钮
// - 生成中 / 已就绪：成品网格（PosterRenderCard）
// - 单张返修：背景重生 / 局部 inpaint（Dialog）/ 自定义指令
// - 完成时显示"完成交付并保存资产"按钮（status==="needs_review"）

import { Check, Sparkles, Pencil, Plus } from "lucide-react";
import { useMemo, useState } from "react";

import { Button } from "@/components/ui/primitives/Button";
import { toast } from "@/components/ui/primitives/Toast";
import {
  useCompleteWorkflowDeliveryMutation,
  useCreatePosterRendersMutation,
  useInpaintPosterRenderMutation,
  useRevisePosterRenderMutation,
} from "@/lib/queries";
import type {
  BackendImageMeta,
  PosterAspectRatio,
  PosterRender,
  WorkflowRun,
} from "@/lib/apiClient";
import { cn } from "@/lib/utils";
import { ImagePreviewModal } from "../components/ImagePreviewModal";
import { PosterInpaintDialog } from "../components/PosterInpaintDialog";
import { PosterRenderCard } from "../components/PosterRenderCard";
import { StageFrame } from "../components/StageFrame";
import { POSTER_ASPECT_LABELS, POSTER_DEFAULT_TARGET_ASPECTS } from "../types";
import { stepOf } from "../utils";

function findImageById(
  workflow: WorkflowRun,
  imageId: string | null | undefined,
): BackendImageMeta | undefined {
  if (!imageId) return undefined;
  return [...workflow.product_images, ...workflow.generated_images].find(
    (image) => image.id === imageId,
  );
}

export function PosterMultiSizeStage({ workflow }: { workflow: WorkflowRun }) {
  const step = stepOf(workflow, "multi_size_generation");
  const renders = useMemo(
    () => workflow.poster_renders ?? [],
    [workflow.poster_renders],
  );

  const meta = (workflow.metadata_jsonb || {}) as Record<string, unknown>;
  const targetAspectsFromMeta = Array.isArray(meta.target_aspects)
    ? (meta.target_aspects as string[])
    : [...POSTER_DEFAULT_TARGET_ASPECTS];

  const [aspects, setAspects] = useState<string[]>(targetAspectsFromMeta);
  const [previewList, setPreviewList] = useState<BackendImageMeta[]>([]);
  const [previewIndex, setPreviewIndex] = useState(-1);
  const [inpaintTarget, setInpaintTarget] = useState<PosterRender | null>(null);
  const [reviseTarget, setReviseTarget] = useState<PosterRender | null>(null);
  const [reviseScope, setReviseScope] = useState<"background" | "style">(
    "background",
  );
  const [reviseInstruction, setReviseInstruction] = useState("");

  const create = useCreatePosterRendersMutation(workflow.id, {
    onError: (err) =>
      toast.error("生成多尺寸成品失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      }),
    onSuccess: () => toast.success("多尺寸任务已派发"),
  });

  const revise = useRevisePosterRenderMutation(workflow.id, {
    onError: (err) =>
      toast.error("返修失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      }),
    onSuccess: () => {
      toast.success("返修任务已派发");
      setReviseTarget(null);
      setReviseInstruction("");
    },
  });

  const inpaint = useInpaintPosterRenderMutation(workflow.id, {
    onError: (err) =>
      toast.error("局部修复失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      }),
    onSuccess: () => {
      toast.success("局部修复任务已派发");
      setInpaintTarget(null);
    },
  });

  const hasRenders = renders.length > 0;
  const readyRenderCount = renders.filter((render) => render.image_id && render.status === "ready").length;

  const existingAspectSet = useMemo(
    () => new Set(renders.map((r) => r.aspect_ratio)),
    [renders],
  );

  const complete = useCompleteWorkflowDeliveryMutation(workflow.id, {
    onError: (err) =>
      toast.error("完成交付失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      }),
    onSuccess: () => toast.success("海报已完成交付，成品已加入项目素材"),
  });

  const onTriggerGenerate = () => {
    if (!aspects.length) {
      toast.warning("至少选择一个尺寸");
      return;
    }
    const newAspects = aspects.filter((a) => !existingAspectSet.has(a));
    if (!newAspects.length) {
      toast.warning("所选尺寸都已生成；如需重生请使用返修");
      return;
    }
    create.mutate({
      aspects: newAspects as PosterAspectRatio[],
      use_master_as_reference: true,
    });
  };

  const toggleAspect = (value: string) => {
    setAspects((prev) =>
      prev.includes(value)
        ? prev.filter((item) => item !== value)
        : [...prev, value],
    );
  };

  const renderTarget = inpaintTarget;
  const renderTargetImage = renderTarget
    ? findImageById(workflow, renderTarget.image_id)
    : undefined;

  return (
    <StageFrame
      eyebrow="N°06 — 多尺寸成品"
      title="多尺寸成品"
      subtitle="基于选定母版生成 1:1 / 9:16 / 16:9 / 3:4 等多版本，可逐张返修。"
    >
      <section className="border-t border-[var(--border)] py-4">
        <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
          目标尺寸
        </p>
        <div className="mt-3 flex flex-wrap gap-2">
          {POSTER_ASPECT_LABELS.map(([value, label]) => {
            const active = aspects.includes(value);
            const existed = existingAspectSet.has(value);
            return (
              <button
                key={value}
                type="button"
                onClick={() => toggleAspect(value)}
                className={cn(
                  "inline-flex min-h-9 cursor-pointer items-center gap-1.5 rounded-full border px-3 text-[12px] transition-colors",
                  active
                    ? "border-[var(--border-amber)] bg-[var(--accent-soft)] text-[var(--amber-300)]"
                    : existed
                      ? "border-[var(--border)] bg-[var(--bg-1)] text-[var(--fg-2)]"
                      : "border-[var(--border)] text-[var(--fg-1)] hover:border-[var(--border-strong)] hover:text-[var(--fg-0)]",
                )}
              >
                {label}
                {existed ? (
                  <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-3)]">
                    已生成
                  </span>
                ) : null}
              </button>
            );
          })}
        </div>

        <div className="mt-5 grid grid-cols-1 gap-3 min-[420px]:flex min-[420px]:flex-wrap min-[420px]:items-center">
          <Button
            variant="primary"
            loading={create.isPending}
            onClick={onTriggerGenerate}
            className="w-full min-[420px]:w-auto"
            leftIcon={<Sparkles className="h-4 w-4" />}
          >
            {hasRenders ? "生成所选新尺寸" : "生成多尺寸成品"}
          </Button>
        </div>
      </section>

      {hasRenders ? (
        <ul className="mt-4 grid grid-cols-1 gap-x-4 gap-y-8 sm:grid-cols-2 lg:grid-cols-3">
          {renders.map((render) => (
            <PosterRenderCard
              key={render.id}
              workflow={workflow}
              render={render}
              reviseLoading={revise.isPending || inpaint.isPending}
              onPreview={(image) => {
                setPreviewList([image]);
                setPreviewIndex(0);
              }}
              onReviseBackground={() => {
                setReviseTarget(render);
                setReviseScope("background");
                setReviseInstruction("");
              }}
              onInpaint={() => setInpaintTarget(render)}
              onRegenerate={() => {
                setReviseTarget(render);
                setReviseScope("style");
                setReviseInstruction("");
              }}
            />
          ))}
        </ul>
      ) : null}

      {/* 返修对话框（背景 / 风格） */}
      {reviseTarget ? (
        <ReviseDialog
          open
          render={reviseTarget}
          scope={reviseScope}
          instruction={reviseInstruction}
          onInstructionChange={setReviseInstruction}
          onScopeChange={setReviseScope}
          busy={revise.isPending}
          onClose={() => {
            if (!revise.isPending) {
              setReviseTarget(null);
              setReviseInstruction("");
            }
          }}
          onSubmit={() => {
            const text = reviseInstruction.trim();
            if (!text) {
              toast.error("请输入返修指令");
              return;
            }
            revise.mutate({
              render_id: reviseTarget.id,
              scope: reviseScope,
              instruction: text,
            });
          }}
        />
      ) : null}

      {/* 局部 inpaint 对话框 */}
      {inpaintTarget && renderTargetImage ? (
        <PosterInpaintDialog
          open
          image={renderTargetImage}
          busy={inpaint.isPending}
          onClose={() => {
            if (!inpaint.isPending) setInpaintTarget(null);
          }}
          onSubmit={({ instruction, mask_image_id }) => {
            inpaint.mutate({
              render_id: inpaintTarget.id,
              instruction,
              mask_image_id,
            });
          }}
        />
      ) : null}

      <ImagePreviewModal
        images={previewList}
        index={previewIndex}
        onClose={() => setPreviewIndex(-1)}
      />

      {step?.status === "needs_review" ? (
        <div className="mt-8 border-t border-[var(--border)] pt-5">
          <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
            <div className="min-w-0">
              <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
                Delivery Ready
              </p>
              <p className="mt-1 text-[13px] leading-[1.7] text-[var(--fg-1)]">
                {readyRenderCount} 个尺寸已就绪。完成后会进入交付页，并把海报成品写入项目素材。
              </p>
            </div>
            <Button
              variant="primary"
              loading={complete.isPending}
              onClick={() => complete.mutate()}
              leftIcon={<Check className="h-4 w-4" />}
              className="w-full sm:w-auto"
            >
              完成交付并保存素材
            </Button>
          </div>
        </div>
      ) : null}
    </StageFrame>
  );
}

function ReviseDialog({
  open,
  render,
  scope,
  instruction,
  onInstructionChange,
  onScopeChange,
  busy,
  onClose,
  onSubmit,
}: {
  open: boolean;
  render: PosterRender;
  scope: "background" | "style";
  instruction: string;
  onInstructionChange: (next: string) => void;
  onScopeChange: (next: "background" | "style") => void;
  busy: boolean;
  onClose: () => void;
  onSubmit: () => void;
}) {
  if (!open) return null;
  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="返修"
      className="mobile-dialog-shell fixed inset-0 z-[var(--z-dialog)] flex items-end justify-center bg-black/55 backdrop-blur-sm md:items-center"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !busy) onClose();
      }}
    >
      <div className="mobile-dialog-panel relative flex w-full max-w-md flex-col overflow-hidden bg-[var(--bg-0)] shadow-[var(--shadow-2)] max-md:max-h-[var(--mobile-dialog-max-height)] max-md:rounded-t-[var(--radius-sheet)] md:rounded-[var(--radius-dialog)] md:border md:border-[var(--border)]">
        <header className="border-b border-[var(--border)] px-5 py-4">
          <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
            Revise · {render.aspect_ratio}
          </p>
          <h2 className="type-section-title mt-1">单张返修</h2>
        </header>

        <div className="mobile-dialog-scroll min-h-0 flex-1 overflow-y-auto px-5 py-4">
          <div className="grid grid-cols-2 rounded-full border border-[var(--border)] p-0.5">
            <button
              type="button"
              onClick={() => onScopeChange("background")}
              className={cn(
                "inline-flex h-8 min-w-0 items-center justify-center gap-1.5 rounded-full px-3 font-mono text-[10px] uppercase tracking-[0.18em] transition-colors",
                scope === "background"
                  ? "bg-[var(--amber-400)] text-[var(--accent-on)]"
                  : "text-[var(--fg-1)] hover:text-[var(--fg-0)]",
              )}
            >
              <Plus className="h-3.5 w-3.5" />
              背景重生
            </button>
            <button
              type="button"
              onClick={() => onScopeChange("style")}
              className={cn(
                "inline-flex h-8 min-w-0 items-center justify-center gap-1.5 rounded-full px-3 font-mono text-[10px] uppercase tracking-[0.18em] transition-colors",
                scope === "style"
                  ? "bg-[var(--amber-400)] text-[var(--accent-on)]"
                  : "text-[var(--fg-1)] hover:text-[var(--fg-0)]",
              )}
            >
              <Pencil className="h-3.5 w-3.5" />
              风格调整
            </button>
          </div>

          <label className="mt-4 block">
            <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
              返修指令
            </span>
            <textarea
              value={instruction}
              onChange={(event) => onInstructionChange(event.target.value.slice(0, 400))}
              rows={5}
              maxLength={400}
              placeholder={
                scope === "background"
                  ? "例如：背景改成浅米色棚拍，去掉道具"
                  : "例如：色调更冷一点，留白多一些"
              }
              className="mt-2 w-full resize-y border-b border-[var(--border)] bg-transparent px-1 py-2 text-[14px] leading-6 text-[var(--fg-0)] outline-none transition-colors placeholder:text-[var(--fg-3)] focus:border-[var(--amber-400)]"
            />
          </label>
        </div>

        <footer className="mobile-dialog-footer grid shrink-0 grid-cols-1 gap-2 border-t border-[var(--border)] px-5 py-3 sm:flex sm:items-center sm:justify-end">
          <Button variant="ghost" size="sm" onClick={onClose} disabled={busy} className="w-full sm:w-auto">
            取消
          </Button>
          <Button
            variant="primary"
            size="sm"
            loading={busy}
            onClick={onSubmit}
            disabled={!instruction.trim()}
            className="w-full sm:w-auto"
          >
            派发返修
          </Button>
        </footer>
      </div>
    </div>
  );
}
