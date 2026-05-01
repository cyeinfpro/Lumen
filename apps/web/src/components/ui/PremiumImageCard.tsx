"use client";

// 生成图卡片（DESIGN §12.3）：
// - 双击图像 = 继续以此图迭代（promoteImageToReference）
// - hover 时底部淡入四颗 IconButton：迭代 / 涡轮重跑 / 下载 / 查看原图
// - isStreaming 时隐藏按钮并显示骨架 shimmer
// - 图像加载：先模糊后清晰；失败自动重试最多 3 次

import { AnimatePresence, motion } from "framer-motion";
import { useEffect, useRef, useState } from "react";
import { ArrowUpRight, Download, Maximize2, Pencil, RefreshCw, Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { useUiStore } from "@/store/useUiStore";
import { useChatStore } from "@/store/useChatStore";
import { ViewportImage } from "./ViewportImage";

interface PremiumImageCardProps {
  id: string;
  src: string;
  previewSrc?: string;
  lightboxPreviewSrc?: string;
  alt: string;
  isStreaming?: boolean;
  compact?: boolean;
  onEdit?: () => void;
  className?: string;
  style?: React.CSSProperties;
}

// 加载失败后的退避重试延迟（ms）
const RETRY_DELAYS = [100, 300, 1000] as const;

export function PremiumImageCard({
  id,
  src,
  previewSrc,
  lightboxPreviewSrc,
  alt,
  isStreaming = false,
  compact = false,
  onEdit,
  className,
  style,
}: PremiumImageCardProps) {
  const [isHovered, setIsHovered] = useState(false);
  const [isFocused, setIsFocused] = useState(false);
  const [isUpscalePending, setIsUpscalePending] = useState(false);
  const [isRerollPending, setIsRerollPending] = useState(false);
  const [loadError, setLoadError] = useState(false);
  const [retryToken, setRetryToken] = useState(0);
  const [loaded, setLoaded] = useState(false);
  const [useFallbackSrc, setUseFallbackSrc] = useState(false);
  const retryCountRef = useRef(0);
  const retryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const openLightbox = useUiStore((s) => s.openLightbox);
  // 隐藏的 <a> 用于 data: URL 下载（浏览器安全模型不允许 window.open 直接触发下载）
  const downloadAnchorRef = useRef<HTMLAnchorElement>(null);

  // React 19 推荐：prop 变化时在 render 阶段用 prev-check 同步 state，
  // 避免 useEffect+setState 触发的级联渲染。refs 清理放到下方 effect 的 cleanup 里处理。
  const [prevSrc, setPrevSrc] = useState(src);
  if (prevSrc !== src) {
    setPrevSrc(src);
    setLoaded(false);
    setLoadError(false);
    setUseFallbackSrc(false);
  }

  // src 变化或组件卸载时清理 pending retry timer 和计数（effect cleanup 能安全触及 ref）
  useEffect(() => {
    retryCountRef.current = 0;
    return () => {
      if (retryTimerRef.current) {
        clearTimeout(retryTimerRef.current);
        retryTimerRef.current = null;
      }
    };
  }, [src]);

  const handleImageError = () => {
    if (previewSrc && !useFallbackSrc) {
      setUseFallbackSrc(true);
      return;
    }
    const attempt = retryCountRef.current;
    if (attempt < RETRY_DELAYS.length) {
      const delay = RETRY_DELAYS[attempt];
      retryCountRef.current = attempt + 1;
      retryTimerRef.current = setTimeout(() => {
        setRetryToken((t) => t + 1);
      }, delay);
      return;
    }
    setLoadError(true);
  };

  // 关键决策：若父组件提供 onEdit 用它；否则假定 id 是 generated image id，
  // 调用 store 的 promoteImageToReference（DESIGN §12.3 / §22.1）。
  const handleIterate = () => {
    if (onEdit) {
      onEdit();
    } else {
      useChatStore.getState().promoteImageToReference(id);
    }
  };

  const handleUpscale = async () => {
    if (isUpscalePending) return;
    setIsUpscalePending(true);
    try {
      await useChatStore.getState().upscaleImage(id);
    } finally {
      setIsUpscalePending(false);
    }
  };

  const handleReroll = async () => {
    if (isRerollPending) return;
    setIsRerollPending(true);
    try {
      await useChatStore.getState().rerollImage(id);
    } finally {
      setIsRerollPending(false);
    }
  };

  // data: URL 用隐藏 <a download>；http(s) URL 用新标签页
  const handleDownload = () => {
    if (src.startsWith("data:")) {
      const a = downloadAnchorRef.current;
      if (a) {
        a.href = src;
        const mimeMatch = src.match(/^data:([^;]+);/);
        const ext = mimeMatch ? mimeMatch[1].split("/")[1] || "png" : "png";
        a.download = `lumen-${id}.${ext}`;
        a.click();
      }
    } else {
      window.open(src);
    }
  };

  const handleOpenLightbox = () => {
    if (isStreaming) return;
    openLightbox(id, src, alt, lightboxPreviewSrc ?? previewSrc);
  };

  // 手动重试（从错误面板触发）
  const handleManualRetry = () => {
    retryCountRef.current = 0;
    setLoadError(false);
    setLoaded(false);
    setRetryToken((t) => t + 1);
  };

  // 触摸设备：点按切换操作层可见性
  const isTouchDevice = typeof window !== "undefined" && "ontouchstart" in window;
  const controlsOpen = isHovered || isFocused;
  // 触控设备：首次尚未打开过操作层时显示「轻点查看操作」提示
  const [touchHintSeen, setTouchHintSeen] = useState(false);
  const showTouchHint =
    isTouchDevice && !isStreaming && !controlsOpen && !touchHintSeen && loaded;

  return (
    <motion.div
      style={style}
      role="group"
      tabIndex={isStreaming ? -1 : 0}
      aria-label={`${alt}。按 Enter 查看大图，双击或使用按钮继续迭代。`}
      className={cn(
        "relative overflow-hidden rounded-2xl cursor-pointer group select-none",
        "bg-black/20 shadow-lumen-card ring-1 ring-white/10",
        // 兜底 aspect-ratio：父组件若已显式设定 aspect/高度，会覆盖本默认值，避免 CLS
        "aspect-[4/3]",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-lumen-amber)]/70",
        className,
      )}
      onHoverStart={() => !isTouchDevice && setIsHovered(true)}
      onHoverEnd={() => !isTouchDevice && setIsHovered(false)}
      onFocus={() => {
        setIsFocused(true);
        if (isTouchDevice) setTouchHintSeen(true);
      }}
      onBlur={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) {
          setIsFocused(false);
        }
      }}
      onKeyDown={(e) => {
        if (e.target !== e.currentTarget) return;
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          handleOpenLightbox();
        }
      }}
      whileHover={isTouchDevice ? undefined : { y: -2 }}
      transition={{ type: "spring", stiffness: 320, damping: 22 }}
      onClick={(e) => {
        if (isTouchDevice && !isStreaming) {
          // 触摸设备：第一次点击显示操作层，第二次打开 lightbox
          if (!isHovered) {
            e.stopPropagation();
            setIsHovered(true);
            return;
          }
        }
        handleOpenLightbox();
      }}
      // DESIGN §12.3：双击图像 = 继续以此图迭代
      onDoubleClick={(e) => {
        if (isStreaming) return;
        e.stopPropagation();
        handleIterate();
      }}
    >
      {/* 骨架 shimmer（streaming 态 或 初次加载） */}
      {(isStreaming || (!loaded && !loadError)) && (
        <div
          aria-hidden
          className="absolute inset-0 overflow-hidden"
        >
          <div className="absolute inset-0 bg-[linear-gradient(110deg,rgba(255,255,255,0.03)_30%,rgba(255,255,255,0.09)_50%,rgba(255,255,255,0.03)_70%)] bg-[length:200%_100%] animate-lumen-shimmer" />
        </div>
      )}

      {loadError ? (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1, x: [0, -4, 4, -3, 3, 0] }}
          transition={{ duration: 0.5 }}
          className={cn(
            "w-full h-full flex flex-col items-center justify-center gap-2",
            "bg-neutral-900/40 border border-red-400/25 rounded-2xl",
          )}
        >
          <span className="text-xs text-neutral-300">图片加载失败</span>
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              handleManualRetry();
            }}
            className={cn(
              "text-[11px] text-neutral-200 underline decoration-dotted",
              "hover:text-white active:scale-[0.97] transition-all",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-lumen-amber)]/60 rounded-sm",
            )}
          >
            重试
          </button>
        </motion.div>
      ) : (
        <motion.div
          key={retryToken}
          layoutId={`image-${id}`}
          className="w-full h-full object-contain block"
          initial={{ filter: "blur(24px)", opacity: 0, scale: 1.04 }}
          animate={{
            filter: isStreaming
              ? "blur(12px)"
              : loaded
                ? "blur(0px)"
                : "blur(16px)",
            opacity: loaded || isStreaming ? 1 : 0.35,
            scale: loaded || isStreaming ? 1 : 1.02,
          }}
          transition={{ duration: 0.45, ease: [0.16, 1, 0.3, 1] }}
        >
          <ViewportImage
            src={useFallbackSrc ? src : (previewSrc ?? src)}
            alt={alt}
            onError={handleImageError}
            onLoad={() => setLoaded(true)}
            className="h-full w-full object-contain"
            draggable={false}
            unloadWhenHidden={!isStreaming}
          />
        </motion.div>
      )}

      {/* 隐藏下载触发器 */}
      <a ref={downloadAnchorRef} className="hidden" aria-hidden="true" />

      {/* 触控设备首次提示：底部轻淡入的操作提示 */}
      <AnimatePresence>
        {showTouchHint && (
          <motion.div
            key="touch-hint"
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 6 }}
            transition={{ duration: 0.24, delay: 0.4 }}
            className={cn(
              "pointer-events-none absolute bottom-2 left-1/2 -translate-x-1/2",
              "px-2.5 py-1 rounded-full text-[11px]",
              "bg-black/55 border border-white/10 text-white/80 backdrop-blur-md",
            )}
            aria-hidden
          >
            轻点查看操作
          </motion.div>
        )}
      </AnimatePresence>

      {/* hover 操作层：底部渐变 + IconButton 行 */}
      <motion.div
        className={cn(
          "absolute inset-x-0 bottom-0 flex justify-end gap-2 bg-gradient-to-t from-black/75 via-black/30 to-transparent",
          compact ? "p-2 pt-14" : "p-3 pt-20",
        )}
        initial={false}
        animate={{
          opacity: controlsOpen && !isStreaming ? 1 : 0,
        }}
        transition={{ duration: 0.18, ease: [0.16, 1, 0.3, 1] }}
      >
        <AnimatePresence>
          {controlsOpen && !isStreaming && [
              <HoverIconButton
                key="iterate"
                label="继续以此图迭代"
                delay={0}
                onClick={(e) => {
                  e.stopPropagation();
                  handleIterate();
                }}
              >
                <Pencil className="w-4 h-4" />
              </HoverIconButton>,
              <HoverIconButton
                key="upscale"
                label="放大到中等质量"
                delay={0.03}
                onClick={(e) => {
                  e.stopPropagation();
                  void handleUpscale();
                }}
                disabled={isUpscalePending}
              >
                {isUpscalePending ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  <ArrowUpRight className="w-4 h-4" />
                )}
              </HoverIconButton>,
              <HoverIconButton
                key="reroll"
                label="重新生成"
                delay={0.06}
                onClick={(e) => {
                  e.stopPropagation();
                  void handleReroll();
                }}
                disabled={isRerollPending}
              >
                {isRerollPending ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  <RefreshCw className="w-4 h-4" />
                )}
              </HoverIconButton>,
              <HoverIconButton
                key="download"
                label="下载"
                delay={0.09}
                onClick={(e) => {
                  e.stopPropagation();
                  handleDownload();
                }}
              >
                <Download className="w-4 h-4" />
              </HoverIconButton>,
              <HoverIconButton
                key="lightbox"
                label="查看大图"
                delay={0.12}
                onClick={(e) => {
                  e.stopPropagation();
                  handleOpenLightbox();
                }}
              >
                <Maximize2 className="w-4 h-4" />
              </HoverIconButton>,
            ]}
        </AnimatePresence>
      </motion.div>
    </motion.div>
  );
}

function HoverIconButton({
  label,
  onClick,
  disabled,
  delay,
  children,
}: {
  label: string;
  onClick: (e: React.MouseEvent) => void;
  disabled?: boolean;
  delay: number;
  children: React.ReactNode;
}) {
  return (
    <motion.button
      type="button"
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: 8 }}
      transition={{ duration: 0.18, ease: [0.16, 1, 0.3, 1], delay }}
      onClick={onClick}
      disabled={disabled}
      aria-label={label}
      title={label}
      whileHover={{ scale: 1.05 }}
      whileTap={{ scale: 0.92 }}
      className={cn(
        "pointer-events-auto inline-flex items-center justify-center w-11 h-11 md:w-10 md:h-10 rounded-full",
        "bg-black/60 backdrop-blur-md border border-white/15",
        "text-white hover:bg-black/75 hover:border-white/25",
        "transition-colors duration-150",
        "disabled:opacity-40 disabled:pointer-events-none",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-lumen-amber)]/70",
      )}
    >
      {children}
    </motion.button>
  );
}
