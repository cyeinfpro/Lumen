"use client";

// LightboxParamsPanel —— 上拉展开的参数面板。
// 内容：prompt 全文 + 分组元数据（生成参数 / 文件信息 / 记录）。
// 使用共享 BottomSheet 即可（snapPoints=["auto"]），但这里手写一个更贴合的叠加层，
// 因为它叠加在 Lightbox 顶部 chrome 之上，不是全局 tray。

import { AnimatePresence, motion } from "framer-motion";
import { Check, Copy, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { cn } from "@/lib/utils";
import { MobileIconButton } from "@/components/ui/primitives/mobile/MobileIconButton";
import { SPRING } from "@/lib/motion";
import type { LightboxItem } from "./types";
import { buildLightboxMetadataSections } from "./utils";

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
  const [copiedItemId, setCopiedItemId] = useState<string | null>(null);
  const copied = open && item?.id === copiedItemId;
  const metadataSections = useMemo(
    () => (item ? buildLightboxMetadataSections(item) : []),
    [item],
  );

  const handleCopy = () => {
    if (!item?.prompt) return;
    if (onCopyPrompt) {
      onCopyPrompt();
    } else {
      void navigator.clipboard?.writeText(item.prompt);
    }
    setCopiedItemId(item?.id ?? null);
    window.setTimeout(() => setCopiedItemId(null), 1400);
  };

  return (
    <AnimatePresence>
      {open && item && (
        <motion.div
          key="lightbox-params-panel"
          role="dialog"
          aria-modal="false"
          aria-label="图片参数"
          className={cn(
            "fixed inset-x-0 bottom-0 z-[var(--z-dialog,90)]",
            "rounded-t-2xl bg-[var(--bg-1)]/96 backdrop-blur-2xl",
            "border-t border-[var(--border-subtle)]",
            "pb-[max(1rem,env(safe-area-inset-bottom))]",
            "max-h-[70vh] overflow-y-auto",
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

          <div className="px-4 pb-5 space-y-3.5">
            {item.prompt && (
              <section className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-2)]/55 p-3">
                <div className="flex items-center justify-between mb-1">
                  <h3 className="text-[11px] font-mono uppercase tracking-wide text-[var(--fg-2)]">
                    prompt
                  </h3>
                  <button
                    type="button"
                    onClick={handleCopy}
                    className={cn(
                      "inline-flex min-h-9 items-center gap-1.5 rounded-full px-3",
                      "text-[12px] text-[var(--fg-1)] transition-colors",
                      "hover:bg-[var(--bg-3)] active:text-[var(--amber-400)]",
                      "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
                    )}
                    aria-live="polite"
                  >
                    {copied ? (
                      <Check className="w-3.5 h-3.5" />
                    ) : (
                      <Copy className="w-3.5 h-3.5" />
                    )}
                    {copied ? "已复制" : "复制"}
                  </button>
                </div>
                <p className="text-[15px] leading-relaxed text-[var(--fg-0)] whitespace-pre-wrap break-words">
                  {item.prompt}
                </p>
              </section>
            )}

            {metadataSections.map((section) => (
              <section
                key={section.title}
                className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-2)]/45 p-3"
              >
                <h3 className="mb-2 text-[11px] font-mono uppercase tracking-wide text-[var(--fg-2)]">
                  {section.title}
                </h3>
                <div className="grid grid-cols-1 gap-2 font-mono text-xs text-[var(--fg-1)]">
                  {section.rows.map((row) => (
                    <ParamRow
                      key={`${section.title}-${row.label}`}
                      label={row.label}
                      value={row.value}
                    />
                  ))}
                </div>
              </section>
            ))}
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}

function ParamRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="grid min-w-0 grid-cols-[5.5rem_minmax(0,1fr)] items-baseline gap-2.5 py-0.5">
      <span className="text-[var(--fg-2)] uppercase text-[10px] tracking-wider font-medium">
        {label}
      </span>
      <span className="min-w-0 break-words text-[var(--fg-0)] text-[13px]">{value}</span>
    </div>
  );
}
