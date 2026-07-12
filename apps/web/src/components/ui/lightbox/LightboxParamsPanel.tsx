"use client";

// LightboxParamsPanel —— 上拉展开的参数面板。
// 内容：prompt 全文 + revised prompt + 分组元数据（参数差异 / 运行信息 / 文件）。
// 使用共享 BottomSheet 即可（snapPoints=["auto"]），但这里手写一个更贴合的叠加层，
// 因为它叠加在 Lightbox 顶部 chrome 之上，不是全局 tray。

import { AnimatePresence, motion } from "framer-motion";
import { X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { cn } from "@/lib/utils";
import { MobileIconButton } from "@/components/ui/primitives/mobile/MobileIconButton";
import { SPRING } from "@/lib/motion";
import type { LightboxItem } from "./types";
import { LightboxDetailsContent } from "./LightboxDetailsContent";

export interface LightboxParamsPanelProps {
  open: boolean;
  onClose: () => void;
  item: LightboxItem | null;
  onCopyPrompt?: () => void;
}

function usePanelReducedMotion(): boolean {
  const [reduced, setReduced] = useState(false);
  useEffect(() => {
    if (typeof window === "undefined") return;
    const mql = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReduced(mql.matches);
    update();
    mql.addEventListener("change", update);
    return () => mql.removeEventListener("change", update);
  }, []);
  return reduced;
}

export function LightboxParamsPanel({
  open,
  onClose,
  item,
  onCopyPrompt,
}: LightboxParamsPanelProps) {
  const reducedMotion = usePanelReducedMotion();
  const panelRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const frame = window.requestAnimationFrame(() => {
      panelRef.current?.focus({ preventScroll: true });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [open]);

  return (
    <AnimatePresence>
      {open && item && (
        <motion.div
          key="lightbox-params-panel"
          ref={panelRef}
          role="dialog"
          aria-modal="false"
          aria-label="图片参数"
          tabIndex={-1}
          className={cn(
            "fixed inset-x-0 bottom-0 z-[var(--z-dialog,90)]",
            "rounded-t-[var(--radius-sheet)] bg-[var(--bg-1)]/96 backdrop-blur-2xl",
            "border-t border-[var(--border-subtle)]",
            "mobile-dialog-sheet mobile-dialog-scroll safe-x pb-[var(--mobile-dialog-footer-pad-bottom)]",
            "max-h-[min(70dvh,var(--mobile-dialog-max-height))] overflow-y-auto overscroll-contain",
            "[@media(orientation:landscape)_and_(max-height:500px)]:max-h-[var(--mobile-dialog-max-height)]",
          )}
          initial={reducedMotion ? { opacity: 0 } : { y: "100%" }}
          animate={reducedMotion ? { opacity: 1 } : { y: 0 }}
          exit={reducedMotion ? { opacity: 0 } : { y: "100%" }}
          transition={
            reducedMotion
              ? { duration: 0.18, ease: "linear" }
              : SPRING.sheet
          }
        >
          <div className="flex items-center justify-between px-4 pt-3.5">
            <div className="w-9 h-1 rounded-full bg-[var(--fg-3)]/50 mx-auto" />
          </div>
          <div className="flex items-center justify-between px-4 pt-3 pb-1.5">
            <div>
              <div className="text-[14px] font-semibold text-[var(--fg-0)]">
                图片信息
              </div>
              <div className="mt-0.5 font-mono text-[10px] text-[var(--fg-2)] tracking-wide">
                {item.id}
              </div>
            </div>
            <MobileIconButton
              icon={<X className="w-4 h-4" />}
              label="关闭参数面板"
              onPress={onClose}
            />
          </div>

          <LightboxDetailsContent
            item={item}
            onCopyPrompt={onCopyPrompt}
            className="px-4 pb-5"
          />
        </motion.div>
      )}
    </AnimatePresence>
  );
}
