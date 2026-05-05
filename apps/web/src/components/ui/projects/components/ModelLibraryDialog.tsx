"use client";

// 项目流程内的"选模特"弹窗：
//  - 浏览部分（年龄 tab / 来源 filter / grid / 上传）抽到 ModelLibraryBrowser
//  - 这里只保留 dialog 外壳；卡片点击 = 打开 Lightbox（统一规则）
//  - 选模特通过 Lightbox 内 action（「设为当前模特」）触发，不在 dialog footer 上重复
//  - footer 只剩「关闭」+ 提示文字 +「生成模特候选」（保留旧候选生成入口）
//  - 桌面端：居中 modal；移动端：BottomSheet（snap 88%）
//
// 关键链路：Dialog 打开 → Browser 渲染卡片 → 点卡片 →
//   useUiStore.openLightboxFromItems(items, id, action) → Lightbox 打开 →
//   按 action 按钮 → onSelectItem(item) → 这里 mutate → 成功后关闭 lightbox + dialog

import { ArrowUpRight, Library, WandSparkles, X } from "lucide-react";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef } from "react";

import { Button } from "@/components/ui/primitives/Button";
import { BottomSheet } from "@/components/ui/primitives/mobile/BottomSheet";
import { toast } from "@/components/ui/primitives/Toast";
import { useIsMobile } from "@/hooks/useMediaQuery";
import type {
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
}

export function ModelLibraryDialog({
  open,
  workflow,
  defaultAgeSegment,
  onClose,
  onGenerateCandidates,
  generatingCandidates = false,
}: ModelLibraryDialogProps) {
  const isMobile = useIsMobile();
  const router = useRouter();
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
      selectItem.mutate(item.id);
    },
    [selectItem, setLightboxActionPending],
  );

  const openFullLibrary = () => {
    onClose();
    router.push("/library");
  };

  const headerHint = "点击图片预览，并在大图内选择模特";

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
      className="fixed inset-0 z-[var(--z-dialog)] flex items-center justify-center bg-black/60 p-5 backdrop-blur-md"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="模特库"
        className="flex max-h-[88vh] w-full max-w-6xl flex-col overflow-hidden rounded-md border border-[var(--border)] bg-[var(--bg-0)] shadow-[var(--shadow-2)]"
      >
        <DialogHeader
          onOpenFullLibrary={openFullLibrary}
          onClose={onClose}
          hint={headerHint}
        />
        <div className="min-h-0 flex-1 overflow-y-auto">
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
}: {
  onOpenFullLibrary: () => void;
  onClose: () => void;
  hint: string;
}) {
  return (
    <header className="flex shrink-0 items-center justify-between gap-3 border-b border-[var(--border)] bg-white/[0.035] px-4 py-3">
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <Library className="h-4 w-4 text-[var(--amber-300)]" />
          <h2 className="text-base font-semibold text-[var(--fg-0)]">模特库</h2>
        </div>
        <p className="mt-0.5 text-xs text-[var(--fg-2)]">{hint}</p>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <Button
          size="sm"
          variant="outline"
          onClick={onOpenFullLibrary}
          rightIcon={<ArrowUpRight className="h-3.5 w-3.5" />}
        >
          打开完整模特库
        </Button>
        <button
          type="button"
          onClick={onClose}
          className="inline-flex h-11 min-h-11 w-11 min-w-11 cursor-pointer items-center justify-center rounded-md border border-[var(--border)] text-[var(--fg-1)] transition-colors hover:bg-white/8 hover:text-[var(--fg-0)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/50 md:h-9 md:min-h-0 md:w-9 md:min-w-0"
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
    <footer className="mobile-dialog-footer flex shrink-0 flex-col gap-2 border-t border-[var(--border)] bg-[var(--bg-0)] px-4 py-3 md:flex-row md:items-center md:justify-between md:pb-3">
      <p className="min-w-0 text-xs text-[var(--fg-2)]">
        点击任一图片预览，「设为当前模特」按钮在大图内。
      </p>
      <div className="flex gap-2">
        <Button
          variant="secondary"
          loading={generatingCandidates}
          onClick={onGenerateCandidates}
          leftIcon={<WandSparkles className="h-4 w-4" />}
        >
          生成模特候选
        </Button>
        <Button variant="ghost" onClick={onClose}>
          关闭
        </Button>
      </div>
    </footer>
  );
}
