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

import { DURATION, EASE, SPRING } from "@/lib/motion";
import { cn } from "@/lib/utils";
import { Spinner } from "@/components/ui/primitives/Spinner";
import { MobileIconButton } from "@/components/ui/primitives/mobile/MobileIconButton";
import type { LightboxAction } from "@/store/useUiStore";

import { LightboxParamsPanel } from "./LightboxParamsPanel";
import {
  displayUrlForItem,
  isImageDecoded,
  markImageDecoded,
  posterUrlForItem,
} from "./mobileLightboxMedia";
import type { LightboxItem } from "./types";

export type ImgStatus = "loading" | "loaded" | "error";
export type DownloadStatus = "idle" | "downloading" | "success" | "error";
export type ActionNotice = {
  kind: "success" | "error" | "info";
  text: string;
} | null;
export type VisibleSlide = {
  item: LightboxItem;
  offset: -1 | 0 | 1;
};
export type ThumbnailItem = {
  item: LightboxItem;
  itemIdx: number;
};

interface MobileLightboxViewProps {
  current: LightboxItem | null;
  idx: number;
  total: number;
  isFirst: boolean;
  isLast: boolean;
  paramsOpen: boolean;
  imgStatus: ImgStatus;
  useFallback: boolean;
  fallbackItemIds: ReadonlySet<string>;
  chromeVisible: boolean;
  zoomLevel: number;
  downloadStatus: DownloadStatus;
  actionNotice: ActionNotice;
  boundaryHint: "first" | "last" | null;
  lightboxAction: LightboxAction | null;
  visibleSlides: VisibleSlide[];
  thumbItems: ThumbnailItem[];
  gestureTargetRef: RefObject<HTMLDivElement | null>;
  downloadAnchorRef: RefObject<HTMLAnchorElement | null>;
  dialogRootRef: RefObject<HTMLDivElement | null>;
  closeButtonRef: RefObject<HTMLButtonElement | null>;
  activeThumbRef: RefObject<HTMLButtonElement | null>;
  dialogTitleId: string;
  dragX: MotionValue<number>;
  dragY: MotionValue<number>;
  scale: MotionValue<number>;
  haloOpacity: MotionValue<number>;
  onClose: () => void;
  onGoto: (delta: 1 | -1) => void;
  onResetZoom: () => void;
  onDownload: () => void;
  onSwitchItem: (item: LightboxItem) => void;
  onMarkFallback: (id: string) => void;
  setUseFallback: Dispatch<SetStateAction<boolean>>;
  setImgStatus: Dispatch<SetStateAction<ImgStatus>>;
  onIterate: () => void;
  onInpaint: () => void;
  onUpscale: () => void;
  onReroll: () => void;
  onCopyPrompt: () => void;
  onShare: () => void;
  onOpenParams: () => void;
  onCloseParams: () => void;
}

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
      className="fixed inset-0 overflow-hidden outline-none"
      style={{ zIndex: "var(--z-lightbox, 80)" as unknown as number }}
    >
      <span id={dialogTitleId} className="sr-only">
        {current.prompt ? `图片预览：${current.prompt}` : "图片查看器"}
      </span>
      <motion.div
        aria-hidden
        className="absolute inset-0 bg-black"
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

function LightboxNotice({
  actionNotice,
  boundaryHint,
}: {
  actionNotice: ActionNotice;
  boundaryHint: "first" | "last" | null;
}) {
  if (!actionNotice && !boundaryHint) return null;
  const text =
    boundaryHint === "first"
      ? "已经是第一张"
      : boundaryHint === "last"
        ? "已经是最后一张"
        : actionNotice?.text;
  const isError = actionNotice?.kind === "error";
  return (
    <motion.div
      key={actionNotice?.text ?? boundaryHint}
      initial={{ opacity: 0, y: -8, scale: 0.98 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      exit={{ opacity: 0, y: -8, scale: 0.98 }}
      transition={SPRING.snap}
      role={isError ? "alert" : "status"}
      aria-live={isError ? "assertive" : "polite"}
      className={cn(
        "pointer-events-none absolute left-1/2 top-[calc(env(safe-area-inset-top)+4.25rem)]",
        "-translate-x-1/2 rounded-full border px-3 py-1.5",
        "bg-black/62 text-[12px] text-white/86 shadow-lg",
        isError ? "border-danger-border" : "border-white/12",
      )}
    >
      {text}
    </motion.div>
  );
}

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
        <div
          role="alert"
          className="max-w-[280px] rounded-[var(--radius-dialog)] border border-white/10 bg-black/50 px-8 py-10 text-center"
        >
          <p className="text-base text-white/90">图片加载失败</p>
          <p className="mt-2 text-xs text-white/50">
            数据可能已过期或网络异常，可关闭后重试。
          </p>
        </div>
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
        <div className="pointer-events-none absolute inset-0 flex items-center justify-center bg-black/10">
          <Spinner size={24} className="text-white/50" />
        </div>
      ) : null}
    </div>
  );
}

function DownloadStatusIcon({ status }: { status: DownloadStatus }) {
  if (status === "downloading") {
    return <Spinner size={16} className="text-white" />;
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
        "bg-gradient-to-b from-black/55 to-transparent px-3 pb-4 pt-[calc(env(safe-area-inset-top)+8px)]",
      )}
    >
      <MobileIconButton
        ref={closeButtonRef}
        icon={<X className="h-5 w-5" />}
        label="关闭"
        variant="plain"
        onPress={onClose}
        tabIndex={chromeVisible ? undefined : -1}
        className="pointer-events-auto border border-white/10 bg-black/55 text-white"
      />
      <div className="pointer-events-none flex items-center gap-2 rounded-full border border-white/10 bg-black/50 px-3.5 py-2 font-mono text-[13px] text-white/85 tabular-nums">
        <span>{total > 1 ? `${idx + 1} / ${total}` : sourceLabel}</span>
        {isZoomed ? (
          <>
            <span className="h-3 w-px bg-white/18" />
            <span>{zoomPercent}</span>
          </>
        ) : null}
      </div>
      <MobileIconButton
        icon={<DownloadStatusIcon status={downloadStatus} />}
        label={downloadStatus === "downloading" ? "正在下载" : "下载原图"}
        variant="plain"
        onPress={onDownload}
        disabled={downloadStatus === "downloading"}
        tabIndex={chromeVisible ? undefined : -1}
        className={cn(
          "pointer-events-auto inline-flex h-11 w-11 items-center justify-center",
          "rounded-full border border-white/10 bg-black/55 text-white",
          "transition-transform active:scale-95",
        )}
      />
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
      <motion.button
        type="button"
        onClick={() => onGoto(-1)}
        disabled={isFirst}
        tabIndex={chromeVisible ? undefined : -1}
        aria-hidden={!chromeVisible}
        aria-label="上一张"
        animate={chromeVisible ? { opacity: 1, x: 0 } : { opacity: 0, x: -8 }}
        transition={{ duration: DURATION.normal, ease: EASE.shutter }}
        className={cn(
          "absolute left-3 top-1/2 inline-flex h-11 w-11 -translate-y-1/2 items-center justify-center rounded-full",
          "border border-white/10 bg-black/50 text-white transition-transform active:scale-95 disabled:opacity-25",
          !chromeVisible && "pointer-events-none",
        )}
      >
        <ChevronLeft className="h-5 w-5" />
      </motion.button>
      <motion.button
        type="button"
        onClick={() => onGoto(1)}
        disabled={isLast}
        tabIndex={chromeVisible ? undefined : -1}
        aria-hidden={!chromeVisible}
        aria-label="下一张"
        animate={chromeVisible ? { opacity: 1, x: 0 } : { opacity: 0, x: 8 }}
        transition={{ duration: DURATION.normal, ease: EASE.shutter }}
        className={cn(
          "absolute right-3 top-1/2 inline-flex h-11 w-11 -translate-y-1/2 items-center justify-center rounded-full",
          "border border-white/10 bg-black/50 text-white transition-transform active:scale-95 disabled:opacity-25",
          !chromeVisible && "pointer-events-none",
        )}
      >
        <ChevronRight className="h-5 w-5" />
      </motion.button>
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
    <motion.button
      type="button"
      onClick={onReset}
      aria-label="重置缩放"
      initial={{ opacity: 0, y: -6 }}
      animate={{ opacity: chromeVisible ? 1 : 0.82, y: 0 }}
      exit={{ opacity: 0, y: -6 }}
      transition={{ duration: DURATION.normal, ease: EASE.shutter }}
      className={cn(
        "absolute left-1/2 top-[calc(env(safe-area-inset-top)+4rem)] -translate-x-1/2",
        "inline-flex h-9 items-center gap-1.5 rounded-full px-3",
        "border border-white/10 bg-black/55 font-mono text-xs text-white/82",
        "transition-transform active:scale-95",
      )}
    >
      <RotateCcw className="h-3.5 w-3.5" />
      {zoomPercent}
    </motion.button>
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
              "relative h-12 w-12 shrink-0 overflow-hidden rounded-[var(--radius-panel)] border",
              "bg-black/45 shadow-sm transition-all duration-200",
              active
                ? "scale-105 border-white opacity-100 ring-2 ring-[var(--color-lumen-amber)]/80"
                : "border-white/10 opacity-60 active:opacity-100",
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
            {active ? (
              <span
                aria-hidden
                className="absolute inset-x-1.5 bottom-1 h-[2px] rounded-full bg-[var(--color-lumen-amber)]"
              />
            ) : null}
          </button>
        );
      })}
    </div>
  );
}

function LightboxCreativeActions({
  chromeVisible,
  onInpaint,
  onIterate,
  onReroll,
  onUpscale,
}: {
  chromeVisible: boolean;
  onInpaint: () => void;
  onIterate: () => void;
  onReroll: () => void;
  onUpscale: () => void;
}) {
  const tabIndex = chromeVisible ? undefined : -1;
  const buttonClass =
    "pointer-events-auto inline-flex h-10 items-center gap-1.5 rounded-full border border-[rgba(242,169,58,0.35)] bg-[rgba(242,169,58,0.2)] px-4 text-[13px] font-medium text-[var(--amber-300)] transition-transform active:scale-95";
  return (
    <div className="mx-auto mt-2 flex max-w-[34rem] flex-wrap justify-center gap-2">
      <button
        type="button"
        onClick={onIterate}
        tabIndex={tabIndex}
        className={buttonClass}
      >
        <Pencil className="h-3.5 w-3.5" aria-hidden />
        迭代
      </button>
      <button
        type="button"
        onClick={onInpaint}
        tabIndex={tabIndex}
        className={buttonClass}
      >
        <Brush className="h-3.5 w-3.5" aria-hidden />
        局部
      </button>
      <button
        type="button"
        onClick={onUpscale}
        tabIndex={tabIndex}
        className={buttonClass}
      >
        <ArrowUpRight className="h-3.5 w-3.5" aria-hidden />
        放大
      </button>
      <button
        type="button"
        onClick={onReroll}
        tabIndex={tabIndex}
        className={buttonClass}
      >
        <RefreshCw className="h-3.5 w-3.5" aria-hidden />
        重画
      </button>
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
      <button
        type="button"
        disabled={action.pending}
        onClick={() => action.onClick(current)}
        tabIndex={chromeVisible ? undefined : -1}
        className={cn(
          "pointer-events-auto inline-flex h-11 items-center gap-2 rounded-full px-5",
          "bg-[var(--color-lumen-amber)] text-[14px] font-semibold text-black",
          "shadow-[var(--shadow-amber)] transition-transform active:scale-95",
          "disabled:cursor-not-allowed disabled:opacity-70",
        )}
      >
        {action.pending ? (
          <Spinner size={16} className="text-black" />
        ) : (
          <Check className="h-3.5 w-3.5" aria-hidden />
        )}
        {action.label}
      </button>
    </div>
  );
}

function LightboxAuxiliaryActions({
  chromeVisible,
  hasPrompt,
  onCopyPrompt,
  onOpenParams,
  onShare,
}: {
  chromeVisible: boolean;
  hasPrompt: boolean;
  onCopyPrompt: () => void;
  onOpenParams: () => void;
  onShare: () => void;
}) {
  const tabIndex = chromeVisible ? undefined : -1;
  const buttonClass =
    "pointer-events-auto inline-flex h-10 items-center gap-1.5 rounded-full border border-white/12 bg-black/50 px-4 text-[13px] font-medium text-white transition-transform active:scale-95";
  return (
    <div className="mx-auto mt-2 flex max-w-[34rem] justify-center gap-2.5">
      {hasPrompt ? (
        <button
          type="button"
          onClick={onCopyPrompt}
          tabIndex={tabIndex}
          className={buttonClass}
        >
          <Copy className="h-3.5 w-3.5" aria-hidden />
          Prompt
        </button>
      ) : null}
      <button
        type="button"
        onClick={onShare}
        tabIndex={tabIndex}
        className={buttonClass}
      >
        <Share2 className="h-3.5 w-3.5" aria-hidden />
        分享
      </button>
      <button
        type="button"
        onClick={onOpenParams}
        tabIndex={tabIndex}
        className={buttonClass}
      >
        <Info className="h-3.5 w-3.5" aria-hidden />
        参数
      </button>
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
        "mobile-dialog-scroll max-h-[min(48dvh,24rem)] overflow-y-auto overscroll-contain",
        "bg-gradient-to-t from-black/65 via-black/30 to-transparent",
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
      <LightboxCreativeActions
        chromeVisible={chromeVisible}
        onInpaint={onInpaint}
        onIterate={onIterate}
        onReroll={onReroll}
        onUpscale={onUpscale}
      />
      <LightboxInjectedAction
        action={lightboxAction}
        chromeVisible={chromeVisible}
        current={current}
      />
      <LightboxAuxiliaryActions
        chromeVisible={chromeVisible}
        hasPrompt={Boolean(current.prompt)}
        onCopyPrompt={onCopyPrompt}
        onOpenParams={onOpenParams}
        onShare={onShare}
      />
    </motion.div>
  );
}
