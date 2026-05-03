"use client";

// 质检结果卡：缩略图 + ScoreRing + 推荐结论 + 选择"用于返修"的复选按钮。
// 选中态：琥珀外环 + 选择按钮高亮。

import { motion } from "framer-motion";
import { CheckCheck } from "lucide-react";

import { Button } from "@/components/ui/primitives/Button";
import type { BackendImageMeta, QualityReport } from "@/lib/apiClient";
import { cn } from "@/lib/utils";
import { ScoreRing } from "./ScoreRing";
import { RECOMMENDATION_LABEL } from "../types";
import { imageSrc } from "../utils";

const REC_TONE: Record<string, string> = {
  approve: "border-[var(--success)]/30 bg-[var(--success-soft)] text-[var(--success)]",
  revise: "border-[var(--danger)]/30 bg-[var(--danger-soft)] text-[var(--danger)]",
  pending: "border-[var(--border)] bg-white/[0.04] text-[var(--fg-1)]",
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
      transition={{ duration: 0.22, ease: [0.22, 1, 0.36, 1] }}
      className={cn(
        "rounded-md border bg-white/[0.035] p-2 text-left transition-shadow",
        selected
          ? "border-[var(--border-amber)] shadow-[var(--shadow-amber)]"
          : "border-[var(--border)] hover:border-[var(--border-strong)]",
      )}
    >
      <button
        type="button"
        onClick={onPreview}
        className="block w-full overflow-hidden rounded-md focus-visible:outline-none"
      >
        <img
          src={imageSrc(image)}
          alt="展示图"
          loading="lazy"
          className="aspect-[4/5] w-full object-cover transition-transform duration-[var(--dur-slow)] hover:scale-[1.02]"
        />
      </button>
      <div className="mt-2 flex items-center justify-between gap-2">
        <span
          className={cn(
            "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px]",
            REC_TONE[recommendation] ?? REC_TONE.pending,
          )}
        >
          {RECOMMENDATION_LABEL[recommendation] ?? recommendation}
        </span>
        {typeof score === "number" ? (
          <ScoreRing score={score} size={32} />
        ) : (
          <span className="text-xs tabular-nums text-[var(--fg-2)]">--</span>
        )}
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
