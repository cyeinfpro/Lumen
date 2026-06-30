"use client";

// 海报母版生成阶段：
// 1) 还未触发：显示参数面板 + "生成 4 张候选" 按钮
// 2) 生成中：4 个 portrait 骨架占位
// 3) 已就绪：4 张候选网格 + "生成更多" + "选定此版本"
//
// 业务逻辑：
//   - workflow.poster_masters 数组（候选 master）
//   - master.status: generating / ready / selected / failed
//   - approvePosterMaster mutation 用 master_id

import { Loader2, RefreshCw, Sparkles, Check } from "lucide-react";
import Image from "next/image";
import { useState } from "react";

import { Button } from "@/components/ui/primitives/Button";
import { toast } from "@/components/ui/primitives/Toast";
import {
  useApprovePosterMasterMutation,
  useCreatePosterMastersMutation,
} from "@/lib/queries";
import type { BackendImageMeta, PosterMaster, WorkflowRun } from "@/lib/apiClient";
import { cn } from "@/lib/utils";
import { ImagePreviewModal } from "../components/ImagePreviewModal";
import { RunningState, StageFrame } from "../components/StageFrame";
import { imageSrc, stepOf } from "../utils";

function findImageById(
  workflow: WorkflowRun,
  imageId: string | null | undefined,
): BackendImageMeta | undefined {
  if (!imageId) return undefined;
  return [...workflow.product_images, ...workflow.generated_images].find(
    (image) => image.id === imageId,
  );
}

export function PosterMasterGenerationStage({ workflow }: { workflow: WorkflowRun }) {
  const step = stepOf(workflow, "master_generation");
  const masters = workflow.poster_masters ?? [];
  const isRunning = step?.status === "running";
  const hasMasters = masters.length > 0;
  const [previewList, setPreviewList] = useState<BackendImageMeta[]>([]);
  const [previewIndex, setPreviewIndex] = useState(-1);

  const create = useCreatePosterMastersMutation(workflow.id, {
    onError: (err) =>
      toast.error("生成母版失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      }),
    onSuccess: () => toast.success("母版任务已派发"),
  });

  const approve = useApprovePosterMasterMutation(workflow.id, {
    onError: (err) =>
      toast.error("选定母版失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      }),
    onSuccess: () => toast.success("母版已选定"),
  });

  const trigger = () => {
    create.mutate({ candidate_count: 4 });
  };

  const more = () => {
    create.mutate({ candidate_count: 4 });
  };

  // 阶段一：未触发任何生成
  if (!hasMasters && !isRunning) {
    return (
      <StageFrame
        eyebrow="N°04 — 母版生成"
        title="母版生成"
        subtitle="基于文案切分和选定风格，一次生成 4 张 1:1 候选母版。"
      >
        <section className="border-y border-[var(--border)] py-6">
          <p className="text-[13px] leading-[1.7] text-[var(--fg-1)]">
            母版作为主版本，后续多尺寸成品都会参考它。建议从 4 张候选中选一张最契合的。
          </p>
        </section>
        <div className="mt-6 flex flex-wrap items-center gap-3 border-t border-[var(--border)] pt-5">
          <Button
            variant="primary"
            loading={create.isPending}
            onClick={trigger}
            leftIcon={<Sparkles className="h-4 w-4" />}
          >
            生成 4 张母版候选
          </Button>
        </div>
      </StageFrame>
    );
  }

  // 阶段二：生成中且无任何 master 行
  if (isRunning && !hasMasters) {
    return (
      <StageFrame
        eyebrow="N°04 — 母版生成"
        title="母版生成"
        subtitle="正在生成 4 张母版候选，预计 30-60 秒。"
      >
        <RunningState label="正在派发母版任务…" />
      </StageFrame>
    );
  }

  // 阶段三：已有候选（generating / ready 混合）
  const isApprovalStep = workflow.current_step === "master_approval";

  return (
    <StageFrame
      eyebrow={isApprovalStep ? "N°05 — 母版选定" : "N°04 — 母版生成"}
      title={isApprovalStep ? "母版选定" : "母版生成"}
      subtitle="预览候选；选中后会推进到多尺寸成品阶段。"
      actions={
        <Button
          variant="ghost"
          size="sm"
          loading={create.isPending}
          onClick={more}
          leftIcon={<RefreshCw className="h-3.5 w-3.5" />}
          className="w-full sm:w-auto"
        >
          再生成 4 张
        </Button>
      }
    >
      <ul className="mt-2 grid grid-cols-2 gap-x-4 gap-y-8 md:grid-cols-3 xl:grid-cols-4">
        {masters.map((master) => (
          <MasterCard
            key={master.id}
            workflow={workflow}
            master={master}
            approving={approve.isPending}
            onPreview={(image, list, index) => {
              setPreviewList(list);
              setPreviewIndex(index);
              void image;
            }}
            onApprove={() => approve.mutate({ master_id: master.id })}
          />
        ))}
      </ul>
      <ImagePreviewModal
        images={previewList}
        index={previewIndex}
        onClose={() => setPreviewIndex(-1)}
      />
    </StageFrame>
  );
}

function MasterCard({
  workflow,
  master,
  approving,
  onPreview,
  onApprove,
}: {
  workflow: WorkflowRun;
  master: PosterMaster;
  approving: boolean;
  onPreview: (image: BackendImageMeta, list: BackendImageMeta[], index: number) => void;
  onApprove: () => void;
}) {
  const image = findImageById(workflow, master.image_id);
  const isGenerating = master.status === "generating";
  const isFailed = master.status === "failed";
  const isSelected = master.status === "selected";
  const isReady = master.status === "ready";

  return (
    <li className="group relative">
      <button
        type="button"
        onClick={() => {
          if (image) onPreview(image, [image], 0);
        }}
        disabled={!image}
        className={cn(
          "relative block aspect-square w-full overflow-hidden rounded-[var(--radius-card)] bg-[var(--bg-2)] transition-shadow duration-[var(--dur-base)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
          isSelected && "ring-1 ring-inset ring-[var(--border-amber)]",
        )}
      >
        {image ? (
          <Image
            src={imageSrc(image)}
            alt={`母版候选 ${master.candidate_index}`}
            fill
            sizes="(max-width: 768px) 50vw, 260px"
            unoptimized
            className="h-full w-full object-cover transition-transform duration-[var(--dur-slow)] ease-[var(--ease-develop)] group-hover:scale-[1.02]"
          />
        ) : (
          <div className="flex h-full flex-col items-center justify-center gap-2 text-[var(--fg-2)]">
            {isGenerating ? (
              <>
                <Loader2 className="h-5 w-5 animate-spin" />
                <span className="font-mono text-[10px] uppercase tracking-[0.18em]">
                  生成中
                </span>
              </>
            ) : isFailed ? (
              <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--danger)]">
                生成失败
              </span>
            ) : (
              <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-3)]">
                等待中
              </span>
            )}
          </div>
        )}

        <span className="absolute left-3 top-3 font-mono text-[10px] uppercase tracking-[0.22em] text-white/90 mix-blend-difference">
          N°{String(master.candidate_index).padStart(2, "0")}
        </span>

        {isSelected ? (
          <span className="absolute right-3 top-3 inline-flex items-center gap-1.5 rounded-full bg-[var(--amber-400)] px-2.5 py-1 font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--accent-on)] shadow-[var(--shadow-amber)]">
            <Check className="h-3 w-3" />
            已选定
          </span>
        ) : null}
      </button>

      <div className="mt-3 flex items-baseline justify-between gap-3 border-b border-[var(--border)] pb-2">
        <p
          className={cn(
            "text-[15px] font-semibold leading-tight tracking-tight transition-colors",
            isSelected ? "text-[var(--amber-300)]" : "text-[var(--fg-0)]",
          )}
        >
          候选 {master.candidate_index}
        </p>
        <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
          {isSelected ? "已选" : isReady ? "可选" : isFailed ? "失败" : "生成中"}
        </span>
      </div>

      <div className="mt-3">
        <Button
          variant={isSelected ? "secondary" : "primary"}
          fullWidth
          disabled={!image || isGenerating || isFailed}
          loading={approving && !isSelected && isReady}
          onClick={onApprove}
          leftIcon={
            isSelected ? <Check className="h-4 w-4" /> : <Sparkles className="h-4 w-4" />
          }
        >
          {isSelected ? "已选定" : isGenerating ? "生成中" : "选定此版本"}
        </Button>
      </div>
    </li>
  );
}
