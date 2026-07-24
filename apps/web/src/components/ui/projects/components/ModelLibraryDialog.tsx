"use client";

// Editorial 项目流程内的"选模特"弹窗：
//  - header 去旧半透明卡片，改为 mono eyebrow + compact title + hairline
//  - 不再用 Library lucide 图标做 prefix；左上角清晰排印自带气场
//  - 桌面 modal 改为轻圆角 + hairline + bg-0 极简底
//  - footer 去厚底 bg；按钮 secondary 改 outline (hairline)
//  - 移动端：BottomSheet 仍用 88% snap，header 同款排印
//
// 关键链路保持：Dialog 打开 → Browser 渲染卡片 → 点卡片 →
//   useUiStore.openLightboxFromItems → Lightbox 打开 → 按 action →
//   onSelectItem(item) → 这里 mutate → 成功后关闭 lightbox + dialog

import { ArrowUpRight, WandSparkles, X } from "lucide-react";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useId, useRef } from "react";

import { Button } from "@/components/ui/primitives/Button";
import { BottomSheet } from "@/components/ui/primitives/mobile/BottomSheet";
import { useModalLayer } from "@/components/ui/primitives/mobile/useModalLayer";
import { toast } from "@/components/ui/primitives/Toast";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import { useIsMobile } from "@/hooks/useMediaQuery";
import type {
  AccessoryPlan,
  ApparelModelLibraryItem,
  ModelLibraryAgeSegment,
  WorkflowRun,
} from "@/lib/apiClient";
import { useSelectApparelModelLibraryItemMutation } from "@/lib/queries";
import { useUiStore } from "@/store/useUiStore";
import { ModelLibraryBrowser } from "../library/ModelLibraryBrowser";

interface ModelLibraryDialogProps {
  open: boolean;
  workflow: WorkflowRun;
  defaultAgeSegment: ModelLibraryAgeSegment;
  onClose: () => void;
  onGenerateCandidates: () => void;
  generatingCandidates?: boolean;
  selectionAccessoryPlan?: AccessoryPlan;
  selectionStylePrompt?: string;
}

export function ModelLibraryDialog({
  open,
  workflow,
  defaultAgeSegment,
  onClose,
  onGenerateCandidates,
  generatingCandidates = false,
  selectionAccessoryPlan,
  selectionStylePrompt,
}: ModelLibraryDialogProps) {
  const isMobile = useIsMobile();
  const router = useRouter();
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const titleId = useId();
  const descriptionId = useId();
  const closeLightbox = useUiStore((s) => s.closeLightbox);
  const setLightboxActionPending = useUiStore((s) => s.setLightboxActionPending);

  const selectItem = useSelectApparelModelLibraryItemMutation(workflow.id, {
    onSuccess: () => {
      toast.success("已选入模特候选");
      closeLightbox();
      onClose();
    },
    onError: (err) => {
      setLightboxActionPending(false);
      toast.error("选择模特失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      });
    },
  });

  // 关闭时同步关闭 lightbox（避免 dialog 关后 lightbox 还挂着孤儿 action）
  // 仅当本组件之前打开过、随后被合上时才执行；首次 mount 不无差别 closeLightbox。
  const wasOpenRef = useRef(false);
  useEffect(() => {
    if (open) {
      wasOpenRef.current = true;
      return;
    }
    if (!wasOpenRef.current) return;
    wasOpenRef.current = false;
    closeLightbox();
  }, [closeLightbox, open]);

  // Browser 把 lightbox action 路由到这里：调 mutate(item.id) + 同步 pending
  const handleSelect = useCallback(
    (item: ApparelModelLibraryItem) => {
      setLightboxActionPending(true);
      selectItem.mutate({
        library_item_id: item.id,
        accessory_plan: selectionAccessoryPlan,
        style_prompt: selectionStylePrompt,
      });
    },
    [selectItem, selectionAccessoryPlan, selectionStylePrompt, setLightboxActionPending],
  );

  const openFullLibrary = () => {
    onClose();
    router.push("/library");
  };

  const headerHint = "可直接在卡片下方选择模特，也可以点图片预览后在大图内选择";
  const onDialogKeyDown = useModalLayer({
    open: isMobile === false && open,
    rootRef: dialogRef,
    onClose,
  });
  useBodyScrollLock(isMobile === false && open);

  if (isMobile === null) {
    // SSR / hydration 第一帧：不渲染外壳避免 flash
    return null;
  }

  if (isMobile) {
    return (
      <BottomSheet
        open={open}
        onClose={onClose}
        ariaLabel="模特库"
        snapPoints={["88%"]}
      >
        <div className="flex h-full min-h-0 flex-col">
          <DialogHeader
            onOpenFullLibrary={openFullLibrary}
            onClose={onClose}
            hint={headerHint}
            titleId={titleId}
            descriptionId={descriptionId}
          />
          <div className="min-h-0 flex-1 overflow-hidden">
            <ModelLibraryBrowser
              workflow={workflow}
              mode="dialog"
              defaultAgeSegment={defaultAgeSegment}
              onSelectItem={handleSelect}
              showSourceSidebar
              showHeader={false}
              className="min-h-0 flex-1"
            />
          </div>
          <DialogFooter
            onClose={onClose}
            onGenerateCandidates={onGenerateCandidates}
            generatingCandidates={generatingCandidates}
          />
        </div>
      </BottomSheet>
    );
  }

  // 桌面端：自定义居中 modal（flex 列布局，header / footer 固定，body 滚动）
  if (!open) return null;
  return (
    <div
      className="mobile-dialog-shell fixed inset-0 z-[var(--z-dialog)] flex items-center justify-center bg-black/60 backdrop-blur-md"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descriptionId}
        tabIndex={-1}
        onKeyDown={onDialogKeyDown}
        className="mobile-dialog-panel flex h-[min(90dvh,860px)] min-h-0 w-full max-w-6xl flex-col overflow-hidden rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)] shadow-[var(--shadow-2)]"
      >
        <DialogHeader
          onOpenFullLibrary={openFullLibrary}
          onClose={onClose}
          hint={headerHint}
          titleId={titleId}
          descriptionId={descriptionId}
        />
        <div className="mobile-dialog-scroll min-h-0 flex-1 overflow-y-auto">
          <ModelLibraryBrowser
            workflow={workflow}
            mode="dialog"
            defaultAgeSegment={defaultAgeSegment}
            onSelectItem={handleSelect}
            showSourceSidebar
            showHeader={false}
            className="min-h-0 flex-1"
          />
        </div>
        <DialogFooter
          onClose={onClose}
          onGenerateCandidates={onGenerateCandidates}
          generatingCandidates={generatingCandidates}
        />
      </div>
    </div>
  );
}

function DialogHeader({
  onOpenFullLibrary,
  onClose,
  hint,
  titleId,
  descriptionId,
}: {
  onOpenFullLibrary: () => void;
  onClose: () => void;
  hint: string;
  titleId: string;
  descriptionId: string;
}) {
  return (
    <header className="flex shrink-0 items-start justify-between gap-4 border-b border-[var(--border)] px-5 py-4 md:px-6 md:py-5">
      <div className="min-w-0">
        <p className="type-page-kicker">
          模特库
        </p>
        <h2 id={titleId} className="type-page-title-sm mt-1.5 md:text-[24px]">
          模特库
        </h2>
        <p id={descriptionId} className="type-page-subtitle mt-2 max-w-md">
          {hint}
        </p>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <Button
          size="sm"
          variant="outline"
          onClick={onOpenFullLibrary}
          rightIcon={<ArrowUpRight className="h-3.5 w-3.5" />}
          className="max-sm:hidden"
        >
          打开完整模特库
        </Button>
        <button
          type="button"
          onClick={onClose}
          className="inline-flex h-11 w-11 cursor-pointer items-center justify-center rounded-full text-[var(--fg-1)] transition-colors hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/50 sm:h-10 sm:w-10"
          aria-label="关闭模特库"
        >
          <X className="h-4 w-4" />
        </button>
      </div>
    </header>
  );
}

function DialogFooter({
  onClose,
  onGenerateCandidates,
  generatingCandidates,
}: {
  onClose: () => void;
  onGenerateCandidates: () => void;
  generatingCandidates: boolean;
}) {
  return (
    <footer className="mobile-dialog-footer flex shrink-0 flex-col gap-3 border-t border-[var(--border)] bg-[var(--bg-0)] px-4 py-3 md:flex-row md:items-center md:justify-between md:px-6 md:py-4">
      <p className="min-w-0 font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--fg-2)]">
        Tip · 卡片下方和大图内都可以设为当前模特
      </p>
      <div className="grid grid-cols-1 gap-2 min-[420px]:grid-cols-2 md:flex md:flex-row">
        <Button
          variant="outline"
          loading={generatingCandidates}
          onClick={onGenerateCandidates}
          leftIcon={<WandSparkles className="h-4 w-4" />}
          className="w-full md:w-auto"
        >
          生成模特候选
        </Button>
        <Button variant="ghost" onClick={onClose} className="w-full md:w-auto">
          关闭
        </Button>
      </div>
    </footer>
  );
}
