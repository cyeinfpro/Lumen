"use client";

import { AnimatePresence, motion } from "framer-motion";
import {
  ArrowUpRight,
  Brush,
  Check,
  ChevronLeft,
  ChevronRight,
  Edit2,
  ExternalLink,
  Info,
  RefreshCw,
  X,
  ZoomIn,
  ZoomOut,
  type LucideIcon,
} from "lucide-react";

import { Button } from "@/components/ui/primitives/Button";
import { ErrorState } from "@/components/ui/primitives/ErrorState";
import { Kbd } from "@/components/ui/primitives/Kbd";
import { MediaControlButton } from "@/components/ui/primitives/MediaControlButton";
import { Tooltip } from "@/components/ui/primitives/Tooltip";
import { cn } from "@/lib/utils";

import { LightboxActionMenu } from "./LightboxActionMenu";
import { LightboxDetailsContent } from "./LightboxDetailsContent";
import {
  MAX_ZOOM,
  MIN_ZOOM,
  formatZoom,
  type DesktopGalleryItem,
  type ViewMode,
} from "./desktopLightboxModel";

import type { DesktopLightboxViewProps } from "./DesktopLightboxViewTypes";
export type { DesktopLightboxViewProps } from "./DesktopLightboxViewTypes";

import {
  DownloadStatusIcon,
  ShareStatusIcon,
} from "./DesktopLightboxStatusIcons";

function DesktopTopBar(props: DesktopLightboxViewProps) {
  const overflowActions = [
    {
      label: "迭代",
      icon: <Edit2 className="h-4 w-4" />,
      onSelect: props.onIterate,
      disabled: !props.imageActionsAvailable,
    },
    {
      label: "局部修改",
      icon: <Brush className="h-4 w-4" />,
      onSelect: props.onInpaint,
      disabled: !props.imageActionsAvailable,
    },
    {
      label: "放大到 4K",
      icon: <ArrowUpRight className="h-4 w-4" />,
      onSelect: props.onUpscale,
      disabled: !props.imageActionsAvailable,
    },
    {
      label: "重新生成",
      icon: <RefreshCw className="h-4 w-4" />,
      onSelect: props.onReroll,
      disabled: !props.imageActionsAvailable,
    },
    {
      label: props.shareText,
      icon: <ShareStatusIcon status={props.shareStatus} />,
      onSelect: props.onShare,
      disabled: !props.imageId || props.shareStatus === "creating",
    },
  ];

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
      <div className="pointer-events-auto flex min-w-0 items-center gap-2">
        <ToolIconButton
          onClick={props.onZoomOut}
          title="缩小（-）"
          icon={ZoomOut}
          disabled={props.activeZoom <= MIN_ZOOM}
        />
        <MediaControlButton
          size="lg"
          onClick={props.onResetView}
          title="重置为适应窗口（0）"
          aria-label="重置为适应窗口（0）"
          className="type-caption w-auto min-w-16 px-3 font-mono tabular-nums shadow-[var(--shadow-2)]"
        >
          {formatZoom(props.activeZoom)}
        </MediaControlButton>
        <ToolIconButton
          onClick={props.onZoomIn}
          title="放大（+）"
          icon={ZoomIn}
          disabled={props.activeZoom >= MAX_ZOOM}
        />
      </div>

      {props.galleryLength > 0 && props.currentIndex >= 0 ? (
        <div
          className={cn(
            "type-caption pointer-events-auto place-self-start rounded-full px-3.5 py-2 font-mono tabular-nums",
            "bg-[var(--media-control-bg)] text-[var(--media-control-fg)] backdrop-blur-xl",
            "shadow-[var(--shadow-2)]",
          )}
        >
          {props.currentIndex + 1} / {props.galleryLength}
        </div>
      ) : null}

      <div className="pointer-events-auto flex flex-wrap justify-end gap-2">
        <MediaControlButton
          size="lg"
          onClick={props.onDownload}
          title={props.downloadTitle}
          aria-label={props.downloadTitle}
          disabled={props.downloadStatus === "downloading"}
          className="type-body-sm w-auto gap-1.5 px-3 shadow-[var(--shadow-2)]"
        >
          <DownloadStatusIcon status={props.downloadStatus} />
          <span>{props.downloadText}</span>
        </MediaControlButton>
        <LightboxActionMenu actions={overflowActions} />

        <ToolIconButton
          onClick={props.onToggleDetails}
          title="图片信息（I）"
          icon={Info}
          active={props.detailsOpen}
        />
        <MediaControlButton
          size="lg"
          id={props.closeButtonElementId}
          onClick={props.onClose}
          aria-label="关闭（Esc）"
          title="关闭（Esc）"
          className="shadow-[var(--shadow-2)]"
        >
          <X className="h-5 w-5" />
        </MediaControlButton>
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
        <ErrorState
          title="图片加载失败"
          description="网络异常或图片已过期。"
          onRetry={props.onRetryImage}
          className="pointer-events-auto max-w-md"
        />
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
          "rounded-[var(--radius-control)] shadow-[var(--shadow-2)]",
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
    "relative z-[var(--z-header)] w-full h-full px-4 sm:px-6 md:px-10 py-20",
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

function imageStyle(props: DesktopLightboxViewProps): React.CSSProperties {
  const transformed =
    props.isPanning || props.activeZoom > 1 || props.activeViewMode !== "fit";
  const pannable = props.activeZoom > 1 || props.activeViewMode !== "fit";
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
            bottom: "max(6.5rem, calc(env(safe-area-inset-bottom) + 5.5rem))",
          }}
          className={cn(
            "absolute z-[var(--z-tray)] flex w-[min(22rem,calc(100vw-2.5rem))] flex-col overflow-hidden",
            "rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--media-control-bg)] text-[var(--media-control-fg)]",
            "backdrop-blur-xl shadow-[var(--shadow-2)]",
            "pointer-events-auto",
          )}
        >
          <div className="flex items-center justify-between border-b border-[var(--border)] px-4 py-3">
            <div>
              <p className="type-body-sm font-medium">图片信息</p>
              <p className="type-caption mt-0.5 opacity-60">
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
              <LightboxDetailsContent item={props.currentItem} tone="media" />
            ) : null}

            <section className="space-y-2">
              <h3 className="type-caption opacity-60">快捷键</h3>
              <div className="type-caption grid grid-cols-2 gap-2 opacity-75">
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

          <div className="grid grid-cols-2 gap-2 border-t border-[var(--border)] p-3">
            <MediaControlButton
              size="md"
              onClick={props.onOpenOriginal}
              aria-label="打开原图"
              className="type-body-sm w-full gap-2 rounded-[var(--radius-control)]"
            >
              <ExternalLink className="h-4 w-4" />
              打开
            </MediaControlButton>
            <MediaControlButton
              size="md"
              onClick={props.onDownload}
              aria-label={props.downloadTitle}
              disabled={props.downloadStatus === "downloading"}
              className="type-body-sm w-full gap-2 rounded-[var(--radius-control)]"
            >
              <DownloadStatusIcon status={props.downloadStatus} />
              {props.downloadText}
            </MediaControlButton>
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
        <Button
          variant="primary"
          size="md"
          loading={props.injectedAction.pending}
          onClick={props.injectedAction.onClick}
          leftIcon={<Check className="h-4 w-4" aria-hidden />}
          className="pointer-events-auto rounded-full shadow-[var(--shadow-amber)]"
        >
          {props.injectedAction.label}
        </Button>
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
            "type-caption pointer-events-auto rounded-full px-3 py-1",
            "bg-[var(--media-control-bg)] text-[var(--media-control-fg)] backdrop-blur-md",
          )}
          role="status"
        >
          下一张加载中
        </motion.div>
      ) : null}
      {props.edgeHint ? (
        <motion.div
          initial={{ opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: 6 }}
          className={cn(
            "type-caption pointer-events-auto rounded-full px-3 py-1",
            "bg-[var(--media-control-bg)] text-[var(--media-control-fg)] backdrop-blur-md",
          )}
          role="status"
        >
          {props.edgeHint === "first" ? "已是第一张" : "已是最后一张"}
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
        "rounded-[var(--radius-panel)] bg-[var(--media-control-bg)] text-[var(--media-control-fg)] backdrop-blur-md",
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
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]/70",
        active
          ? "border-accent-border ring-1 ring-[var(--accent)]"
          : "border-[var(--border)] opacity-70 hover:border-[var(--border-strong)] hover:opacity-100",
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
        className="pointer-events-none absolute inset-0 bg-[var(--surface-media)]"
        transition={{
          duration: 0.28,
          ease: [0.16, 1, 0.3, 1],
        }}
        aria-hidden
      />
      <span id={props.dialogTitleId} className="sr-only">
        {props.imageAlt ? `图片预览：${props.imageAlt}` : "图片预览"}
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

export function DesktopLightboxView(props: DesktopLightboxViewProps) {
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
    <MediaControlButton
      size="md"
      onClick={onClick}
      disabled={disabled}
      aria-label={title}
      title={title}
      className={cn(
        "shadow-[var(--shadow-2)]",
        active &&
          "bg-accent-soft text-accent ring-1 ring-inset ring-accent-border",
        className,
      )}
    >
      <Icon className="h-4 w-4" aria-hidden />
    </MediaControlButton>
  );
  return (
    <Tooltip content={title} side="bottom" enabled={!disabled}>
      {button}
    </Tooltip>
  );
}

function Shortcut({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between gap-2 rounded-[var(--radius-card)] border border-[var(--border)] px-2 py-1.5">
      <span>{label}</span>
      <Kbd className="border-[var(--border)] bg-transparent text-[var(--media-control-fg)]">
        {value}
      </Kbd>
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
    <MediaControlButton
      size="lg"
      onClick={onClick}
      disabled={disabled}
      aria-label={label}
      title={label}
      className={cn(
        "absolute top-1/2 -translate-y-1/2 z-[var(--z-tabbar)]",
        side === "left"
          ? "left-2 sm:left-3 md:left-6"
          : "right-2 sm:right-3 md:right-6",
        "shadow-[var(--shadow-2)]",
      )}
    >
      <Icon className="h-5 w-5" />
    </MediaControlButton>
  );
}
