"use client";

// Editorial 质检结果卡：portrait 大图 + ScoreRing + recommendation chip + 选择按钮。
// 选中态：amber ring + 按钮高亮，去除整卡 shadow。

import { motion } from "framer-motion";
import { CheckCheck } from "lucide-react";
import Image from "next/image";

import { Button } from "@/components/ui/primitives/Button";
import type { BackendImageMeta, QualityReport } from "@/lib/apiClient";
import { cn } from "@/lib/utils";
import { ScoreRing } from "./ScoreRing";
import { RECOMMENDATION_LABEL } from "../types";
import { imageSrc } from "../utils";

const REC_TONE: Record<string, string> = {
  approve: "text-[var(--success)]",
  revise: "text-[var(--danger)]",
  pending: "text-[var(--fg-2)]",
};

const REC_DOT: Record<string, string> = {
  approve: "bg-[var(--success)]",
  revise: "bg-[var(--danger)]",
  pending: "bg-[var(--fg-3)]",
};

interface ResultImageCardProps {
  image: BackendImageMeta;
  report?: QualityReport;
  selected: boolean;
  onSelect: () => void;
  onPreview: () => void;
}

export function ResultImageCard({
  image,
  report,
  selected,
  onSelect,
  onPreview,
}: ResultImageCardProps) {
  const recommendation = report?.recommendation ?? "pending";
  const score = report?.overall_score;

  return (
    <motion.article
      layout
      transition={{ duration: 0.24, ease: [0.22, 1, 0.36, 1] }}
      className="group relative"
    >
      <button
        type="button"
        onClick={onPreview}
        className={cn(
          "relative block aspect-[4/5] w-full overflow-hidden rounded-[var(--radius-card)] bg-[var(--bg-2)] focus-visible:outline-none",
          selected && "ring-1 ring-inset ring-[var(--border-amber)]",
        )}
      >
        <Image
          src={imageSrc(image)}
          alt="展示图"
          fill
          sizes="(max-width: 768px) 50vw, 360px"
          unoptimized
          className="h-full w-full object-cover transition-transform duration-[var(--dur-slow)] ease-[var(--ease-develop)] group-hover:scale-[1.02]"
        />
        {typeof score === "number" ? (
          <div className="absolute right-3 top-3">
            <ScoreRing score={score} size={36} stroke={1.5} />
          </div>
        ) : null}
      </button>

      <div className="mt-2 flex items-center justify-between gap-2 border-b border-[var(--border)] pb-2 font-mono text-[10px] uppercase tracking-[0.18em]">
        <span className={cn("inline-flex items-center gap-1.5", REC_TONE[recommendation] ?? REC_TONE.pending)}>
          <span aria-hidden className={cn("inline-block h-1.5 w-1.5 rounded-full", REC_DOT[recommendation] ?? REC_DOT.pending)} />
          {RECOMMENDATION_LABEL[recommendation] ?? recommendation}
        </span>
        <span className="text-[var(--fg-2)] tabular-nums">
          {typeof score === "number" ? `${Math.round(score)} pts` : "—"}
        </span>
      </div>
      <Button
        className="mt-2"
        variant={selected ? "secondary" : "ghost"}
        fullWidth
        onClick={onSelect}
        leftIcon={selected ? <CheckCheck className="h-3.5 w-3.5" /> : null}
      >
        {selected ? "已选中" : "选择返修"}
      </Button>
    </motion.article>
  );
}
