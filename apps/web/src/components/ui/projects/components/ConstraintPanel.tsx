"use client";

// 右侧约束面板：用现有 InfoPanel 拼装。
// 中等屏幕走 Drawer 模式（受控 open / onOpenChange），桌面级常驻。

import { motion, AnimatePresence } from "framer-motion";
import { X } from "lucide-react";

import type { WorkflowRun } from "@/lib/apiClient";
import { cn } from "@/lib/utils";
import { ImageGrid } from "./ImageGrid";
import { InfoPanel } from "./StageFrame";
import { jsonValue, stepOf } from "../utils";

interface ConstraintPanelProps {
  workflow: WorkflowRun;
  className?: string;
}

function ConstraintBody({ workflow }: { workflow: WorkflowRun }) {
  const product = stepOf(workflow, "product_analysis")?.output_json ?? {};
  const selected = workflow.model_candidates.find(
    (candidate) => candidate.status === "selected",
  );
  const accessory = stepOf(workflow, "model_approval")?.input_json?.accessory_plan;
  const outputSpecs = stepOf(workflow, "showcase_generation")?.input_json ?? {};
  const qualitySummary = stepOf(workflow, "quality_review")?.output_json ?? {};

  return (
    <div className="space-y-4">
      <InfoPanel title="商品原图">
        <ImageGrid images={workflow.product_images} compact />
      </InfoPanel>
      <InfoPanel title="必须保留">
        <p className="whitespace-pre-wrap">{jsonValue(product.must_preserve)}</p>
      </InfoPanel>
      <InfoPanel title="已确认模特">
        <p>{selected ? `方案 ${selected.candidate_index}` : "未确认"}</p>
      </InfoPanel>
      <InfoPanel title="饰品方案">
        <p className="whitespace-pre-wrap">{jsonValue(accessory)}</p>
      </InfoPanel>
      <InfoPanel title="输出规格">
        <p className="whitespace-pre-wrap">{jsonValue(outputSpecs)}</p>
      </InfoPanel>
      <InfoPanel title="质检摘要">
        <p className="whitespace-pre-wrap">{jsonValue(qualitySummary)}</p>
      </InfoPanel>
    </div>
  );
}

export function ConstraintPanel({ workflow, className }: ConstraintPanelProps) {
  return (
    <div className={cn("space-y-4", className)}>
      <ConstraintBody workflow={workflow} />
    </div>
  );
}

interface ConstraintDrawerProps extends ConstraintPanelProps {
  open: boolean;
  onClose: () => void;
}

export function ConstraintDrawer({ workflow, open, onClose }: ConstraintDrawerProps) {
  return (
    <AnimatePresence>
      {open ? (
        <motion.div
          key="constraint-drawer"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.18 }}
          className="fixed inset-0 z-[var(--z-tray)] bg-black/55 backdrop-blur-sm"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) onClose();
          }}
          role="dialog"
          aria-modal="true"
          aria-label="项目约束面板"
        >
          <motion.aside
            initial={{ x: "100%" }}
            animate={{ x: 0 }}
            exit={{ x: "100%" }}
            transition={{ duration: 0.26, ease: [0.22, 1, 0.36, 1] }}
            className="absolute inset-y-0 right-0 flex w-[min(360px,86vw)] flex-col border-l border-[var(--border)] bg-[var(--bg-1)] shadow-[var(--shadow-3)]"
          >
            <header className="flex h-11 items-center justify-between border-b border-[var(--border)] px-3">
              <p className="text-sm font-medium text-[var(--fg-0)]">项目约束</p>
              <button
                type="button"
                onClick={onClose}
                aria-label="关闭"
                className="inline-flex h-9 w-9 items-center justify-center rounded-md text-[var(--fg-1)] transition-colors hover:bg-white/[0.06] hover:text-[var(--fg-0)]"
              >
                <X className="h-4 w-4" />
              </button>
            </header>
            <div className="flex-1 overflow-y-auto p-3">
              <ConstraintBody workflow={workflow} />
            </div>
          </motion.aside>
        </motion.div>
      ) : null}
    </AnimatePresence>
  );
}
