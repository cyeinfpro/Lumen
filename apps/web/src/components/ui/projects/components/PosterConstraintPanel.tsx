"use client";

// 海报工作流右侧约束面板：风格摘要 + 文案切分 + 品牌资产。
// 桌面常驻（≥xl），中屏 / 移动走抽屉。

import { motion, AnimatePresence } from "framer-motion";
import { X } from "lucide-react";
import { useId, useRef } from "react";

import { BottomSheet } from "@/components/ui/primitives/mobile/BottomSheet";
import { useModalLayer } from "@/components/ui/primitives/mobile/useModalLayer";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import { useIsMobile } from "@/hooks/useMediaQuery";
import type { WorkflowRun } from "@/lib/apiClient";
import { cn } from "@/lib/utils";
import { InfoPanel } from "./StageFrame";
import { jsonValue, stepOf } from "../utils";

interface PosterConstraintPanelProps {
  workflow: WorkflowRun;
  className?: string;
}

function PosterConstraintBody({ workflow }: { workflow: WorkflowRun }) {
  const meta = (workflow.metadata_jsonb || {}) as Record<string, unknown>;
  const styleSummary = (meta.style_summary || {}) as Record<string, unknown>;
  const brandAssets = (meta.brand_assets || {}) as Record<string, unknown>;
  const targetAspects = Array.isArray(meta.target_aspects)
    ? (meta.target_aspects as string[])
    : [];
  const copyAnalysis = stepOf(workflow, "copy_analysis")?.output_json ?? {};
  const selectedMaster = (workflow.poster_masters ?? []).find(
    (master) => master.status === "selected",
  );

  return (
    <div className="min-w-0">
      <InfoPanel title="原始文案">
        <p className="whitespace-pre-wrap break-words text-[13px] leading-[1.7] text-[var(--fg-1)]">
          {workflow.user_prompt || "未录入"}
        </p>
      </InfoPanel>
      <InfoPanel title="选定风格">
        <p className="whitespace-pre-wrap break-words">
          {typeof styleSummary.title === "string" && styleSummary.title
            ? String(styleSummary.title)
            : "未指定"}
        </p>
        {typeof styleSummary.mood === "string" && styleSummary.mood ? (
          <p className="mt-1 text-[12px] text-[var(--fg-2)]">
            {String(styleSummary.mood)}
          </p>
        ) : null}
      </InfoPanel>
      <InfoPanel title="目标尺寸">
        <p className="whitespace-pre-wrap break-words">
          {targetAspects.length ? targetAspects.join("、") : "未指定"}
        </p>
      </InfoPanel>
      <InfoPanel title="文案切分">
        <p className="whitespace-pre-wrap break-words">{jsonValue(copyAnalysis)}</p>
      </InfoPanel>
      <InfoPanel title="品牌素材">
        <p className="whitespace-pre-wrap break-words">{jsonValue(brandAssets)}</p>
      </InfoPanel>
      <InfoPanel title="选定母版">
        <p className="break-words">
          {selectedMaster
            ? `候选 ${selectedMaster.candidate_index}（${selectedMaster.id.slice(0, 8)}）`
            : "未选定"}
        </p>
      </InfoPanel>
    </div>
  );
}

export function PosterConstraintPanel({
  workflow,
  className,
}: PosterConstraintPanelProps) {
  return (
    <div className={cn("relative", className)}>
      <header className="border-b border-[var(--border)] pb-4">
        <p className="type-page-kicker">Constraints</p>
        <h3 className="type-section-title mt-1.5">项目约束</h3>
      </header>
      <PosterConstraintBody workflow={workflow} />
    </div>
  );
}

interface PosterConstraintDrawerProps extends PosterConstraintPanelProps {
  open: boolean;
  onClose: () => void;
}

function DrawerHeader({
  onClose,
  titleId,
}: {
  onClose: () => void;
  titleId: string;
}) {
  return (
    <header className="flex shrink-0 items-start justify-between gap-3 border-b border-[var(--border)] px-5 py-4">
      <div className="min-w-0">
        <p className="type-page-kicker">Constraints</p>
        <h2 id={titleId} className="type-section-title mt-1.5">
          项目约束
        </h2>
      </div>
      <button
        type="button"
        onClick={onClose}
        aria-label="关闭"
        className="-mr-2 inline-flex h-11 w-11 shrink-0 cursor-pointer items-center justify-center rounded-full text-[var(--fg-1)] transition-colors hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)] sm:h-10 sm:w-10"
      >
        <X className="h-4 w-4" />
      </button>
    </header>
  );
}

export function PosterConstraintDrawer({
  workflow,
  open,
  onClose,
}: PosterConstraintDrawerProps) {
  const isMobile = useIsMobile();
  const isDesktop = isMobile !== true;
  const drawerRef = useRef<HTMLElement | null>(null);
  const titleId = useId();
  const onDrawerKeyDown = useModalLayer({
    open: isDesktop && open,
    rootRef: drawerRef,
    onClose,
  });
  useBodyScrollLock(isDesktop && open);

  if (!isDesktop) {
    return (
      <BottomSheet
        open={open}
        onClose={onClose}
        ariaLabel="项目约束面板"
        snapPoints={["80%", "60%"]}
      >
        <DrawerHeader onClose={onClose} titleId={titleId} />
        <div className="mobile-dialog-scroll min-h-0 min-w-0 flex-1 overflow-y-auto px-5 pb-[var(--mobile-dialog-footer-pad-bottom)]">
          <PosterConstraintBody workflow={workflow} />
        </div>
      </BottomSheet>
    );
  }

  return (
    <AnimatePresence>
      {open ? (
        <motion.div
          key="poster-constraint-drawer"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.18 }}
          className="fixed inset-0 z-[var(--z-tray)] bg-black/55 backdrop-blur-sm"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) onClose();
          }}
        >
          <motion.aside
            ref={drawerRef}
            role="dialog"
            aria-modal="true"
            aria-labelledby={titleId}
            tabIndex={-1}
            onKeyDown={onDrawerKeyDown}
            initial={{ x: "100%" }}
            animate={{ x: 0 }}
            exit={{ x: "100%" }}
            transition={{ duration: 0.26, ease: [0.22, 1, 0.36, 1] }}
            className={cn(
              "absolute inset-y-0 right-0 flex w-[min(380px,86vw)] flex-col",
              "max-h-[100dvh] border-l border-[var(--border)] bg-[var(--bg-0)] shadow-[var(--shadow-2)]",
            )}
          >
            <DrawerHeader onClose={onClose} titleId={titleId} />
            <div className="mobile-dialog-scroll min-h-0 flex-1 overflow-y-auto px-5 pb-6">
              <PosterConstraintBody workflow={workflow} />
            </div>
          </motion.aside>
        </motion.div>
      ) : null}
    </AnimatePresence>
  );
}
