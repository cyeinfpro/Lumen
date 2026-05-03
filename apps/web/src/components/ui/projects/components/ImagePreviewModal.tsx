"use client";

// 升级版大图预览：
// 1) 多图列表 + 上下导航（← →）+ N/M 计数
// 2) 键盘 ESC 关闭、Z 切换原图/适应、+ - 缩放、D 下载
// 3) 不再"点遮罩任意位置就关"，避免误触。点遮罩只关闭非 image / 非控件区域
// 4) Body scroll-lock + 焦点回收 + AnimatePresence

import { AnimatePresence, motion } from "framer-motion";
import { ChevronLeft, ChevronRight, Download, Maximize2, Minimize2, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { cn } from "@/lib/utils";
import type { BackendImageMeta } from "@/lib/apiClient";
import { canDownload, imageSrc } from "../utils";

// 切换图片时把缩放模式重置回 contain。沿用 React 推荐的 "render-phase reset" 模式
// （prevValue 比对 + setState），避免 effect 中 setState 触发额外渲染。

interface ImagePreviewModalProps {
  images: BackendImageMeta[];
  index: number;
  onClose: () => void;
  onIndexChange?: (next: number) => void;
}

export function ImagePreviewModal({
  images,
  index,
  onClose,
  onIndexChange,
}: ImagePreviewModalProps) {
  const open = images.length > 0 && index >= 0 && index < images.length;
  const overlayRef = useRef<HTMLDivElement>(null);
  const previousActiveRef = useRef<HTMLElement | null>(null);
  const [fitMode, setFitMode] = useState<"contain" | "actual">("contain");
  const [trackedIndex, setTrackedIndex] = useState(index);
  if (trackedIndex !== index) {
    setTrackedIndex(index);
    setFitMode("contain");
  }
  const current = open ? images[index] : null;

  const goPrev = useCallback(() => {
    if (!open) return;
    onIndexChange?.((index - 1 + images.length) % images.length);
  }, [images.length, index, onIndexChange, open]);

  const goNext = useCallback(() => {
    if (!open) return;
    onIndexChange?.((index + 1) % images.length);
  }, [images.length, index, onIndexChange, open]);

  useEffect(() => {
    if (!open) return;
    previousActiveRef.current = (document.activeElement as HTMLElement) ?? null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const raf = requestAnimationFrame(() => overlayRef.current?.focus({ preventScroll: true }));

    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
      } else if (event.key === "ArrowLeft") {
        event.preventDefault();
        goPrev();
      } else if (event.key === "ArrowRight") {
        event.preventDefault();
        goNext();
      } else if (event.key === "z" || event.key === "Z") {
        setFitMode((mode) => (mode === "contain" ? "actual" : "contain"));
      } else if (event.key === "d" || event.key === "D") {
        if (current) {
          const url = canDownload(current);
          if (url) {
            const anchor = document.createElement("a");
            anchor.href = url;
            anchor.download = "";
            anchor.rel = "noopener";
            document.body.appendChild(anchor);
            anchor.click();
            document.body.removeChild(anchor);
          }
        }
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      cancelAnimationFrame(raf);
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = previousOverflow;
      previousActiveRef.current?.focus?.();
    };
  }, [current, goNext, goPrev, onClose, open]);

  const downloadHref = useMemo(() => (current ? canDownload(current) : null), [current]);

  return (
    <AnimatePresence>
      {open && current ? (
        <motion.div
          ref={overlayRef}
          tabIndex={-1}
          role="dialog"
          aria-modal="true"
          aria-label="图片预览"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.16 }}
          className="fixed inset-0 z-[var(--z-lightbox)] flex items-center justify-center bg-black/85 p-4 backdrop-blur-md focus:outline-none"
          onMouseDown={(event) => {
            // 仅当点击遮罩本身（不是控件、不是图片容器）才关闭
            if (event.target === event.currentTarget) onClose();
          }}
        >
          <motion.figure
            key={current.id}
            initial={{ opacity: 0, scale: 0.97 }}
            animate={{ opacity: 1, scale: 1 }}
            exit={{ opacity: 0, scale: 0.98 }}
            transition={{ duration: 0.22, ease: [0.22, 1, 0.36, 1] }}
            className="relative flex max-h-[88dvh] max-w-[92vw] items-center justify-center"
          >
            <img
              src={imageSrc(current)}
              alt="大图预览"
              className={cn(
                "select-none rounded-md object-contain shadow-[var(--shadow-3)]",
                fitMode === "actual"
                  ? "max-h-none max-w-none"
                  : "max-h-[88dvh] max-w-[92vw]",
              )}
              draggable={false}
              onClick={(event) => event.stopPropagation()}
            />
            <figcaption className="sr-only">{current.id}</figcaption>
          </motion.figure>

          {/* 顶部计数 + 关闭 */}
          <div className="pointer-events-none absolute inset-x-0 top-3 flex items-center justify-between px-3 md:px-5">
            <div className="pointer-events-auto inline-flex items-center gap-2 rounded-full border border-white/15 bg-black/60 px-3 py-1 text-xs tabular-nums text-white">
              {index + 1} / {images.length}
            </div>
            <div className="pointer-events-auto flex items-center gap-2">
              <button
                type="button"
                onClick={() => setFitMode((m) => (m === "contain" ? "actual" : "contain"))}
                className="inline-flex h-9 items-center gap-1.5 rounded-md border border-white/15 bg-black/60 px-3 text-xs text-white transition-colors hover:bg-black/80"
                aria-label={fitMode === "contain" ? "切换原始尺寸 (Z)" : "适应屏幕 (Z)"}
              >
                {fitMode === "contain" ? (
                  <>
                    <Maximize2 className="h-3.5 w-3.5" />
                    原始尺寸
                  </>
                ) : (
                  <>
                    <Minimize2 className="h-3.5 w-3.5" />
                    适应屏幕
                  </>
                )}
              </button>
              {downloadHref ? (
                <a
                  href={downloadHref}
                  download
                  rel="noopener"
                  className="inline-flex h-9 items-center gap-1.5 rounded-md border border-white/15 bg-black/60 px-3 text-xs text-white transition-colors hover:bg-black/80"
                  aria-label="下载 (D)"
                >
                  <Download className="h-3.5 w-3.5" />
                  下载
                </a>
              ) : null}
              <button
                type="button"
                onClick={onClose}
                aria-label="关闭 (Esc)"
                className="inline-flex h-9 w-9 items-center justify-center rounded-md border border-white/15 bg-black/60 text-white transition-colors hover:bg-black/80"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
          </div>

          {/* 左右导航 */}
          {images.length > 1 ? (
            <>
              <button
                type="button"
                onClick={goPrev}
                aria-label="上一张 (←)"
                className="absolute left-3 top-1/2 inline-flex h-12 w-12 -translate-y-1/2 items-center justify-center rounded-full border border-white/15 bg-black/55 text-white transition-colors hover:bg-black/80 md:left-6"
              >
                <ChevronLeft className="h-6 w-6" />
              </button>
              <button
                type="button"
                onClick={goNext}
                aria-label="下一张 (→)"
                className="absolute right-3 top-1/2 inline-flex h-12 w-12 -translate-y-1/2 items-center justify-center rounded-full border border-white/15 bg-black/55 text-white transition-colors hover:bg-black/80 md:right-6"
              >
                <ChevronRight className="h-6 w-6" />
              </button>
            </>
          ) : null}

          {/* 底部快捷键提示（桌面） */}
          <div className="pointer-events-none absolute inset-x-0 bottom-3 hidden items-center justify-center md:flex">
            <div className="rounded-full border border-white/10 bg-black/55 px-3 py-1 text-[11px] text-white/70 backdrop-blur">
              ← → 切换 · Z 缩放 · D 下载 · Esc 关闭
            </div>
          </div>
        </motion.div>
      ) : null}
    </AnimatePresence>
  );
}

