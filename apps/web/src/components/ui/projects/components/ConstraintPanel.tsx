"use client";

// 右侧约束面板（editorial）：
// - 桌面常驻（≥ xl）：直接铺 InfoPanel，section 之间走 hairline
// - 中屏 / 移动：抽屉/BottomSheet，header 用 mono eyebrow + serif italic title
// - 不再叠加 bg-white/[0.035] + border + shadow 的旧卡片包裹
//
// SSR safe：useState false → effect 里读 matchMedia 切换；与 useMediaQuery 一致策略。

import { motion, AnimatePresence } from "framer-motion";
import { X } from "lucide-react";

import { useIsMobile } from "@/hooks/useMediaQuery";
import type { WorkflowRun } from "@/lib/apiClient";
import { cn } from "@/lib/utils";
import { BottomSheet } from "@/components/ui/primitives/mobile/BottomSheet";
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
    <div>
      <InfoPanel title="商品原图">
        <ImageGrid images={workflow.product_images} compact />
      </InfoPanel>
      <InfoPanel title="商品还原点">
        <p className="whitespace-pre-wrap">{jsonValue(product.must_preserve)}</p>
      </InfoPanel>
      <InfoPanel title="推荐背景">
        <p className="whitespace-pre-wrap">{jsonValue(product.background_recommendation)}</p>
      </InfoPanel>
      <InfoPanel title="已确认模特">
        <p>{selected ? `方案 ${selected.candidate_index}` : "未确认"}</p>
      </InfoPanel>
      <InfoPanel title="配饰四宫格">
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
    <div className={cn("relative", className)}>
      <header className="pb-3">
        <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
          Constraints
        </p>
        <h3 className="mt-1.5 font-display text-[20px] italic leading-[1.1] text-[var(--fg-0)]">
          项目约束
        </h3>
      </header>
      <ConstraintBody workflow={workflow} />
    </div>
  );
}

interface ConstraintDrawerProps extends ConstraintPanelProps {
  open: boolean;
  onClose: () => void;
}

function DrawerHeader({ onClose }: { onClose: () => void }) {
  return (
    <header className="flex shrink-0 items-start justify-between gap-3 border-b border-[var(--border)] px-5 py-4">
      <div className="min-w-0">
        <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
          Constraints
        </p>
        <h2 className="mt-1.5 font-display text-[22px] italic leading-[1.1] text-[var(--fg-0)]">
          项目约束
        </h2>
      </div>
      <button
        type="button"
        onClick={onClose}
        aria-label="关闭"
        className="-mr-2 inline-flex h-10 w-10 shrink-0 cursor-pointer items-center justify-center rounded-full text-[var(--fg-1)] transition-colors hover:bg-white/[0.06] hover:text-[var(--fg-0)]"
      >
        <X className="h-4 w-4" />
      </button>
    </header>
  );
}

export function ConstraintDrawer({ workflow, open, onClose }: ConstraintDrawerProps) {
  // SSR safe 桌面/移动判定：useIsMobile 首屏返回 null（视为桌面，隐藏 BottomSheet 体感），
  // 客户端挂载后由 matchMedia 修正；避免 hydration 抖动也避免移动端首屏就跑全屏 motion。
  const isMobile = useIsMobile();
  const isDesktop = isMobile !== true;

  if (!isDesktop) {
    return (
      <BottomSheet
        open={open}
        onClose={onClose}
        ariaLabel="项目约束面板"
        snapPoints={["80%", "60%"]}
      >
        <DrawerHeader onClose={onClose} />
        <div className="px-5">
          <ConstraintBody workflow={workflow} />
        </div>
      </BottomSheet>
    );
  }

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
            className={cn(
              "absolute inset-y-0 right-0 flex w-[min(380px,86vw)] flex-col",
              "max-h-[100dvh] border-l border-[var(--border)] bg-[var(--bg-0)] shadow-[var(--shadow-3)]",
            )}
          >
            <DrawerHeader onClose={onClose} />
            <div className="flex-1 min-h-0 overflow-y-auto px-5 pb-6">
              <ConstraintBody workflow={workflow} />
            </div>
          </motion.aside>
        </motion.div>
      ) : null}
    </AnimatePresence>
  );
}
