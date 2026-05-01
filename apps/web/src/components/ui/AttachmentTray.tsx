"use client";

// DESIGN §4.2 / §22.1：参考图堆叠，第一张为 primary。
// - 横向滚动 chip；第一张 amber ring + PRIMARY 徽标
// - 每张 hover scale-105；移除按钮（×）hover 变红
// - 左右边缘根据滚动位置显示 fade 遮罩；3 张以上默认出右侧 fade

import { AnimatePresence, motion } from "framer-motion";
import { X } from "lucide-react";
import { useChatStore } from "@/store/useChatStore";
import { cn } from "@/lib/utils";
import { useEffect, useRef, useState } from "react";
import { toast } from "@/components/ui/primitives";

export function AttachmentTray() {
  const attachments = useChatStore((s) => s.composer.attachments);
  const removeAttachment = useChatStore((s) => s.removeAttachment);
  const addAttachment = useChatStore((s) => s.addAttachment);

  const scrollerRef = useRef<HTMLDivElement | null>(null);
  const [fadeLeft, setFadeLeft] = useState(false);
  const [fadeRight, setFadeRight] = useState(false);

  // 监听滚动位置，决定是否显示左右渐变遮罩
  useEffect(() => {
    const el = scrollerRef.current;
    if (!el) return;
    const update = () => {
      const { scrollLeft, scrollWidth, clientWidth } = el;
      setFadeLeft(scrollLeft > 8);
      setFadeRight(scrollLeft + clientWidth < scrollWidth - 8);
    };
    update();
    el.addEventListener("scroll", update, { passive: true });
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => {
      el.removeEventListener("scroll", update);
      ro.disconnect();
    };
  }, [attachments.length]);

  if (attachments.length === 0) return null;

  return (
    <div className="relative">
      <div
        ref={scrollerRef}
        className="flex items-center gap-2 overflow-x-auto scroll-smooth pb-1 snap-x snap-mandatory"
        role="list"
        aria-label="参考图列表"
      >
        <AnimatePresence initial={false}>
          {attachments.map((att, idx) => (
            <motion.div
              key={att.id}
              role="listitem"
              initial={{ opacity: 0, scale: 0.82, y: 6 }}
              animate={{ opacity: 1, scale: 1, y: 0 }}
              exit={{ opacity: 0, scale: 0.82, y: -4 }}
              transition={{
                type: "spring",
                damping: 24,
                stiffness: 320,
                delay: idx * 0.03,
              }}
              whileHover={{ scale: 1.05 }}
              className={cn(
                "relative shrink-0 h-16 w-16 sm:h-20 sm:w-20 rounded-xl overflow-hidden snap-start",
                "border bg-neutral-800 cursor-default",
                "transition-[border-color,box-shadow] duration-150",
                idx === 0
                  ? "border-[var(--color-lumen-amber)]/60 ring-1 ring-[var(--color-lumen-amber)]/40 shadow-[0_0_14px_rgba(242,169,58,0.18)]"
                  : "border-white/10 hover:border-white/25",
              )}
              title={
                att.kind === "generated"
                  ? "先前生成的参考图"
                  : "上传的参考图"
              }
            >
              {/* 使用原生 <img>：attachment.data_url 已是 dataURL，next/image 无意义 */}
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={att.data_url}
                alt={
                  att.kind === "generated"
                    ? "generated reference"
                    : "uploaded reference"
                }
                className="h-full w-full object-cover"
                draggable={false}
              />

              {idx === 0 && (
                <span
                  className={cn(
                    "absolute left-1 top-1 px-1.5 py-0.5 rounded-md",
                    "text-[9px] font-bold tracking-wider uppercase",
                    "bg-[var(--color-lumen-amber)] text-black",
                    "shadow-[0_2px_6px_rgba(0,0,0,0.35)]",
                  )}
                  aria-label="主参考图"
                >
                  Primary
                </span>
              )}

              <button
                type="button"
                onClick={() => {
                  // 暂存被删的 attachment 以便 toast undo 还原
                  const removed = att;
                  removeAttachment(att.id);
                  toast.info("已移除参考图", {
                    action: {
                      label: "撤销",
                      onClick: () => addAttachment(removed),
                    },
                  });
                }}
                aria-label={`移除参考图 ${idx + 1}`}
                title="移除"
                className={cn(
                  // 移动端放大到 7×7 + 外圈 hit-area，防止误触命中附件本身
                  "absolute right-1 top-1 h-7 w-7 sm:h-6 sm:w-6 rounded-full",
                  "inline-flex items-center justify-center",
                  "bg-black/65 text-white backdrop-blur-sm border border-white/10",
                  "hover:bg-red-500/85 hover:border-red-300/40",
                  "active:scale-[0.92] transition-all duration-150",
                  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-400/70",
                )}
              >
                <X className="h-3 w-3" />
              </button>
            </motion.div>
          ))}
        </AnimatePresence>
      </div>

      {/* 左右渐变遮罩：根据滚动状态动态显示 */}
      <AnimatePresence>
        {fadeLeft && (
          <motion.div
            key="fade-left"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            aria-hidden
            className="pointer-events-none absolute top-0 left-0 h-full w-6 bg-gradient-to-r from-neutral-900/95 to-transparent"
          />
        )}
        {fadeRight && (
          <motion.div
            key="fade-right"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            aria-hidden
            className="pointer-events-none absolute top-0 right-0 h-full w-6 bg-gradient-to-l from-neutral-900/95 to-transparent"
          />
        )}
      </AnimatePresence>
    </div>
  );
}
