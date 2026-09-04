"use client";

import {
  AlertCircle,
  ArrowUpRight,
  Brush,
  Check,
  ChevronLeft,
  ChevronRight,
  Copy,
  Download,
  Info,
  Pencil,
  RefreshCw,
  RotateCcw,
  Share2,
  X,
} from "lucide-react";
import { motion, type MotionValue } from "framer-motion";
import type { Dispatch, RefObject, SetStateAction } from "react";

import { Button } from "@/components/ui/primitives/Button";
import { ErrorState } from "@/components/ui/primitives/ErrorState";
import { MediaControlButton } from "@/components/ui/primitives/MediaControlButton";
import { Spinner } from "@/components/ui/primitives/Spinner";
import { DURATION, EASE } from "@/lib/motion";
import { cn } from "@/lib/utils";
import type { LightboxAction } from "@/store/useUiStore";

import { LightboxActionMenu } from "./LightboxActionMenu";
import { LightboxParamsPanel } from "./LightboxParamsPanel";
import {
  displayUrlForItem,
  isImageDecoded,
  markImageDecoded,
  posterUrlForItem,
} from "./mobileLightboxMedia";
import type { LightboxItem } from "./types";

import type {
  ImgStatus,
  DownloadStatus,
  VisibleSlide,
  ThumbnailItem,
  MobileLightboxViewProps,
} from "./MobileLightboxViewTypes";
export type {
  ActionNotice,
  DownloadStatus,
  ImgStatus,
  ThumbnailItem,
  VisibleSlide,
} from "./MobileLightboxViewTypes";

export function MobileLightboxView({
  current,
  idx,
  total,
  isFirst,
  isLast,
  paramsOpen,
  imgStatus,
  useFallback,
  fallbackItemIds,
  chromeVisible,
  zoomLevel,
  downloadStatus,
  actionNotice,
  boundaryHint,
  lightboxAction,
  visibleSlides,
  thumbItems,
  gestureTargetRef,
  downloadAnchorRef,
  dialogRootRef,
  closeButtonRef,
  activeThumbRef,
  dialogTitleId,
  dragX,
  dragY,
  scale,
  haloOpacity,
  onClose,
  onGoto,
  onResetZoom,
  onDownload,
  onSwitchItem,
  onMarkFallback,
  setUseFallback,
  setImgStatus,
  onIterate,
  onInpaint,
  onUpscale,
  onReroll,
  onCopyPrompt,
  onShare,
  onOpenParams,
  onCloseParams,
}: MobileLightboxViewProps) {
  if (!current) return null;
  const currentUseFallback = useFallback || fallbackItemIds.has(current.id);
  const displayUrl = displayUrlForItem(current, currentUseFallback);
  const posterUrl = posterUrlForItem(current);
  const showPoster = imgStatus === "loading" && posterUrl !== displayUrl;
  const sourceLabel =
    !currentUseFallback && current.previewUrl ? "预览" : "原图";
  const isZoomed = zoomLevel > 1.02;
  const zoomPercent = `${Math.round(zoomLevel * 100)}%`;

  return (
    <div
      ref={dialogRootRef}
      tabIndex={-1}
      role="dialog"
      aria-modal="true"
      aria-labelledby={dialogTitleId}
      className="fixed inset-0 z-[var(--z-lightbox)] overflow-hidden outline-none"
    >
      <span id={dialogTitleId} className="sr-only">
        {current.prompt ? `图片预览：${current.prompt}` : "图片查看器"}
      </span>
      <motion.div
        aria-hidden
        className="absolute inset-0 bg-[var(--surface-media)]"
        style={{ opacity: haloOpacity }}
      />
      <a ref={downloadAnchorRef} className="hidden" aria-hidden="true" />
      <LightboxImageStage
        current={current}
        currentUseFallback={currentUseFallback}
        fallbackItemIds={fallbackItemIds}
        gestureTargetRef={gestureTargetRef}
        imgStatus={imgStatus}
        posterUrl={posterUrl}
        showPoster={showPoster}
        visibleSlides={visibleSlides}
        dragX={dragX}
        dragY={dragY}
        scale={scale}
        onMarkFallback={onMarkFallback}
        setUseFallback={setUseFallback}
        setImgStatus={setImgStatus}
      />
      <LightboxTopBar
        chromeVisible={chromeVisible}
        closeButtonRef={closeButtonRef}
        downloadStatus={downloadStatus}
        idx={idx}
        isZoomed={isZoomed}
        sourceLabel={sourceLabel}
        total={total}
        zoomPercent={zoomPercent}
        onClose={onClose}
        onDownload={onDownload}
      />
      <LightboxNotice actionNotice={actionNotice} boundaryHint={boundaryHint} />
      <LightboxNavigation
        chromeVisible={chromeVisible}
        isFirst={isFirst}
        isLast={isLast}
        total={total}
        onGoto={onGoto}
      />
      <LightboxZoomReset
        chromeVisible={chromeVisible}
        isZoomed={isZoomed}
        zoomPercent={zoomPercent}
        onReset={onResetZoom}
      />
      <LightboxFooter
        activeThumbRef={activeThumbRef}
        chromeVisible={chromeVisible}
        current={current}
        idx={idx}
        lightboxAction={lightboxAction}
        thumbItems={thumbItems}
        total={total}
        onCopyPrompt={onCopyPrompt}
        onInpaint={onInpaint}
        onIterate={onIterate}
        onOpenParams={onOpenParams}
        onReroll={onReroll}
        onShare={onShare}
        onSwitchItem={onSwitchItem}
        onUpscale={onUpscale}
      />
      <LightboxParamsPanel
        open={paramsOpen}
        onClose={onCloseParams}
        item={current}
        onCopyPrompt={current.prompt ? onCopyPrompt : undefined}
      />
    </div>
  );
}

import { LightboxNotice } from "./MobileLightboxNotice";

function LightboxImageStage({
  current,
  currentUseFallback,
  fallbackItemIds,
  gestureTargetRef,
  imgStatus,
  posterUrl,
  showPoster,
  visibleSlides,
  dragX,
  dragY,
  scale,
  onMarkFallback,
  setUseFallback,
  setImgStatus,
}: {
  current: LightboxItem;
  currentUseFallback: boolean;
  fallbackItemIds: ReadonlySet<string>;
  gestureTargetRef: RefObject<HTMLDivElement | null>;
  imgStatus: ImgStatus;
  posterUrl: string;
  showPoster: boolean;
  visibleSlides: VisibleSlide[];
  dragX: MotionValue<number>;
  dragY: MotionValue<number>;
  scale: MotionValue<number>;
  onMarkFallback: (id: string) => void;
  setUseFallback: Dispatch<SetStateAction<boolean>>;
  setImgStatus: Dispatch<SetStateAction<ImgStatus>>;
}) {
  if (imgStatus === "error") {
    return (
      <div
        ref={gestureTargetRef}
        className="absolute inset-0 flex items-center justify-center overflow-hidden"
        style={{ touchAction: "none" }}
      >
        <ErrorState
          title="图片加载失败"
          description="网络异常或图片已过期。"
          onRetry={() => setImgStatus("loading")}
          className="mx-4 max-w-[280px]"
        />
      </div>
    );
  }

  return (
    <div
      ref={gestureTargetRef}
      className="absolute inset-0 flex items-center justify-center overflow-hidden"
      style={{ touchAction: "none" }}
    >
      <motion.div
        className="absolute inset-0"
        style={{
          x: dragX,
          y: dragY,
          willChange: "transform",
          backfaceVisibility: "hidden",
        }}
      >
        {visibleSlides.map(({ item, offset }) => {
          const active = offset === 0;
          const slideUseFallback = active
            ? currentUseFallback
            : fallbackItemIds.has(item.id);
          const slideDisplayUrl = displayUrlForItem(item, slideUseFallback);
          const slideCanFallback =
            !slideUseFallback &&
            Boolean(item.previewUrl) &&
            item.previewUrl !== item.url;
          const slideLoading = active && imgStatus === "loading";
          return (
            <div
              key={item.id}
              aria-hidden={!active}
              className="pointer-events-none absolute inset-0 flex items-center justify-center"
              style={{
                transform: `translate3d(${offset * 100}%, 0, 0)`,
                willChange: "transform",
                contain: "layout paint",
              }}
            >
              {active && showPoster ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img
                  src={posterUrl}
                  alt=""
                  aria-hidden
                  draggable={false}
                  className="pointer-events-none absolute max-h-full max-w-full select-none object-contain opacity-60"
                />
              ) : null}
              <motion.img
                src={slideDisplayUrl}
                alt={active ? (current.prompt ?? "") : ""}
                draggable={false}
                loading={active ? "eager" : "lazy"}
                decoding="async"
                fetchPriority={active ? "high" : "low"}
                onLoad={(event) => {
                  markImageDecoded(slideDisplayUrl);
                  const img = event.currentTarget;
                  if (img.complete && img.naturalWidth > 0) {
                    void img.decode?.().catch(() => undefined);
                  }
                  if (active) setImgStatus("loaded");
                }}
                onError={() => {
                  if (slideCanFallback) {
                    onMarkFallback(item.id);
                    if (active) {
                      setUseFallback(true);
                      setImgStatus(
                        isImageDecoded(item.url) ? "loaded" : "loading",
                      );
                    }
                    return;
                  }
                  if (active) setImgStatus("error");
                }}
                className={cn(
                  "max-h-full max-w-full select-none object-contain",
                  "transform-gpu will-change-transform",
                  slideLoading ? "opacity-0" : "opacity-100",
                )}
                style={{
                  scale: active ? scale : 1,
                  touchAction: "none",
                  userSelect: "none",
                  WebkitUserSelect: "none",
                  backfaceVisibility: "hidden",
                }}
              />
            </div>
          );
        })}
      </motion.div>
      {imgStatus === "loading" ? (
        <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
          <span className="absolute inset-0 bg-[var(--media-control-bg)] opacity-20" />
          <Spinner
            size={24}
            className="relative text-[var(--media-control-fg)] opacity-70"
          />
        </div>
      ) : null}
    </div>
  );
}

function DownloadStatusIcon({ status }: { status: DownloadStatus }) {
  if (status === "downloading") {
    return <Spinner size={16} />;
  }
  if (status === "success") return <Check className="h-5 w-5" />;
  if (status === "error") return <AlertCircle className="h-5 w-5" />;
  return <Download className="h-5 w-5" />;
}

function LightboxTopBar({
  chromeVisible,
  closeButtonRef,
  downloadStatus,
  idx,
  isZoomed,
  sourceLabel,
  total,
  zoomPercent,
  onClose,
  onDownload,
}: {
  chromeVisible: boolean;
  closeButtonRef: RefObject<HTMLButtonElement | null>;
  downloadStatus: DownloadStatus;
  idx: number;
  isZoomed: boolean;
  sourceLabel: string;
  total: number;
  zoomPercent: string;
  onClose: () => void;
  onDownload: () => void;
}) {
  return (
    <motion.div
      aria-hidden={!chromeVisible}
      animate={chromeVisible ? { opacity: 1, y: 0 } : { opacity: 0, y: -10 }}
      transition={{ duration: DURATION.normal, ease: EASE.shutter }}
      className={cn(
        "pointer-events-none absolute inset-x-0 top-0 flex items-center justify-between",
        "bg-gradient-to-b from-[var(--media-control-bg)] to-transparent px-3 pb-4 pt-[calc(env(safe-area-inset-top)+8px)]",
      )}
    >
      <MediaControlButton
        ref={closeButtonRef}
        size="lg"
        aria-label="关闭"
        onClick={onClose}
        tabIndex={chromeVisible ? undefined : -1}
        className="pointer-events-auto shadow-[var(--shadow-2)]"
      >
        <X className="h-5 w-5" />
      </MediaControlButton>
      <div className="type-body-sm pointer-events-none flex items-center gap-2 rounded-full bg-[var(--media-control-bg)] px-3.5 py-2 text-[var(--media-control-fg)] tabular-nums">
        <span>{total > 1 ? `${idx + 1} / ${total}` : sourceLabel}</span>
        {isZoomed ? (
          <>
            <span className="h-3 w-px bg-[var(--media-control-fg)] opacity-20" />
            <span>{zoomPercent}</span>
          </>
        ) : null}
      </div>
      <MediaControlButton
        size="lg"
        aria-label={downloadStatus === "downloading" ? "下载中" : "下载原图"}
        onClick={onDownload}
        disabled={downloadStatus === "downloading"}
        tabIndex={chromeVisible ? undefined : -1}
        className="pointer-events-auto shadow-[var(--shadow-2)]"
      >
        <DownloadStatusIcon status={downloadStatus} />
      </MediaControlButton>
    </motion.div>
  );
}

function LightboxNavigation({
  chromeVisible,
  isFirst,
  isLast,
  total,
  onGoto,
}: {
  chromeVisible: boolean;
  isFirst: boolean;
  isLast: boolean;
  total: number;
  onGoto: (delta: 1 | -1) => void;
}) {
  if (total <= 1) return null;
  return (
    <>
      <motion.div
        aria-hidden={!chromeVisible}
        aria-label="上一张"
        animate={chromeVisible ? { opacity: 1, x: 0 } : { opacity: 0, x: -8 }}
        transition={{ duration: DURATION.normal, ease: EASE.shutter }}
        className={cn(
          "absolute left-3 top-1/2 -translate-y-1/2",
          !chromeVisible && "pointer-events-none",
        )}
      >
        <MediaControlButton
          size="lg"
          onClick={() => onGoto(-1)}
          disabled={isFirst}
          tabIndex={chromeVisible ? undefined : -1}
          aria-label="上一张"
          className="shadow-[var(--shadow-2)]"
        >
          <ChevronLeft className="h-5 w-5" />
        </MediaControlButton>
      </motion.div>
      <motion.div
        aria-hidden={!chromeVisible}
        aria-label="下一张"
        animate={chromeVisible ? { opacity: 1, x: 0 } : { opacity: 0, x: 8 }}
        transition={{ duration: DURATION.normal, ease: EASE.shutter }}
        className={cn(
          "absolute right-3 top-1/2 -translate-y-1/2",
          !chromeVisible && "pointer-events-none",
        )}
      >
        <MediaControlButton
          size="lg"
          onClick={() => onGoto(1)}
          disabled={isLast}
          tabIndex={chromeVisible ? undefined : -1}
          aria-label="下一张"
          className="shadow-[var(--shadow-2)]"
        >
          <ChevronRight className="h-5 w-5" />
        </MediaControlButton>
      </motion.div>
    </>
  );
}

function LightboxZoomReset({
  chromeVisible,
  isZoomed,
  zoomPercent,
  onReset,
}: {
  chromeVisible: boolean;
  isZoomed: boolean;
  zoomPercent: string;
  onReset: () => void;
}) {
  if (!isZoomed) return null;
  return (
    <motion.div
      initial={{ opacity: 0, y: -6 }}
      animate={{ opacity: chromeVisible ? 1 : 0.82, y: 0 }}
      exit={{ opacity: 0, y: -6 }}
      transition={{ duration: DURATION.normal, ease: EASE.shutter }}
      className="absolute left-1/2 top-[calc(env(safe-area-inset-top)+4rem)] -translate-x-1/2"
    >
      <MediaControlButton
        size="md"
        onClick={onReset}
        aria-label="重置缩放"
        className="type-caption w-auto gap-1.5 px-3 font-mono shadow-[var(--shadow-2)]"
      >
        <RotateCcw className="h-3.5 w-3.5" />
        {zoomPercent}
      </MediaControlButton>
    </motion.div>
  );
}

function LightboxThumbnailStrip({
  activeThumbRef,
  chromeVisible,
  current,
  idx,
  thumbItems,
  total,
  onSwitchItem,
}: {
  activeThumbRef: RefObject<HTMLButtonElement | null>;
  chromeVisible: boolean;
  current: LightboxItem;
  idx: number;
  thumbItems: ThumbnailItem[];
  total: number;
  onSwitchItem: (item: LightboxItem) => void;
}) {
  if (total <= 1) return null;
  return (
    <div className="pointer-events-auto mx-auto mb-3.5 flex max-w-[34rem] gap-2.5 overflow-x-auto px-1 py-1 no-scrollbar">
      {thumbItems.map(({ item, itemIdx }) => {
        const active = item.id === current.id;
        return (
          <button
            key={item.id}
            ref={active ? activeThumbRef : undefined}
            type="button"
            onClick={() => {
              if (!active) onSwitchItem(item);
            }}
            tabIndex={chromeVisible ? undefined : -1}
            aria-label={`第 ${itemIdx + 1} 张`}
            aria-current={active}
            className={cn(
              "relative h-12 w-12 shrink-0 overflow-hidden rounded-[var(--radius-card)] border",
              "bg-[var(--media-control-bg)] shadow-[var(--shadow-1)] transition-[border-color,opacity] duration-200",
              active
                ? "border-accent-border opacity-100 ring-2 ring-[var(--accent)]"
                : "border-[var(--border)] opacity-60 active:opacity-100",
            )}
          >
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={item.thumbUrl ?? item.previewUrl ?? item.url}
              alt=""
              loading={Math.abs(itemIdx - idx) <= 4 ? "eager" : "lazy"}
              decoding="async"
              fetchPriority={Math.abs(itemIdx - idx) <= 2 ? "high" : "low"}
              draggable={false}
              className="h-full w-full object-cover"
            />
          </button>
        );
      })}
    </div>
  );
}

function LightboxActionBar({
  chromeVisible,
  hasPrompt,
  onCopyPrompt,
  onInpaint,
  onIterate,
  onOpenParams,
  onReroll,
  onShare,
  onUpscale,
}: {
  chromeVisible: boolean;
  hasPrompt: boolean;
  onCopyPrompt: () => void;
  onInpaint: () => void;
  onIterate: () => void;
  onOpenParams: () => void;
  onReroll: () => void;
  onShare: () => void;
  onUpscale: () => void;
}) {
  const tabIndex = chromeVisible ? undefined : -1;
  const actions = [
    {
      label: "局部修改",
      icon: <Brush className="h-4 w-4" />,
      onSelect: onInpaint,
    },
    {
      label: "放大到 4K",
      icon: <ArrowUpRight className="h-4 w-4" />,
      onSelect: onUpscale,
    },
    {
      label: "重新生成",
      icon: <RefreshCw className="h-4 w-4" />,
      onSelect: onReroll,
    },
    ...(hasPrompt
      ? [
          {
            label: "复制提示词",
            icon: <Copy className="h-4 w-4" />,
            onSelect: onCopyPrompt,
          },
        ]
      : []),
    {
      label: "分享",
      icon: <Share2 className="h-4 w-4" />,
      onSelect: onShare,
    },
    {
      label: "图片信息",
      icon: <Info className="h-4 w-4" />,
      onSelect: onOpenParams,
    },
  ];

  return (
    <div className="mx-auto mt-2 flex max-w-[34rem] justify-center gap-2">
      <MediaControlButton
        size="lg"
        onClick={onIterate}
        tabIndex={tabIndex}
        aria-label="迭代"
        className="type-body-sm pointer-events-auto w-auto gap-1.5 px-4 shadow-[var(--shadow-2)]"
      >
        <Pencil className="h-4 w-4" aria-hidden />
        迭代
      </MediaControlButton>
      <LightboxActionMenu actions={actions} side="top" tabIndex={tabIndex} />
    </div>
  );
}

function LightboxInjectedAction({
  action,
  chromeVisible,
  current,
}: {
  action: LightboxAction | null;
  chromeVisible: boolean;
  current: LightboxItem;
}) {
  if (!action) return null;
  return (
    <div className="mx-auto mt-2 flex max-w-[34rem] justify-center">
      <Button
        variant="primary"
        size="md"
        loading={action.pending}
        onClick={() => action.onClick(current)}
        tabIndex={chromeVisible ? undefined : -1}
        leftIcon={<Check className="h-3.5 w-3.5" aria-hidden />}
        className="pointer-events-auto rounded-full shadow-[var(--shadow-amber)]"
      >
        {action.label}
      </Button>
    </div>
  );
}

function LightboxFooter({
  activeThumbRef,
  chromeVisible,
  current,
  idx,
  lightboxAction,
  thumbItems,
  total,
  onCopyPrompt,
  onInpaint,
  onIterate,
  onOpenParams,
  onReroll,
  onShare,
  onSwitchItem,
  onUpscale,
}: {
  activeThumbRef: RefObject<HTMLButtonElement | null>;
  chromeVisible: boolean;
  current: LightboxItem;
  idx: number;
  lightboxAction: LightboxAction | null;
  thumbItems: ThumbnailItem[];
  total: number;
  onCopyPrompt: () => void;
  onInpaint: () => void;
  onIterate: () => void;
  onOpenParams: () => void;
  onReroll: () => void;
  onShare: () => void;
  onSwitchItem: (item: LightboxItem) => void;
  onUpscale: () => void;
}) {
  return (
    <motion.div
      aria-hidden={!chromeVisible}
      animate={chromeVisible ? { opacity: 1, y: 0 } : { opacity: 0, y: 14 }}
      transition={{ duration: DURATION.normal, ease: EASE.shutter }}
      className={cn(
        "absolute inset-x-0 bottom-0 px-3 pt-6",
        "pb-[var(--mobile-dialog-footer-pad-bottom)]",
        "mobile-dialog-scroll max-h-[min(42dvh,20rem)] overflow-y-auto overscroll-contain",
        "bg-gradient-to-t from-[var(--media-control-bg)] via-[var(--media-control-bg)] to-transparent",
        "pointer-events-auto",
        !chromeVisible && "pointer-events-none",
      )}
      style={{ touchAction: "pan-y" }}
    >
      <LightboxThumbnailStrip
        activeThumbRef={activeThumbRef}
        chromeVisible={chromeVisible}
        current={current}
        idx={idx}
        thumbItems={thumbItems}
        total={total}
        onSwitchItem={onSwitchItem}
      />
      <LightboxActionBar
        chromeVisible={chromeVisible}
        hasPrompt={Boolean(current.prompt)}
        onCopyPrompt={onCopyPrompt}
        onInpaint={onInpaint}
        onIterate={onIterate}
        onOpenParams={onOpenParams}
        onReroll={onReroll}
        onShare={onShare}
        onUpscale={onUpscale}
      />
      <LightboxInjectedAction
        action={lightboxAction}
        chromeVisible={chromeVisible}
        current={current}
      />
    </motion.div>
  );
}
