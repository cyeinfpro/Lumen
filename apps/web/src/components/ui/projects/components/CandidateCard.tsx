"use client";

// Editorial 模特候选卡：portrait 大图 + 上下双联预览 + minimal 信息行 + 双按钮。
// - 选中态：仅角标 + 序号高亮 amber，不再外环 shadow
// - 序号 N°N 顶部 mono
// - 主按钮 minimal，secondary 为 ghost-line

import { motion } from "framer-motion";
import { BookmarkPlus, Check, Sparkles } from "lucide-react";
import Image from "next/image";

import { Button } from "@/components/ui/primitives/Button";
import { Spinner } from "@/components/ui/primitives/Spinner";
import type { BackendImageMeta, ModelCandidate, WorkflowRun } from "@/lib/apiClient";
import { cn } from "@/lib/utils";
import { candidateImages, imageSrc } from "../utils";

interface CandidateCardProps {
  workflow: WorkflowRun;
  candidate: ModelCandidate;
  approving: boolean;
  locallySelected?: boolean;
  onPreview: (image: BackendImageMeta, list: BackendImageMeta[], index: number) => void;
  onChoose?: () => void;
  onApprove: () => void;
  onSaveToLibrary?: () => void;
  savingToLibrary?: boolean;
}

function CandidateGallery({
  images,
  candidate,
  generating,
  onPreview,
  onChoose,
}: {
  images: BackendImageMeta[];
  candidate: ModelCandidate;
  generating: boolean;
  onPreview: CandidateCardProps["onPreview"];
  onChoose?: () => void;
}) {
  if (images.length === 0) {
    return (
      <div className="col-span-full flex h-full flex-col items-center justify-center gap-2 text-[var(--fg-2)]">
        {generating ? (
          <>
            <Spinner size={20} />
            <span className="font-mono text-[10px] uppercase tracking-[0.18em]">
              生成中
            </span>
          </>
        ) : (
          <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-3)]">
            暂无图片
          </span>
        )}
      </div>
    );
  }
  return images.map((candidateImage, index) => (
    <button
      type="button"
      key={candidateImage.id}
      onClick={() =>
        images.length > 1
          ? onPreview(candidateImage, images, index)
          : onChoose?.()
      }
      className="relative h-full min-h-0 w-full overflow-hidden focus-visible:outline-none"
    >
      <Image
        src={imageSrc(candidateImage)}
        alt={`模特候选 ${candidate.candidate_index}-${index + 1}`}
        fill
        sizes="(max-width: 768px) 50vw, 220px"
        unoptimized
        className="h-full w-full object-cover transition-transform duration-[var(--dur-slow)] ease-[var(--ease-develop)] group-hover:scale-[1.02]"
      />
    </button>
  ));
}

function candidateActionLabel(
  selected: boolean,
  generating: boolean,
  locallySelected: boolean,
): string {
  if (selected) return "已确认";
  if (generating) return "生成中";
  return locallySelected ? "已选中" : "设为当前";
}

function CandidateActions({
  hasImage,
  selected,
  generating,
  locallySelected,
  approving,
  onChoose,
  onApprove,
  onSaveToLibrary,
  savingToLibrary,
}: {
  hasImage: boolean;
  selected: boolean;
  generating: boolean;
  locallySelected: boolean;
  approving: boolean;
  onChoose?: () => void;
  onApprove: () => void;
  onSaveToLibrary?: () => void;
  savingToLibrary: boolean;
}) {
  const chosen = selected || locallySelected;

  return (
    <div className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-2">
      <Button
        variant={chosen ? "secondary" : "primary"}
        fullWidth
        disabled={!hasImage || selected || generating}
        loading={approving && !selected}
        onClick={onChoose ?? onApprove}
        leftIcon={
          chosen ? (
            <Check className="h-4 w-4" />
          ) : (
            <Sparkles className="h-4 w-4" />
          )
        }
      >
        {candidateActionLabel(selected, generating, locallySelected)}
      </Button>
      <Button
        variant="outline"
        fullWidth
        disabled={!hasImage || generating}
        loading={savingToLibrary}
        onClick={onSaveToLibrary}
        leftIcon={<BookmarkPlus className="h-4 w-4" />}
      >
        收藏到库
      </Button>
    </div>
  );
}

export function CandidateCard({
  workflow,
  candidate,
  approving,
  locallySelected = false,
  onPreview,
  onChoose,
  onApprove,
  onSaveToLibrary,
  savingToLibrary = false,
}: CandidateCardProps) {
  const images = candidateImages(workflow, candidate);
  const firstImage = images[0];
  const selected = candidate.status === "selected";
  const generating = candidate.status === "generating";

  return (
    <motion.article
      layout
      transition={{ duration: 0.28, ease: [0.22, 1, 0.36, 1] }}
      className="group relative"
    >
      <div className="relative">
        <div
          className={cn(
            "relative grid aspect-[4/5] gap-px overflow-hidden rounded-[var(--radius-card)] bg-[var(--bg-2)] transition-shadow duration-[var(--dur-base)]",
            images.length > 1 ? "grid-cols-2" : "grid-cols-1",
            selected && "ring-1 ring-inset ring-[var(--border-amber)]",
          )}
        >
          <CandidateGallery
            images={images}
            candidate={candidate}
            generating={generating}
            onPreview={onPreview}
            onChoose={onChoose}
          />
        </div>

        <span
          className={cn(
            "absolute left-3 top-3 font-mono text-[10px] uppercase tracking-[0.2em] mix-blend-difference",
            "text-white/90",
          )}
        >
          N°{String(candidate.candidate_index).padStart(2, "0")}
        </span>

        {selected ? (
          <span className="absolute right-3 top-3 inline-flex items-center gap-1.5 rounded-full bg-[var(--amber-400)] px-2.5 py-1 font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--accent-on)] shadow-[var(--shadow-amber)]">
            <Check className="h-3 w-3" />
            已选中
          </span>
        ) : null}
      </div>

      <div className="mt-3 flex items-baseline justify-between gap-3 border-b border-[var(--border)] pb-2">
        <p
          className={cn(
            "text-[15px] font-semibold leading-tight tracking-tight transition-colors",
            selected ? "text-[var(--amber-300)]" : "text-[var(--fg-0)]",
          )}
        >
          方案 {candidate.candidate_index}
        </p>
        <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
          {images.length > 1 ? `${images.length} 张` : "1 张"}
        </span>
      </div>
      <p className="mt-2 text-[12px] leading-5 text-[var(--fg-2)]">
        未试穿商品，仅用于确认模特形象。
      </p>

      <CandidateActions
        hasImage={Boolean(firstImage)}
        selected={selected}
        generating={generating}
        locallySelected={locallySelected}
        approving={approving}
        onChoose={onChoose}
        onApprove={onApprove}
        onSaveToLibrary={onSaveToLibrary}
        savingToLibrary={savingToLibrary}
      />
    </motion.article>
  );
}
