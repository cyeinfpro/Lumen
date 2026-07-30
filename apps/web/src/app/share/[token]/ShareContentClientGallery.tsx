"use client";

/* eslint-disable @next/next/no-img-element -- Share images are public API binaries with variant fallbacks and download handling. */

import {
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type PointerEvent,
} from "react";
import {
  ChevronLeft,
  ChevronRight,
  Download,
  ExternalLink,
  ImageOff,
  Loader2,
  Maximize2,
  X,
} from "lucide-react";

import type { PublicShareImageOut } from "@/lib/types";
import { cn } from "@/lib/utils";
import {
  candidateUrls,
  imageFrameStyle,
  lightboxImageFrameStyle,
  lowQualityPlaceholderUrl,
  shareImageAlt,
  singleImageFrameStyle,
  sizesForSurface,
  srcSetForImage,
  type ShareImageSurface,
} from "./share-content-utils";

export function ShareImageTile({
  image,
  index,
  single = false,
  priority = false,
  downloading = false,
  onOpen,
  onDownload,
}: {
  image: PublicShareImageOut;
  index: number;
  single?: boolean;
  priority?: boolean;
  downloading?: boolean;
  onOpen: (index: number) => void;
  onDownload: (image: PublicShareImageOut) => void;
}) {
  const alt = shareImageAlt(image);
  const frameStyle = single
    ? singleImageFrameStyle(image)
    : imageFrameStyle(image);

  return (
    <div
      className={cn(
        "share-tile-shell group relative overflow-hidden rounded-[var(--radius-card)] border border-white/10 bg-black text-left shadow-[var(--shadow-3)] transition-[border-color,box-shadow] duration-[var(--dur-normal)] hover:border-white/20 hover:shadow-[var(--shadow-amber)]",
        single
          ? "max-w-full"
          : "mb-1.5 w-full break-inside-avoid min-[390px]:mb-2 md:mb-3",
      )}
    >
      <button
        type="button"
        onClick={() => onOpen(index)}
        className="relative block w-full overflow-hidden bg-[var(--bg-0)] text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]/70"
        style={frameStyle}
        aria-label={`查看第 ${index + 1} 张大图`}
      >
        <ResilientShareImage
          key={`${image.id}-${single ? "single" : "grid"}`}
          image={image}
          surface={single ? "single" : "grid"}
          alt={alt}
          width={image.width}
          height={image.height}
          loading={priority ? "eager" : "lazy"}
          fetchPriority={priority ? "high" : "auto"}
          className={cn(
            "absolute inset-0 h-full w-full",
            single ? "object-contain" : "object-cover",
          )}
        />
        <span className="pointer-events-none absolute inset-0 bg-gradient-to-t from-black/50 via-black/0 to-black/0 opacity-0 transition-opacity duration-200 group-hover:opacity-100" />
        <span className="pointer-events-none absolute bottom-2 left-2 rounded-full border border-white/10 bg-black/45 px-2 py-1 text-[10px] font-mono tabular-nums text-white/75 opacity-0 backdrop-blur transition-opacity duration-200 group-hover:opacity-100">
          {index + 1} · {image.width} x {image.height}
        </span>
      </button>

      <button
        type="button"
        onClick={() => onDownload(image)}
        disabled={downloading}
        className="absolute right-1.5 top-1.5 z-10 inline-flex h-11 w-11 items-center justify-center rounded-full border border-white/15 bg-black/55 text-white/90 backdrop-blur transition-[background-color,border-color,opacity] hover:bg-black/70 disabled:opacity-60 min-[390px]:right-2 min-[390px]:top-2 md:opacity-0 md:group-hover:opacity-100 md:group-focus-within:opacity-100 focus-visible:opacity-100"
        aria-label="下载原图"
      >
        {downloading ? (
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
        ) : (
          <Download className="h-4 w-4" aria-hidden />
        )}
      </button>

      <span className="pointer-events-none absolute left-1.5 top-1.5 inline-flex h-8 w-8 items-center justify-center rounded-full border border-white/15 bg-black/45 text-white/80 opacity-100 backdrop-blur min-[390px]:left-2 min-[390px]:top-2 sm:opacity-0 sm:group-hover:opacity-100">
        <Maximize2 className="h-3.5 w-3.5" aria-hidden />
      </span>
    </div>
  );
}

export function ShareLightbox({
  images,
  index,
  isWeChat,
  downloading,
  onClose,
  onPrev,
  onNext,
  onSelect,
  onDownload,
}: {
  images: PublicShareImageOut[];
  index: number;
  isWeChat: boolean;
  downloading: boolean;
  onClose: () => void;
  onPrev: () => void;
  onNext: () => void;
  onSelect: (index: number) => void;
  onDownload: (image: PublicShareImageOut) => void;
}) {
  const image = images[index];
  const multiple = images.length > 1;
  const gestureRef = useRef<{ x: number; y: number; time: number } | null>(null);
  const [dragX, setDragX] = useState(0);
  const dialogRootRef = useRef<HTMLDivElement | null>(null);
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  const previouslyFocusedRef = useRef<HTMLElement | null>(null);
  const dialogTitleId = useId();

  useEffect(() => {
    const body = document.body;
    const root = document.documentElement;
    const scrollY = window.scrollY;
    const previous = {
      bodyOverflow: body.style.overflow,
      bodyPosition: body.style.position,
      bodyTop: body.style.top,
      bodyWidth: body.style.width,
      rootOverscroll: root.style.overscrollBehavior,
    };

    body.style.overflow = "hidden";
    body.style.position = "fixed";
    body.style.top = `-${scrollY}px`;
    body.style.width = "100%";
    root.style.overscrollBehavior = "none";

    return () => {
      // 防御性比对：仅在样式仍是我们设置的值时才恢复，避免覆盖其他代码后续修改
      if (body.style.overflow === "hidden") {
        body.style.overflow = previous.bodyOverflow;
      }
      if (body.style.position === "fixed") {
        body.style.position = previous.bodyPosition;
      }
      if (body.style.top === `-${scrollY}px`) {
        body.style.top = previous.bodyTop;
      }
      if (body.style.width === "100%") {
        body.style.width = previous.bodyWidth;
      }
      if (root.style.overscrollBehavior === "none") {
        root.style.overscrollBehavior = previous.rootOverscroll;
      }
      window.scrollTo(0, scrollY);
    };
  }, []);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
        return;
      }
      if (event.key === "Tab") {
        const root = dialogRootRef.current;
        if (!root) return;
        const focusables = Array.from(
          root.querySelectorAll<HTMLElement>(
            'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
          ),
        ).filter((el) => !el.hasAttribute("data-focus-skip"));
        if (focusables.length === 0) {
          event.preventDefault();
          return;
        }
        const first = focusables[0];
        const last = focusables[focusables.length - 1];
        const active = document.activeElement as HTMLElement | null;
        if (event.shiftKey) {
          if (active === first || !root.contains(active)) {
            event.preventDefault();
            last.focus();
          }
        } else if (active === last) {
          event.preventDefault();
          first.focus();
        }
        return;
      }
      if (!multiple) return;
      if (event.key === "ArrowLeft") onPrev();
      if (event.key === "ArrowRight") onNext();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [multiple, onClose, onNext, onPrev]);

  // 打开时焦点移到关闭按钮，关闭时还原焦点到打开者（通常是 grid 上的 tile button）
  useEffect(() => {
    previouslyFocusedRef.current = document.activeElement as HTMLElement | null;
    let raf = 0;
    raf = requestAnimationFrame(() => {
      const target = closeButtonRef.current ?? dialogRootRef.current;
      target?.focus({ preventScroll: true });
    });
    return () => {
      cancelAnimationFrame(raf);
      const prev = previouslyFocusedRef.current;
      if (prev && typeof prev.focus === "function") {
        try {
          prev.focus({ preventScroll: true });
        } catch {
          /* noop */
        }
      }
      previouslyFocusedRef.current = null;
    };
  }, []);

  if (!image) return null;
  const lightboxStyle = {
    "--share-lightbox-top-space":
      "calc(env(safe-area-inset-top, 0px) + 4.75rem)",
    "--share-lightbox-footer-space": multiple
      ? "calc(var(--mobile-dialog-footer-pad-bottom) + 8.75rem)"
      : "calc(var(--mobile-dialog-footer-pad-bottom) + 4.5rem)",
  } as React.CSSProperties;

  const onPointerDown = (event: PointerEvent<HTMLDivElement>) => {
    if (
      !multiple ||
      (event.pointerType === "mouse" && event.button !== 0)
    ) {
      return;
    }
    gestureRef.current = {
      x: event.clientX,
      y: event.clientY,
      // performance.now() 单调递增，避免系统时钟跳跃（移动端切换/休眠）导致 elapsed 异常
      time: performance.now(),
    };
    event.currentTarget.setPointerCapture?.(event.pointerId);
  };

  const onPointerMove = (event: PointerEvent<HTMLDivElement>) => {
    const start = gestureRef.current;
    if (!start || !multiple) return;
    const dx = event.clientX - start.x;
    const dy = event.clientY - start.y;
    if (Math.abs(dx) > Math.abs(dy) && Math.abs(dx) > 8) {
      setDragX(Math.max(-90, Math.min(90, dx * 0.32)));
    }
  };

  const onPointerUp = (event: PointerEvent<HTMLDivElement>) => {
    const start = gestureRef.current;
    gestureRef.current = null;
    setDragX(0);
    if (!start || !multiple) return;
    const dx = event.clientX - start.x;
    const dy = event.clientY - start.y;
    // elapsed 应该总是正数且合理范围（< 1000ms）；使用 performance.now() 与 start.time 配对
    const elapsed = Math.max(0, performance.now() - start.time);
    if (elapsed > 650) return; // 超过阈值直接忽略（系统时间异常或长按）
    if (Math.abs(dx) > 56 && Math.abs(dx) > Math.abs(dy) * 1.18) {
      if (dx > 0) onPrev();
      else onNext();
    }
  };

  return (
    <div
      ref={dialogRootRef}
      tabIndex={-1}
      style={lightboxStyle}
      className="fixed inset-0 z-[var(--z-lightbox,80)] flex bg-black text-white share-dialog-in outline-none"
      role="dialog"
      aria-modal="true"
      aria-labelledby={dialogTitleId}
    >
      <span id={dialogTitleId} className="sr-only">
        {`图片预览：${shareImageAlt(image)}`}
      </span>
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_50%_0%,rgba(242,169,58,0.10),transparent_28rem),linear-gradient(180deg,rgba(255,255,255,0.05),transparent_35%)]" />

      <div className="pointer-events-none absolute inset-x-0 top-0 z-20 border-b border-white/10 bg-black/45 px-3 pb-2 pt-[calc(env(safe-area-inset-top,0px)+0.5rem)] backdrop-blur-xl mobile-perf-surface sm:pb-3 sm:pt-[calc(env(safe-area-inset-top,0px)+0.75rem)]">
        <div className="flex items-center justify-between gap-2">
          <div className="min-w-0 rounded-full border border-white/10 bg-white/10 px-3 py-2 text-xs font-mono tabular-nums text-white/80">
            {index + 1}/{images.length}
          </div>
          <div className="pointer-events-auto flex items-center gap-2">
            <a
              href={image.image_url}
              target="_blank"
              rel="noopener noreferrer"
              className="hidden min-h-11 items-center justify-center gap-1.5 rounded-full border border-white/15 bg-white/10 px-3 text-xs text-white backdrop-blur transition-colors hover:bg-white/15 sm:inline-flex"
            >
              <ExternalLink className="h-4 w-4" />
              原图
            </a>
            <button
              type="button"
              onClick={() => onDownload(image)}
              disabled={downloading}
              className="hidden min-h-11 items-center justify-center gap-1.5 rounded-full border border-white/15 bg-white/10 px-3 text-xs text-white backdrop-blur transition-colors hover:bg-white/15 disabled:opacity-55 sm:inline-flex"
            >
              {downloading ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Download className="h-4 w-4" />
              )}
              {downloading ? "准备中" : "下载"}
            </button>
            <button
              ref={closeButtonRef}
              type="button"
              aria-label="关闭"
              onClick={onClose}
              className="inline-flex h-11 w-11 items-center justify-center rounded-full border border-white/15 bg-white/10 text-white backdrop-blur transition-colors hover:bg-white/15"
            >
              <X className="h-5 w-5" />
            </button>
          </div>
        </div>
      </div>

      {multiple && (
        <>
          <button
            type="button"
            aria-label="上一张"
            onClick={onPrev}
            className="absolute left-4 top-1/2 z-20 hidden h-12 w-12 -translate-y-1/2 items-center justify-center rounded-full border border-white/15 bg-white/10 text-white backdrop-blur transition-colors hover:bg-white/15 sm:inline-flex"
          >
            <ChevronLeft className="h-6 w-6" />
          </button>
          <button
            type="button"
            aria-label="下一张"
            onClick={onNext}
            className="absolute right-4 top-1/2 z-20 hidden h-12 w-12 -translate-y-1/2 items-center justify-center rounded-full border border-white/15 bg-white/10 text-white backdrop-blur transition-colors hover:bg-white/15 sm:inline-flex"
          >
            <ChevronRight className="h-6 w-6" />
          </button>
        </>
      )}

      <div
        className={cn(
          "relative z-10 flex min-h-0 w-full flex-1 touch-pan-y select-none items-center justify-center px-3 pt-[var(--share-lightbox-top-space)] sm:px-16 sm:pt-24",
          multiple
            ? "pb-[var(--share-lightbox-footer-space)] sm:pb-36"
            : "pb-[var(--share-lightbox-footer-space)] sm:pb-28",
        )}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={() => {
          gestureRef.current = null;
          setDragX(0);
        }}
      >
        <div
          className="relative max-w-full overflow-hidden transition-transform duration-[var(--dur-normal)] ease-[var(--ease-develop)]"
          style={{
            ...lightboxImageFrameStyle(image),
            transform: dragX ? `translate3d(${dragX}px,0,0)` : undefined,
          }}
        >
          <ResilientShareImage
            key={`${image.id}-lightbox`}
            image={image}
            surface="lightbox"
            alt={shareImageAlt(image)}
            width={image.width}
            height={image.height}
            loading="eager"
            fetchPriority="high"
            className="absolute inset-0 h-full w-full object-contain"
          />
        </div>
      </div>

      {multiple && (
        <ShareFilmstrip
          images={images}
          activeIndex={index}
          onSelect={onSelect}
        />
      )}

      <div className="absolute inset-x-0 bottom-0 z-20 border-t border-white/10 bg-black/[0.72] px-3 pb-[var(--mobile-dialog-footer-pad-bottom)] pt-2 backdrop-blur-xl mobile-perf-surface sm:pb-[calc(env(safe-area-inset-bottom,0px)+0.75rem)] sm:pt-3">
        <div className="mx-auto flex w-full max-w-4xl items-center gap-2">
          <button
            type="button"
            onClick={() => onDownload(image)}
            disabled={downloading}
            className="inline-flex min-h-11 flex-1 items-center justify-center gap-2 rounded-[var(--radius-card)] bg-[var(--accent)] px-3 text-sm font-medium text-black transition-[filter,opacity] hover:brightness-110 active:opacity-[var(--op-press)] disabled:opacity-70 sm:px-4"
          >
            {downloading ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <Download className="h-4 w-4" />
            )}
            {isWeChat ? "打开原图" : downloading ? "准备中" : "下载原图"}
          </button>
          <a
            href={image.image_url}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex min-h-11 items-center justify-center gap-1.5 rounded-[var(--radius-card)] border border-white/15 bg-white/10 px-3 text-sm text-white transition-colors hover:bg-white/15"
          >
            <ExternalLink className="h-4 w-4" />
            原图
          </a>
        </div>
        <div className="mx-auto mt-2 hidden w-full max-w-4xl flex-wrap items-center justify-between gap-x-3 gap-y-1 text-[11px] text-white/[0.62] sm:flex">
          <span className="font-mono tabular-nums">
            {image.width} x {image.height} · {image.mime}
          </span>
          {isWeChat ? (
            <span>长按图片可保存；原图按钮打开最高分辨率。</span>
          ) : image.prompt ? (
            <span className="max-w-full truncate sm:max-w-[52vw]">
              {image.prompt}
            </span>
          ) : null}
        </div>
      </div>
    </div>
  );
}

function ShareFilmstrip({
  images,
  activeIndex,
  onSelect,
}: {
  images: PublicShareImageOut[];
  activeIndex: number;
  onSelect: (index: number) => void;
}) {
  return (
    <div className="absolute inset-x-0 bottom-[calc(var(--mobile-dialog-footer-pad-bottom)+3.75rem)] z-20 sm:bottom-[calc(env(safe-area-inset-bottom,0px)+5.8rem)]">
      <div className="mx-auto flex max-w-4xl scroll-px-3 gap-2 overflow-x-auto px-3 py-2 no-scrollbar">
        {images.map((image, index) => (
          <button
            key={image.id}
            type="button"
            onClick={() => onSelect(index)}
            className={cn(
              "relative h-14 w-14 flex-none overflow-hidden rounded-[var(--radius-control)] border bg-white/5 transition-[border-color,opacity] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]",
              index === activeIndex
                ? "border-[var(--accent)] opacity-100"
                : "border-white/15 opacity-[0.62] hover:opacity-90",
            )}
            aria-label={`查看第 ${index + 1} 张`}
          >
            <ResilientShareImage
              image={image}
              surface="filmstrip"
              alt=""
              width={image.width}
              height={image.height}
              loading="lazy"
              fetchPriority="auto"
              className="absolute inset-0 h-full w-full object-cover"
            />
          </button>
        ))}
      </div>
    </div>
  );
}

function ResilientShareImage({
  image,
  surface,
  alt,
  className,
  width,
  height,
  loading,
  fetchPriority,
}: {
  image: PublicShareImageOut;
  surface: ShareImageSurface;
  alt: string;
  className?: string;
  width: number;
  height: number;
  loading: "eager" | "lazy";
  fetchPriority: "high" | "auto";
}) {
  const candidates = useMemo(
    () => candidateUrls(image, surface),
    [image, surface],
  );
  const [attempt, setAttempt] = useState(0);
  const [loaded, setLoaded] = useState(false);
  const src = candidates[attempt];
  const failed = !src;
  const lowQualitySrc = lowQualityPlaceholderUrl(image, surface);
  const showLowQualityPreview =
    Boolean(lowQualitySrc) && !loaded && !failed && lowQualitySrc !== src;

  return (
    <>
      <ShareImageLowQualityPreview
        visible={showLowQualityPreview}
        src={lowQualitySrc}
        surface={surface}
        loading={loading}
      />
      {src ? (
        <img
          key={src}
          src={src}
          srcSet={attempt === 0 ? srcSetForImage(image) : undefined}
          sizes={sizesForSurface(surface)}
          alt={alt}
          width={width}
          height={height}
          loading={loading}
          fetchPriority={fetchPriority}
          decoding="async"
          draggable={false}
          onLoad={(event) => {
            if (event.currentTarget.naturalWidth > 0) setLoaded(true);
          }}
          onError={() => {
            setLoaded(false);
            setAttempt((current) => current + 1);
          }}
          className={cn(
            className,
            "transition-opacity duration-500 ease-out will-change-opacity",
            loaded ? "opacity-100" : "opacity-0",
          )}
        />
      ) : null}
      <ShareImageLoadingOverlay
        visible={!loaded && !failed}
        surface={surface}
      />
      <ShareImageFailure visible={failed} />
    </>
  );
}

function ShareImageLowQualityPreview({
  visible,
  src,
  surface,
  loading,
}: {
  visible: boolean;
  src: string | null;
  surface: ShareImageSurface;
  loading: "eager" | "lazy";
}) {
  if (!visible || !src) return null;
  return (
    <img
      src={src}
      alt=""
      aria-hidden
      loading={loading}
      decoding="async"
      className={cn(
        "pointer-events-none absolute inset-0 h-full w-full",
        surface === "lightbox"
          ? "opacity-35"
          : "scale-[1.025] opacity-60 blur-md",
        surface === "lightbox" || surface === "single"
          ? "object-contain"
          : "object-cover",
      )}
    />
  );
}

function ShareImageLoadingOverlay({
  visible,
  surface,
}: {
  visible: boolean;
  surface: ShareImageSurface;
}) {
  if (!visible) return null;
  return (
    <span
      className={cn(
        "pointer-events-none absolute inset-0 flex items-center justify-center bg-[linear-gradient(110deg,rgba(255,255,255,0.05),rgba(255,255,255,0.12),rgba(255,255,255,0.05))] bg-[length:220%_100%] animate-lumen-shimmer",
        surface === "lightbox"
          ? "bg-black/[0.18]"
          : "bg-white/[0.035]",
      )}
    >
      {surface === "lightbox" ? (
        <Loader2
          className="h-5 w-5 animate-spin text-white/55"
          aria-hidden
        />
      ) : null}
    </span>
  );
}

function ShareImageFailure({ visible }: { visible: boolean }) {
  if (!visible) return null;
  return (
    <span className="pointer-events-none absolute inset-0 flex min-h-32 flex-col items-center justify-center gap-2 bg-[var(--bg-0)] px-4 text-center text-xs text-[var(--fg-1)]">
      <ImageOff className="h-6 w-6 text-[var(--fg-2)]" aria-hidden />
      <span>图片暂时不可用</span>
    </span>
  );
}
