"use client";

import { AnimatePresence, motion } from "framer-motion";
import {
  AlertCircle,
  ArrowUpRight,
  Brush,
  Check,
  ChevronLeft,
  ChevronRight,
  Download,
  Edit2,
  ExternalLink,
  Info,
  Loader2,
  RefreshCw,
  Share2,
  X,
  ZoomIn,
  ZoomOut,
  type LucideIcon,
} from "lucide-react";
import type {
  PointerEventHandler,
  ReactNode,
  WheelEventHandler,
} from "react";

import { Tooltip } from "@/components/ui/primitives/Tooltip";
import { cn } from "@/lib/utils";

import { LightboxDetailsContent } from "./LightboxDetailsContent";
import {
  MAX_ZOOM,
  MIN_ZOOM,
  formatZoom,
  type DesktopGalleryItem,
  type DownloadStatus,
  type PanOffset,
  type ShareStatus,
  type ViewMode,
} from "./desktopLightboxModel";
import type { LightboxItem } from "./types";

type ThumbnailItem = {
  entry: DesktopGalleryItem;
  index: number;
};

type InjectedAction = {
  label: string;
  pending: boolean;
  onClick: () => void;
};

export type DesktopLightboxViewProps = {
  open: boolean;
  imageId: string | null | undefined;
  imageSrc: string | null | undefined;
  imageAlt: string | null | undefined;
  displaySrc: string | null | undefined;
  dialogTitleId: string;
  containerElementId: string;
  downloadAnchorElementId: string;
  imageWrapElementId: string;
  imageElementId: string;
  closeButtonElementId: string;
  galleryLength: number;
  currentIndex: number;
  hasPrevious: boolean;
  hasNext: boolean;
  thumbnails: ThumbnailItem[];
  posterSrc: string | null;
  sourceLabel: string;
  currentItem: LightboxItem | null;
  activeLoadError: boolean;
  activeViewMode: ViewMode;
  activeViewModeLabel: string;
  activeZoom: number;
  activePanOffset: PanOffset;
  isPanning: boolean;
  mainImageLoaded: boolean;
  detailsOpen: boolean;
  imageActionsAvailable: boolean;
  downloadStatus: DownloadStatus;
  downloadTitle: string;
  downloadText: string;
  shareStatus: ShareStatus;
  shareTitle: string;
  shareText: string;
  edgeHint: "first" | "last" | null;
  isSwitchingImage: boolean;
  injectedAction: InjectedAction | null;
  onWheel: WheelEventHandler<HTMLDivElement>;
  onBackdropMouseDown: React.MouseEventHandler<HTMLDivElement>;
  onBackdropMouseUp: React.MouseEventHandler<HTMLDivElement>;
  onClose: () => void;
  onZoomOut: () => void;
  onZoomIn: () => void;
  onResetView: () => void;
  onToggleDetails: () => void;
  onHideDetails: () => void;
  onIterate: () => void;
  onInpaint: () => void;
  onUpscale: () => void;
  onReroll: () => void;
  onDownload: () => void;
  onShare: () => void;
  onOpenOriginal: () => void;
  onPrevious: () => void;
  onNext: () => void;
  onImageLoad: () => void;
  onImageError: () => void;
  onImagePointerDown: PointerEventHandler<HTMLImageElement>;
  onImagePointerMove: PointerEventHandler<HTMLImageElement>;
  onImagePointerUp: PointerEventHandler<HTMLImageElement>;
  onImagePointerCancel: PointerEventHandler<HTMLImageElement>;
  onSelectThumbnail: (
    entry: DesktopGalleryItem,
    index: number,
  ) => void;
};

function DownloadStatusIcon({
  status,
  className = "h-4 w-4",
}: {
  status: DownloadStatus;
  className?: string;
}) {
  if (status === "downloading") {
    return <Loader2 className={cn(className, "animate-spin")} aria-hidden />;
  }
  if (status === "success") {
    return <Check className={className} aria-hidden />;
  }
  if (status === "error") {
    return <AlertCircle className={className} aria-hidden />;
  }
  return <Download className={className} aria-hidden />;
}

function ShareStatusIcon({
  status,
}: {
  status: ShareStatus;
}) {
  if (status === "creating") {
    return <Loader2 className="h-4 w-4 animate-spin" aria-hidden />;
  }
  if (status === "success") {
    return <Check className="h-4 w-4" aria-hidden />;
  }
  if (status === "error") {
    return <AlertCircle className="h-4 w-4" aria-hidden />;
  }
  return <Share2 className="h-4 w-4" aria-hidden />;
}

function DesktopTopBar(props: DesktopLightboxViewProps) {
  return (
    <motion.div
      initial={{ y: -12, opacity: 0 }}
      animate={{ y: 0, opacity: 1 }}
      exit={{ y: -12, opacity: 0 }}
      transition={{
        duration: 0.25,
        ease: [0.16, 1, 0.3, 1],
        delay: 0.05,
      }}
      style={{
        paddingTop: "max(1.25rem, env(safe-area-inset-top))",
        paddingLeft: "max(1.25rem, env(safe-area-inset-left))",
        paddingRight: "max(1.25rem, env(safe-area-inset-right))",
      }}
      className={cn(
        "absolute top-0 left-0 right-0 pb-3 md:pb-4",
        "grid grid-cols-[1fr_auto_1fr] items-start gap-3 pointer-events-none",
      )}
    >
      <div className="flex min-w-0 items-center gap-2 pointer-events-auto">
        <div
          className={cn(
            "flex min-h-11 items-center gap-1 rounded-full",
            "border border-white/10 bg-black/35 p-1 backdrop-blur-xl",
            "shadow-[var(--shadow-2)]",
          )}
        >
          <ToolIconButton
            onClick={props.onZoomOut}
            title="缩小（-）"
            icon={ZoomOut}
            disabled={props.activeZoom <= MIN_ZOOM}
          />
          <button
            type="button"
            onClick={props.onResetView}
            title="重置为适应窗口（0）"
            aria-label="重置为适应窗口（0）"
            className={cn(
              "h-9 min-w-16 rounded-full px-3 text-xs font-mono tabular-nums max-sm:min-h-11",
              "text-white/82 hover:bg-white/10 hover:text-white",
              "transition-colors duration-150 cursor-pointer",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-lumen-amber)]/70",
            )}
          >
            {formatZoom(props.activeZoom)}
          </button>
          <ToolIconButton
            onClick={props.onZoomIn}
            title="放大（+）"
            icon={ZoomIn}
            disabled={props.activeZoom >= MAX_ZOOM}
          />
        </div>
      </div>

      {props.galleryLength > 0 && props.currentIndex >= 0 ? (
        <div
          className={cn(
            "pointer-events-auto place-self-start px-3.5 py-2 rounded-full",
            "bg-black/35 border border-white/10 text-white/82",
            "text-xs font-mono tabular-nums backdrop-blur-xl",
            "shadow-[var(--shadow-2)]",
          )}
        >
          {props.currentIndex + 1} / {props.galleryLength}
        </div>
      ) : null}

      <div className="flex flex-wrap justify-end gap-2 pointer-events-auto">
        <div
          className={cn(
            "flex min-h-11 items-center gap-1 rounded-full",
            "border border-white/10 bg-black/35 p-1 backdrop-blur-xl",
            "shadow-[var(--shadow-2)]",
          )}
        >
          <TopButton
            onClick={props.onIterate}
            title="迭代（E）"
            icon={<Edit2 className="h-4 w-4" aria-hidden />}
            disabled={!props.imageActionsAvailable}
          >
            迭代
          </TopButton>
          <TopButton
            onClick={props.onInpaint}
            title="局部修改"
            icon={<Brush className="h-4 w-4" aria-hidden />}
            disabled={!props.imageActionsAvailable}
          >
            局部
          </TopButton>
          <TopButton
            onClick={props.onUpscale}
            title="放大到4K"
            icon={<ArrowUpRight className="h-4 w-4" aria-hidden />}
            disabled={!props.imageActionsAvailable}
          >
            放大
          </TopButton>
          <TopButton
            onClick={props.onReroll}
            title="重新生成"
            icon={<RefreshCw className="h-4 w-4" aria-hidden />}
            disabled={!props.imageActionsAvailable}
          >
            重画
          </TopButton>
          <TopButton
            onClick={props.onDownload}
            title={props.downloadTitle}
            icon={<DownloadStatusIcon status={props.downloadStatus} />}
            disabled={props.downloadStatus === "downloading"}
          >
            {props.downloadText}
          </TopButton>
          <TopButton
            onClick={props.onShare}
            title={props.shareTitle}
            icon={<ShareStatusIcon status={props.shareStatus} />}
            disabled={!props.imageId || props.shareStatus === "creating"}
          >
            {props.shareText}
          </TopButton>
        </div>

        <ToolIconButton
          onClick={props.onToggleDetails}
          title="图片信息（I）"
          icon={Info}
          active={props.detailsOpen}
        />
        <button
          id={props.closeButtonElementId}
          type="button"
          onClick={props.onClose}
          aria-label="关闭（Esc）"
          title="关闭（Esc）"
          className={cn(
            "pointer-events-auto inline-flex items-center justify-center",
            "w-11 h-11 rounded-full border border-white/15",
            "bg-black/35 text-white backdrop-blur-xl hover:bg-white/15 hover:border-white/25",
            "active:scale-[0.94] transition-all duration-150 cursor-pointer",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-lumen-amber)]/70",
          )}
        >
          <X className="w-5 h-5" />
        </button>
      </div>
    </motion.div>
  );
}

function DesktopSideNavigation(props: DesktopLightboxViewProps) {
  if (props.galleryLength <= 1) return null;
  return (
    <>
      <SideChevron
        side="left"
        disabled={!props.hasPrevious || props.isSwitchingImage}
        onClick={props.onPrevious}
      />
      <SideChevron
        side="right"
        disabled={!props.hasNext || props.isSwitchingImage}
        onClick={props.onNext}
      />
    </>
  );
}

function DesktopMediaStage(props: DesktopLightboxViewProps) {
  if (props.activeLoadError) {
    return (
      <motion.div
        id={props.imageWrapElementId}
        className={mediaWrapClassName(props.detailsOpen)}
      >
        <div
          role="alert"
          className="pointer-events-auto rounded-[var(--radius-dialog)] border border-white/10 bg-black/50 backdrop-blur px-8 py-10 text-center max-w-md"
        >
          <p className="text-base text-white/90">图片加载失败</p>
          <p className="text-xs text-white/50 mt-2">
            数据可能已过期或网络异常，可关闭后重试。
          </p>
        </div>
      </motion.div>
    );
  }
  return (
    <motion.div
      id={props.imageWrapElementId}
      className={mediaWrapClassName(props.detailsOpen)}
    >
      {props.posterSrc &&
      props.posterSrc !== props.displaySrc &&
      !props.mainImageLoaded ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={props.posterSrc}
          alt=""
          aria-hidden
          draggable={false}
          className={cn(
            "pointer-events-none absolute max-h-[calc(100%-8rem)] max-w-[calc(100%-4rem)]",
            "select-none rounded-[var(--radius-control)] object-contain opacity-45 blur-md saturate-110",
          )}
        />
      ) : null}
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        id={props.imageElementId}
        key={`${props.imageId}:${props.displaySrc}`}
        src={props.displaySrc ?? undefined}
        alt={props.imageAlt || ""}
        loading="eager"
        decoding="async"
        fetchPriority="high"
        onLoad={props.onImageLoad}
        onError={props.onImageError}
        style={imageStyle(props)}
        onPointerDown={props.onImagePointerDown}
        onPointerMove={props.onImagePointerMove}
        onPointerUp={props.onImagePointerUp}
        onPointerCancel={props.onImagePointerCancel}
        className={cn(
          "rounded-[var(--radius-control)] shadow-2xl",
          imageViewModeClassName(props.activeViewMode),
          "pointer-events-auto select-none transform-gpu",
          props.edgeHint && "animate-[lb-shake_0.35s_ease-in-out]",
        )}
        draggable={false}
      />
    </motion.div>
  );
}

function mediaWrapClassName(detailsOpen: boolean): string {
  return cn(
    "relative z-10 w-full h-full px-4 sm:px-6 md:px-10 py-20",
    "flex items-center justify-center pointer-events-none",
    "transition-[padding] duration-300 ease-[var(--ease-shutter)]",
    detailsOpen && "md:pr-[23rem] lg:pr-[27rem]",
  );
}

function imageViewModeClassName(viewMode: ViewMode): string {
  if (viewMode === "fill") {
    return "h-full w-full max-w-none max-h-none object-cover";
  }
  if (viewMode === "actual") {
    return "max-w-none max-h-none object-contain";
  }
  return "max-w-full max-h-full object-contain";
}

function imageStyle(
  props: DesktopLightboxViewProps,
): React.CSSProperties {
  const transformed =
    props.isPanning ||
    props.activeZoom > 1 ||
    props.activeViewMode !== "fit";
  const pannable =
    props.activeZoom > 1 || props.activeViewMode !== "fit";
  return {
    transform: `translate3d(${props.activePanOffset.x}px, ${props.activePanOffset.y}px, 0) scale(${props.activeZoom})`,
    willChange: transformed ? "transform" : "auto",
    backfaceVisibility: "hidden",
    cursor: pannable ? (props.isPanning ? "grabbing" : "grab") : "zoom-in",
    transition: props.isPanning ? "none" : "transform 0.2s ease-out",
    touchAction: "none",
    overscrollBehavior: "contain",
  };
}

function DesktopDetailsPanel(props: DesktopLightboxViewProps) {
  return (
    <AnimatePresence>
      {props.detailsOpen ? (
        <motion.aside
          key="desktop-lightbox-details"
          initial={{ opacity: 0, x: 24 }}
          animate={{ opacity: 1, x: 0 }}
          exit={{ opacity: 0, x: 24 }}
          transition={{
            duration: 0.22,
            ease: [0.16, 1, 0.3, 1],
          }}
          style={{
            top: "max(5.5rem, calc(env(safe-area-inset-top) + 5rem))",
            right: "max(1.25rem, env(safe-area-inset-right))",
            bottom:
              "max(6.5rem, calc(env(safe-area-inset-bottom) + 5.5rem))",
          }}
          className={cn(
            "absolute z-30 flex w-[min(22rem,calc(100vw-2.5rem))] flex-col overflow-hidden",
            "rounded-[var(--radius-dialog)] border border-white/12 bg-black/48 text-white",
            "backdrop-blur-2xl shadow-[var(--shadow-3)]",
            "pointer-events-auto",
          )}
        >
          <div className="flex items-center justify-between border-b border-white/10 px-4 py-3">
            <div>
              <p className="text-sm font-medium text-white/90">
                图片信息
              </p>
              <p className="mt-0.5 text-[11px] text-white/45">
                {props.sourceLabel} · {props.activeViewModeLabel} ·{" "}
                {formatZoom(props.activeZoom)}
              </p>
            </div>
            <ToolIconButton
              onClick={props.onHideDetails}
              title="收起信息"
              icon={X}
              className="h-9 w-9"
            />
          </div>

          <div className="min-h-0 flex-1 space-y-5 overflow-y-auto px-4 py-4 scrollbar-thin">
            {props.currentItem ? (
              <LightboxDetailsContent
                item={props.currentItem}
                tone="media"
              />
            ) : null}

            <section className="space-y-2">
              <h3 className="text-[11px] font-mono uppercase tracking-wide text-white/45">
                快捷键
              </h3>
              <div className="grid grid-cols-2 gap-2 text-[11px] text-white/58">
                <Shortcut label="上一张" value="K / ←" />
                <Shortcut label="下一张" value="J / →" />
                <Shortcut label="缩放" value="+ / -" />
                <Shortcut label="适应" value="0" />
                <Shortcut label="模式" value="1 / 2 / 3" />
                <Shortcut label="下载" value="D" />
                <Shortcut label="信息" value="I" />
              </div>
            </section>
          </div>

          <div className="grid grid-cols-2 gap-2 border-t border-white/10 p-3">
            <button
              type="button"
              onClick={props.onOpenOriginal}
              className={cn(
                "inline-flex h-10 items-center justify-center gap-2 rounded-[var(--radius-panel)] max-sm:min-h-11",
                "border border-white/10 bg-white/5 text-sm text-white/80",
                "hover:border-white/25 hover:bg-white/10 hover:text-white",
                "transition-colors duration-150 cursor-pointer",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-lumen-amber)]/70",
              )}
            >
              <ExternalLink className="h-4 w-4" />
              打开
            </button>
            <button
              type="button"
              onClick={props.onDownload}
              disabled={props.downloadStatus === "downloading"}
              className={cn(
                "inline-flex h-10 items-center justify-center gap-2 rounded-[var(--radius-panel)] max-sm:min-h-11",
                "border border-[var(--color-lumen-amber)]/35 bg-[var(--color-lumen-amber)]/14",
                "text-sm text-[var(--amber-100)]",
                "hover:bg-[var(--color-lumen-amber)]/22",
                "disabled:cursor-wait disabled:opacity-70",
                "transition-colors duration-150 cursor-pointer",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-lumen-amber)]/70",
              )}
            >
              <DownloadStatusIcon status={props.downloadStatus} />
              {props.downloadText}
            </button>
          </div>
        </motion.aside>
      ) : null}
    </AnimatePresence>
  );
}

function DesktopFooter(props: DesktopLightboxViewProps) {
  return (
    <motion.div
      initial={{ y: 12, opacity: 0 }}
      animate={{ y: 0, opacity: 1 }}
      exit={{ y: 12, opacity: 0 }}
      transition={{
        duration: 0.25,
        ease: [0.16, 1, 0.3, 1],
        delay: 0.05,
      }}
      style={{
        paddingBottom: "max(1.25rem, env(safe-area-inset-bottom))",
        paddingLeft: "max(1.25rem, env(safe-area-inset-left))",
        paddingRight: "max(1.25rem, env(safe-area-inset-right))",
      }}
      className={cn(
        "absolute bottom-0 left-0 right-0 pt-3 md:pt-4",
        "flex flex-col items-center gap-3 pointer-events-none",
      )}
    >
      <DesktopFooterStatus {...props} />
      {props.injectedAction ? (
        <button
          type="button"
          disabled={props.injectedAction.pending}
          onClick={props.injectedAction.onClick}
          className={cn(
            "pointer-events-auto inline-flex items-center gap-2 rounded-full px-5 py-2.5",
            "bg-[var(--color-lumen-amber)] text-black text-sm font-medium",
            "shadow-[var(--shadow-amber)]",
            "hover:bg-[var(--amber-200)] active:scale-[0.97] transition-all duration-150",
            "disabled:cursor-not-allowed disabled:opacity-70",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-lumen-amber)]/70",
          )}
        >
          {props.injectedAction.pending ? (
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
          ) : (
            <Check className="h-4 w-4" aria-hidden />
          )}
          {props.injectedAction.label}
        </button>
      ) : null}
      <DesktopThumbnailStrip {...props} />
    </motion.div>
  );
}

function DesktopFooterStatus(props: DesktopLightboxViewProps) {
  return (
    <AnimatePresence>
      {props.isSwitchingImage ? (
        <motion.div
          key="switching-image"
          initial={{ opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: 6 }}
          className={cn(
            "pointer-events-auto px-3 py-1 rounded-full text-[11px]",
            "bg-black/60 border border-white/15 text-white/80 backdrop-blur-md",
          )}
          role="status"
        >
          正在载入下一张
        </motion.div>
      ) : null}
      {props.edgeHint ? (
        <motion.div
          initial={{ opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: 6 }}
          className={cn(
            "pointer-events-auto px-3 py-1 rounded-full text-[11px]",
            "bg-black/60 border border-white/15 text-white/80 backdrop-blur-md",
          )}
          role="status"
        >
          {props.edgeHint === "first"
            ? "已是第一张"
            : "已是最后一张"}
        </motion.div>
      ) : null}
    </AnimatePresence>
  );
}

function DesktopThumbnailStrip(props: DesktopLightboxViewProps) {
  if (props.galleryLength <= 1) return null;
  return (
    <div
      className={cn(
        "pointer-events-auto flex items-center gap-1.5 px-2 py-1.5",
        "max-w-[min(720px,90vw)] overflow-x-auto",
        "bg-black/50 border border-white/10 rounded-[var(--radius-panel)] backdrop-blur-md",
      )}
    >
      {props.thumbnails.map(({ entry, index }) => (
        <ThumbnailButton
          key={entry.image.id}
          entry={entry}
          index={index}
          active={entry.image.id === props.imageId}
          onSelect={props.onSelectThumbnail}
        />
      ))}
    </div>
  );
}

function ThumbnailButton({
  entry,
  index,
  active,
  onSelect,
}: {
  entry: DesktopGalleryItem;
  index: number;
  active: boolean;
  onSelect: (entry: DesktopGalleryItem, index: number) => void;
}) {
  const image = entry.image;
  return (
    <button
      type="button"
      onClick={() => onSelect(entry, index)}
      aria-label={`第 ${index + 1} 张`}
      aria-current={active}
      className={cn(
        "relative shrink-0 w-12 h-12 rounded-[var(--radius-card)] overflow-hidden",
        "border transition-all duration-150",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-lumen-amber)]/70",
        active
          ? "border-[var(--color-lumen-amber)] ring-1 ring-[var(--color-lumen-amber)]/60"
          : "border-white/10 hover:border-white/40 opacity-70 hover:opacity-100",
      )}
    >
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src={image.thumb_url ?? image.preview_url ?? image.data_url}
        alt=""
        loading="lazy"
        decoding="async"
        fetchPriority="low"
        className="w-full h-full object-cover"
        draggable={false}
      />
    </button>
  );
}

function DesktopLightboxDialog(props: DesktopLightboxViewProps) {
  return (
    <motion.div
      key="desktop-lightbox"
      id={props.containerElementId}
      tabIndex={-1}
      role="dialog"
      aria-modal="true"
      aria-labelledby={props.dialogTitleId}
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.12, ease: "linear" }}
      className="fixed inset-0 h-[100dvh] w-screen flex items-center justify-center overflow-hidden overscroll-contain outline-none z-[var(--z-lightbox)]"
      style={{ touchAction: "none", overscrollBehavior: "contain" }}
      onWheel={props.onWheel}
      onMouseDown={props.onBackdropMouseDown}
      onMouseUp={props.onBackdropMouseUp}
    >
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        className="absolute inset-0 bg-black/90 pointer-events-none"
        transition={{
          duration: 0.28,
          ease: [0.16, 1, 0.3, 1],
        }}
        aria-hidden
      />
      <span id={props.dialogTitleId} className="sr-only">
        {props.imageAlt
          ? `图片预览：${props.imageAlt}`
          : "图片预览"}
      </span>
      <a
        id={props.downloadAnchorElementId}
        className="hidden"
        aria-hidden="true"
      />
      <DesktopTopBar {...props} />
      <DesktopSideNavigation {...props} />
      <DesktopMediaStage {...props} />
      <DesktopDetailsPanel {...props} />
      <DesktopFooter {...props} />
      <style>{`
        @keyframes lb-shake {
          0%, 100% { transform: translateX(0); }
          25% { transform: translateX(-6px); }
          50% { transform: translateX(6px); }
          75% { transform: translateX(-3px); }
        }
      `}</style>
    </motion.div>
  );
}

export function DesktopLightboxView(
  props: DesktopLightboxViewProps,
) {
  const visible = props.open && props.imageSrc && props.displaySrc;
  return (
    <AnimatePresence>
      {visible ? <DesktopLightboxDialog {...props} /> : null}
    </AnimatePresence>
  );
}

function ToolIconButton({
  onClick,
  title,
  icon: Icon,
  disabled = false,
  active = false,
  className,
}: {
  onClick: () => void;
  title: string;
  icon: LucideIcon;
  disabled?: boolean;
  active?: boolean;
  className?: string;
}) {
  const button = (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-label={title}
      title={title}
      className={cn(
        "inline-flex h-9 w-9 items-center justify-center rounded-full max-sm:min-h-11 max-sm:min-w-11",
        "border border-transparent text-white/68",
        "hover:border-white/15 hover:bg-white/10 hover:text-white",
        "disabled:cursor-not-allowed disabled:opacity-35 disabled:hover:border-transparent disabled:hover:bg-transparent",
        "active:scale-[0.94] transition-all duration-150 cursor-pointer",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-lumen-amber)]/70",
        active &&
          "border-[var(--color-lumen-amber)]/45 bg-[var(--color-lumen-amber)]/16 text-[var(--amber-100)]",
        className,
      )}
    >
      <Icon className="h-4 w-4" aria-hidden />
    </button>
  );
  return (
    <Tooltip content={title} side="bottom" enabled={!disabled}>
      {button}
    </Tooltip>
  );
}

function TopButton({
  onClick,
  title,
  icon,
  disabled = false,
  children,
}: {
  onClick: () => void;
  title: string;
  icon: ReactNode;
  disabled?: boolean;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      aria-label={title}
      className={cn(
        "inline-flex h-9 items-center gap-1.5 rounded-full px-3 text-sm max-sm:min-h-11",
        "border border-transparent text-white/82",
        "hover:border-white/15 hover:bg-white/10 hover:text-white",
        "disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:border-transparent disabled:hover:bg-transparent",
        "active:scale-[0.97] transition-all duration-150 cursor-pointer",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-lumen-amber)]/70",
      )}
    >
      {icon}
      <span>{children}</span>
    </button>
  );
}

function Shortcut({
  label,
  value,
}: {
  label: string;
  value: string;
}) {
  return (
    <div className="flex items-center justify-between gap-2 rounded-[var(--radius-card)] border border-white/8 bg-white/[0.035] px-2 py-1.5">
      <span>{label}</span>
      <span className="font-mono text-white/82">{value}</span>
    </div>
  );
}

function SideChevron({
  side,
  disabled,
  onClick,
}: {
  side: "left" | "right";
  disabled: boolean;
  onClick: () => void;
}) {
  const Icon = side === "left" ? ChevronLeft : ChevronRight;
  const label = side === "left" ? "上一张（K）" : "下一张（J）";
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-label={label}
      title={label}
      className={cn(
        "absolute top-1/2 -translate-y-1/2 z-20",
        side === "left"
          ? "left-2 sm:left-3 md:left-6"
          : "right-2 sm:right-3 md:right-6",
        "inline-flex items-center justify-center w-11 h-11 rounded-full",
        "bg-white/6 border border-white/10 text-white/70 backdrop-blur-md",
        "hover:bg-white/15 hover:text-white hover:border-white/25",
        "active:scale-[0.94] transition-all duration-150",
        "disabled:opacity-30 disabled:cursor-not-allowed",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-lumen-amber)]/70",
      )}
    >
      <Icon className="w-5 h-5" />
    </button>
  );
}
