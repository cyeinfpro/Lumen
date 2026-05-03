"use client";

// 模特候选卡片：四宫格联排预览 + 选中态琥珀外环 + 序号 + 状态徽章。
// 入选时 layout 会有微动画（Framer Motion 的 layout）。

import { motion } from "framer-motion";
import { Check, Sparkles } from "lucide-react";

import { Button } from "@/components/ui/primitives/Button";
import { Spinner } from "@/components/ui/primitives/Spinner";
import type { BackendImageMeta, ModelCandidate, WorkflowRun } from "@/lib/apiClient";
import { cn } from "@/lib/utils";
import { imageById, imageSrc, stringArray } from "../utils";
import { STATUS_LABEL } from "../types";

interface CandidateCardProps {
  workflow: WorkflowRun;
  candidate: ModelCandidate;
  approving: boolean;
  locallySelected?: boolean;
  onPreview: (image: BackendImageMeta, list: BackendImageMeta[], index: number) => void;
  onChoose?: () => void;
  onApprove: () => void;
}

export function CandidateCard({
  workflow,
  candidate,
  approving,
  locallySelected = false,
  onPreview,
  onChoose,
  onApprove,
}: CandidateCardProps) {
  const candidateImageIds = stringArray(candidate.model_brief_json.candidate_image_ids);
  const imageIds = candidateImageIds.length
    ? candidateImageIds
    : candidate.contact_sheet_image_id
      ? [candidate.contact_sheet_image_id]
      : [];
  const images = imageIds
    .map((id) => imageById(workflow, id))
    .filter((image): image is BackendImageMeta => Boolean(image));
  const firstImage = images[0];
  const selected = candidate.status === "selected";
  const generating = candidate.status === "generating";

  return (
    <motion.article
      layout
      transition={{ duration: 0.24, ease: [0.22, 1, 0.36, 1] }}
      className={cn(
        "rounded-md border bg-white/[0.035] p-3 transition-shadow",
        selected
          ? "border-[var(--border-amber)] shadow-[var(--shadow-amber)]"
          : locallySelected
            ? "border-[var(--border-amber)]"
            : "border-[var(--border)] hover:border-[var(--border-strong)]",
      )}
    >
      <div className="relative">
        <div
          className={cn(
            "grid aspect-[4/5] gap-1 overflow-hidden rounded-md bg-[var(--bg-2)]",
            images.length > 1 ? "grid-cols-2" : "grid-cols-1",
          )}
        >
          {images.length > 0 ? (
            images.map((candidateImage, index) => (
              <button
                type="button"
                key={candidateImage.id}
                onClick={() =>
                  images.length > 1
                    ? onPreview(candidateImage, images, index)
                    : onChoose?.()
                }
                className="group h-full min-h-0 w-full overflow-hidden focus-visible:outline-none"
              >
                <img
                  src={imageSrc(candidateImage)}
                  alt={`模特候选 ${candidate.candidate_index}-${index + 1}`}
                  loading="lazy"
                  className="h-full w-full object-cover transition-transform duration-[var(--dur-slow)] group-hover:scale-[1.02]"
                />
              </button>
            ))
          ) : (
            <div className="col-span-full flex h-full items-center justify-center text-sm text-[var(--fg-2)]">
              {generating ? <Spinner size={20} /> : "暂无图像"}
            </div>
          )}
        </div>
        {selected ? (
          <span className="pointer-events-none absolute right-2 top-2 inline-flex h-6 items-center gap-1 rounded-full border border-[var(--border-amber)] bg-[var(--accent)] px-2 text-[10px] font-medium text-black">
            <Check className="h-3 w-3" />
            已确认
          </span>
        ) : null}
      </div>

      <div className="mt-3 flex items-center justify-between gap-2">
        <p className="flex items-center gap-1.5 text-sm font-medium text-[var(--fg-0)]">
          <span className="inline-flex h-5 w-5 items-center justify-center rounded-full bg-[var(--bg-2)] text-[10px] tabular-nums text-[var(--fg-1)]">
            {candidate.candidate_index}
          </span>
          方案 {candidate.candidate_index}
        </p>
        <div className="flex items-center gap-1.5">
          {images.length > 1 ? (
            <span className="rounded-full border border-[var(--border)] px-2 py-0.5 text-[10px] text-[var(--fg-2)]">
              {images.length} 张
            </span>
          ) : null}
          <span
            className={cn(
              "rounded-full border px-2 py-0.5 text-[10px]",
              selected
                ? "border-[var(--border-amber)] bg-[var(--accent-soft)] text-[var(--amber-300)]"
                : generating
                  ? "border-[var(--amber-400)]/40 text-[var(--amber-300)]"
                  : "border-[var(--border)] text-[var(--fg-2)]",
            )}
          >
            {STATUS_LABEL[candidate.status] ?? candidate.status}
          </span>
        </div>
      </div>
      <p className="mt-2 text-xs leading-5 text-[var(--fg-2)]">
        未试穿商品，仅用于确认模特形象。
      </p>
      <Button
        className="mt-3"
        variant={selected || locallySelected ? "secondary" : "primary"}
        fullWidth
        disabled={!firstImage || selected || generating}
        loading={approving && !selected}
        onClick={onChoose ?? onApprove}
        leftIcon={
          selected || locallySelected ? (
            <Check className="h-4 w-4" />
          ) : (
            <Sparkles className="h-4 w-4" />
          )
        }
      >
        {selected ? "已确认" : generating ? "生成中…" : locallySelected ? "已选中" : "选择此模特"}
      </Button>
    </motion.article>
  );
}
