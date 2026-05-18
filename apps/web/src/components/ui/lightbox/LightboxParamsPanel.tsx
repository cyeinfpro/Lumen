"use client";

// LightboxParamsPanel —— 上拉展开的参数面板。
// 内容：prompt 全文 + revised prompt + 分组元数据（参数差异 / 运行信息 / 文件）。
// 使用共享 BottomSheet 即可（snapPoints=["auto"]），但这里手写一个更贴合的叠加层，
// 因为它叠加在 Lightbox 顶部 chrome 之上，不是全局 tray。

import { AnimatePresence, motion } from "framer-motion";
import { Check, ChevronDown, Copy, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { copyTextToClipboard } from "@/lib/clipboard";
import { cn } from "@/lib/utils";
import { MobileIconButton } from "@/components/ui/primitives/mobile/MobileIconButton";
import { SPRING } from "@/lib/motion";
import type { LightboxItem } from "./types";
import {
  buildLightboxMetadataSections,
  getLightboxRevisedPrompt,
  type LightboxMetadataRow,
} from "./utils";

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
  const [copiedKey, setCopiedKey] = useState<string | null>(null);
  const [revisedOpenItemId, setRevisedOpenItemId] = useState<string | null>(null);
  const promptCopied = open && item ? copiedKey === `${item.id}:prompt` : false;
  const revisedCopied = open && item ? copiedKey === `${item.id}:revised` : false;
  const revisedOpen = open && item ? revisedOpenItemId === item.id : false;
  const metadataSections = useMemo(
    () => (item ? buildLightboxMetadataSections(item) : []),
    [item],
  );
  const revisedPrompt = useMemo(
    () => (item ? getLightboxRevisedPrompt(item) : null),
    [item],
  );

  const handleCopy = () => {
    if (!item?.prompt) return;
    if (onCopyPrompt) {
      onCopyPrompt();
    } else {
      void copyTextToClipboard(item.prompt);
    }
    setCopiedKey(`${item.id}:prompt`);
    window.setTimeout(() => setCopiedKey(null), 1400);
  };

  const handleCopyRevisedPrompt = () => {
    if (!item || !revisedPrompt) return;
    void copyTextToClipboard(revisedPrompt);
    setCopiedKey(`${item.id}:revised`);
    window.setTimeout(() => setCopiedKey(null), 1400);
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
            "mobile-dialog-sheet mobile-dialog-scroll safe-x pb-[var(--mobile-dialog-footer-pad-bottom)]",
            "max-h-[min(70dvh,var(--mobile-dialog-max-height))] overflow-y-auto overscroll-contain",
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
            {(item.prompt || revisedPrompt) && (
              <section className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-2)]/55 p-3">
                {item.prompt && (
                  <>
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
                        {promptCopied ? (
                          <Check className="w-3.5 h-3.5" />
                        ) : (
                          <Copy className="w-3.5 h-3.5" />
                        )}
                        {promptCopied ? "已复制" : "复制"}
                      </button>
                    </div>
                    <p className="text-[15px] leading-relaxed text-[var(--fg-0)] whitespace-pre-wrap break-words">
                      {item.prompt}
                    </p>
                  </>
                )}
                {revisedPrompt && (
                  <div className={cn("mt-3 border-t border-[var(--border-subtle)] pt-2.5", !item.prompt && "mt-0 border-t-0 pt-0")}>
                    <div className="flex items-center justify-between gap-2">
                      <button
                        type="button"
                        onClick={() =>
                          setRevisedOpenItemId((value) =>
                            value === item.id ? null : item.id,
                          )
                        }
                        aria-expanded={revisedOpen}
                        className={cn(
                          "inline-flex min-h-9 min-w-0 flex-1 items-center gap-2 rounded-lg px-2 text-left",
                          "text-[12px] font-medium text-[var(--fg-1)] transition-colors",
                          "hover:bg-[var(--bg-3)] hover:text-[var(--fg-0)]",
                          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
                        )}
                      >
                        <ChevronDown
                          className={cn(
                            "h-3.5 w-3.5 shrink-0 transition-transform",
                            revisedOpen && "rotate-180",
                          )}
                        />
                        <span className="truncate">模型改写后的提示词</span>
                      </button>
                      <button
                        type="button"
                        onClick={handleCopyRevisedPrompt}
                        className={cn(
                          "inline-flex min-h-9 shrink-0 items-center gap-1.5 rounded-full px-3",
                          "text-[12px] text-[var(--fg-1)] transition-colors",
                          "hover:bg-[var(--bg-3)] active:text-[var(--amber-400)]",
                          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
                        )}
                      >
                        {revisedCopied ? (
                          <Check className="w-3.5 h-3.5" />
                        ) : (
                          <Copy className="w-3.5 h-3.5" />
                        )}
                        {revisedCopied ? "已复制" : "复制"}
                      </button>
                    </div>
                    <AnimatePresence initial={false}>
                      {revisedOpen && (
                        <motion.p
                          key="revised-prompt"
                          initial={reducedMotion ? { opacity: 0 } : { height: 0, opacity: 0 }}
                          animate={reducedMotion ? { opacity: 1 } : { height: "auto", opacity: 1 }}
                          exit={reducedMotion ? { opacity: 0 } : { height: 0, opacity: 0 }}
                          transition={{ duration: 0.18, ease: "easeOut" }}
                          className="overflow-hidden whitespace-pre-wrap break-words px-2 pt-1 text-[14px] leading-relaxed text-[var(--fg-0)]"
                        >
                          {revisedPrompt}
                        </motion.p>
                      )}
                    </AnimatePresence>
                  </div>
                )}
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
                      row={row}
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

function ParamRow({ row }: { row: LightboxMetadataRow }) {
  return (
    <div className="grid min-w-0 grid-cols-[5.5rem_minmax(0,1fr)] items-baseline gap-2.5 py-0.5">
      <span className="text-[var(--fg-2)] uppercase text-[10px] tracking-wider font-medium">
        {row.label}
      </span>
      <span className="flex min-w-0 flex-wrap items-center gap-1.5 break-words text-[13px] text-[var(--fg-0)]">
        <span className="min-w-0 break-words">{row.value}</span>
        {row.badge ? (
          <span className="shrink-0 rounded-full border border-[var(--color-lumen-amber)]/35 bg-[var(--color-lumen-amber)]/12 px-1.5 py-0.5 font-sans text-[10px] font-medium text-[var(--fg-0)]">
            {row.badge}
          </span>
        ) : null}
      </span>
    </div>
  );
}
