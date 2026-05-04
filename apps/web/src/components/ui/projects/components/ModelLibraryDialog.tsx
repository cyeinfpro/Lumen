"use client";

// 项目流程内的"选模特"弹窗：
//  - 浏览部分（年龄 tab / 来源 filter / grid / 上传）抽到 ModelLibraryBrowser
//  - 这里只保留 dialog 外壳、focus trap、footer（"设为当前模特"+"生成模特候选"）
//  - 顶部加"打开完整模特库 →"链接：onClose() 后 router.push("/projects/library")
//
// 项目候选生成（onGenerateCandidates）继续走旧路径——上层用
// useCreateModelCandidatesMutation(workflow.id) 派发，不动它。

import { AnimatePresence, motion } from "framer-motion";
import { ArrowUpRight, Check, Library, WandSparkles, X } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/primitives/Button";
import { toast } from "@/components/ui/primitives/Toast";
import type {
  ApparelModelLibraryItem,
  ModelLibraryAgeSegment,
  ModelLibrarySource,
  WorkflowRun,
} from "@/lib/apiClient";
import { useSelectApparelModelLibraryItemMutation } from "@/lib/queries";
import { ModelLibraryBrowser } from "../library/ModelLibraryBrowser";

const AGE_LABEL: Record<ModelLibraryAgeSegment, string> = {
  all: "全部",
  user_favorites: "用户收藏",
  toddler: "幼儿",
  child: "儿童",
  teen: "青少年",
  young_adult: "青年",
  adult: "成年",
  middle_aged: "中老年",
  senior: "老年",
};

const SOURCE_LABEL: Record<ModelLibrarySource, string> = {
  preset: "全站预设",
  favorite: "我的收藏",
  user_upload: "我的上传",
  generated: "生成入库",
};

interface ModelLibraryDialogProps {
  open: boolean;
  workflow: WorkflowRun;
  defaultAgeSegment: ModelLibraryAgeSegment;
  onClose: () => void;
  onGenerateCandidates: () => void;
  generatingCandidates?: boolean;
}

export function ModelLibraryDialog({
  open,
  workflow,
  defaultAgeSegment,
  onClose,
  onGenerateCandidates,
  generatingCandidates = false,
}: ModelLibraryDialogProps) {
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const [selectedItem, setSelectedItem] = useState<ApparelModelLibraryItem | null>(
    null,
  );
  const router = useRouter();

  const selectItem = useSelectApparelModelLibraryItemMutation(workflow.id, {
    onSuccess: () => {
      toast.success("已选入模特候选");
      onClose();
    },
    onError: (err) =>
      toast.error("选择模特失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      }),
  });

  useEffect(() => {
    if (!open) return;
    const previousOverflow = document.body.style.overflow;
    const previousActive = document.activeElement as HTMLElement | null;
    document.body.style.overflow = "hidden";
    const raf = requestAnimationFrame(() => dialogRef.current?.focus({ preventScroll: true }));
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
        return;
      }
      // Tab / Shift+Tab 在 dialog 内循环（焦点陷阱）
      if (event.key !== "Tab") return;
      const dialog = dialogRef.current;
      if (!dialog) return;
      const focusables = dialog.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
      );
      if (focusables.length === 0) {
        event.preventDefault();
        dialog.focus();
        return;
      }
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      const active = document.activeElement as HTMLElement | null;
      if (event.shiftKey) {
        if (active === first || !dialog.contains(active)) {
          event.preventDefault();
          last.focus();
        }
      } else if (active === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      cancelAnimationFrame(raf);
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = previousOverflow;
      previousActive?.focus?.();
    };
  }, [onClose, open]);

  const chooseSelected = () => {
    if (!selectedItem) return;
    selectItem.mutate(selectedItem.id);
  };

  const openFullLibrary = () => {
    onClose();
    router.push("/projects/library");
  };

  return (
    <AnimatePresence>
      {open ? (
        <motion.div
          className="fixed inset-0 z-[var(--z-dialog)] flex items-end justify-center bg-black/60 p-0 backdrop-blur-md md:items-center md:p-5"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.16 }}
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) onClose();
          }}
        >
          <motion.div
            ref={dialogRef}
            role="dialog"
            aria-modal="true"
            aria-label="模特库"
            tabIndex={-1}
            initial={{ opacity: 0, y: 24, scale: 0.98 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 18, scale: 0.98 }}
            transition={{ duration: 0.22, ease: [0.22, 1, 0.36, 1] }}
            className="flex h-[92dvh] w-full max-w-6xl flex-col overflow-hidden rounded-t-xl border border-[var(--border)] bg-[var(--bg-0)] shadow-[var(--shadow-2)] focus:outline-none md:h-[82vh] md:rounded-md"
          >
            <header className="flex shrink-0 items-center justify-between gap-3 border-b border-[var(--border)] bg-white/[0.035] px-4 py-3">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <Library className="h-4 w-4 text-[var(--amber-300)]" />
                  <h2 className="text-base font-semibold text-[var(--fg-0)]">模特库</h2>
                </div>
                <p className="mt-0.5 text-xs text-[var(--fg-2)]">
                  从全站预设、我的收藏、上传或已入库的生成模特中挑一位。
                </p>
              </div>
              <div className="flex shrink-0 items-center gap-2">
                <Button
                  size="sm"
                  variant="outline"
                  onClick={openFullLibrary}
                  rightIcon={<ArrowUpRight className="h-3.5 w-3.5" />}
                >
                  打开完整模特库
                </Button>
                <button
                  type="button"
                  onClick={onClose}
                  className="inline-flex h-9 w-9 cursor-pointer items-center justify-center rounded-md border border-[var(--border)] text-[var(--fg-1)] transition-colors hover:bg-white/8 hover:text-[var(--fg-0)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/50"
                  aria-label="关闭模特库"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>
            </header>

            <ModelLibraryBrowser
              workflow={workflow}
              mode="dialog"
              defaultAgeSegment={defaultAgeSegment}
              onSelectItem={setSelectedItem}
              showSourceSidebar
              showHeader={false}
              className="min-h-0 flex-1"
            />

            <footer className="flex shrink-0 flex-col gap-2 border-t border-[var(--border)] bg-[var(--bg-0)] px-4 py-3 md:flex-row md:items-center md:justify-between">
              <div className="min-w-0 text-xs text-[var(--fg-2)]">
                {selectedItem ? (
                  <span>
                    已选 {selectedItem.title} · {SOURCE_LABEL[selectedItem.source]} ·{" "}
                    {AGE_LABEL[selectedItem.age_segment]}
                  </span>
                ) : (
                  <span>选择一个库内模特后，会创建 ready 候选并回到现有确认流程。</span>
                )}
              </div>
              <div className="flex gap-2">
                <Button
                  variant="secondary"
                  loading={generatingCandidates}
                  onClick={onGenerateCandidates}
                  leftIcon={<WandSparkles className="h-4 w-4" />}
                >
                  生成模特候选
                </Button>
                <Button
                  variant="primary"
                  loading={selectItem.isPending}
                  disabled={!selectedItem}
                  onClick={chooseSelected}
                  leftIcon={<Check className="h-4 w-4" />}
                >
                  设为当前模特
                </Button>
              </div>
            </footer>
          </motion.div>
        </motion.div>
      ) : null}
    </AnimatePresence>
  );
}
