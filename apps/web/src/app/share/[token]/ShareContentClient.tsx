"use client";

/* eslint-disable @next/next/no-img-element -- Share images are public API binaries with variant fallbacks and download handling. */

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type PointerEvent,
} from "react";
import Link from "next/link";
import { format, formatDistanceToNow } from "date-fns";
import { zhCN } from "date-fns/locale";
import {
  ArrowRight,
  Check,
  ChevronLeft,
  ChevronRight,
  Clock,
  Download,
  ExternalLink,
  ImageOff,
  Images,
  Loader2,
  Maximize2,
  Share2,
  Sparkles,
  X,
} from "lucide-react";

import { cn } from "@/lib/utils";
import type { PublicShareImageOut, PublicShareOut } from "@/lib/types";

type ShareImageSurface = "grid" | "single" | "lightbox" | "filmstrip";
type NoticeKind = "info" | "success" | "error";
type DownloadStatus = "idle" | "downloading" | "success" | "error";
type DownloadResult = "downloaded" | "shared" | "opened" | "wechat" | "cancelled";

interface Notice {
  kind: NoticeKind;
  text: string;
}

interface DownloadState {
  imageId: string;
  status: DownloadStatus;
}

export function ShareContentClient({ data }: { data: PublicShareOut }) {
  const images = useMemo(() => normalizeShareImages(data), [data]);
  const prompts = useMemo(() => sharePrompts(images), [images]);
  const [activeIndex, setActiveIndex] = useState<number | null>(null);
  const [isWeChat, setIsWeChat] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [downloadState, setDownloadState] = useState<DownloadState | null>(null);
  const [linkShared, setLinkShared] = useState(false);
  const noticeTimerRef = useRef<number | null>(null);
  const createdLabel = safeDistanceToNow(data.created_at);
  const expiresLabel = data.expires_at
    ? safeFormat(data.expires_at, "yyyy-MM-dd HH:mm")
    : null;
  const activeImage =
    activeIndex === null
      ? null
      : images[Math.max(0, Math.min(activeIndex, images.length - 1))] ?? null;

  const showNotice = useCallback((next: Notice, timeout = 2400) => {
    setNotice(next);
    if (typeof window === "undefined") return;
    if (noticeTimerRef.current !== null) {
      window.clearTimeout(noticeTimerRef.current);
    }
    noticeTimerRef.current = window.setTimeout(() => {
      setNotice(null);
      noticeTimerRef.current = null;
    }, timeout);
  }, []);

  const openAt = useCallback((index: number) => setActiveIndex(index), []);
  const close = useCallback(() => setActiveIndex(null), []);
  const goPrev = useCallback(() => {
    setActiveIndex((index) =>
      index === null || images.length === 0
        ? index
        : (index - 1 + images.length) % images.length,
    );
  }, [images.length]);
  const goNext = useCallback(() => {
    setActiveIndex((index) =>
      index === null || images.length === 0 ? index : (index + 1) % images.length,
    );
  }, [images.length]);

  useEffect(() => {
    const schedule = scheduleIdle(() => {
      for (const image of images.slice(0, Math.min(images.length, 10))) {
        preloadShareImage(image, "grid");
      }
    });
    return schedule;
  }, [images]);

  useEffect(() => {
    const id = globalThis.setTimeout(() => setIsWeChat(isWeChatBrowser()), 0);
    return () => globalThis.clearTimeout(id);
  }, []);

  useEffect(() => {
    return () => {
      if (noticeTimerRef.current !== null) {
        window.clearTimeout(noticeTimerRef.current);
      }
    };
  }, []);

  useEffect(() => {
    if (activeIndex === null || images.length === 0) return;
    const preloadIndexes = [
      activeIndex,
      (activeIndex + 1) % images.length,
      (activeIndex - 1 + images.length) % images.length,
    ];
    const cancel = scheduleIdle(() => {
      for (const index of new Set(preloadIndexes)) {
        preloadShareImage(images[index], "lightbox");
      }
    });
    return cancel;
  }, [activeIndex, images]);

  const handleDownload = useCallback(
    async (image: PublicShareImageOut) => {
      if (downloadState?.imageId === image.id && downloadState.status === "downloading") {
        return;
      }

      setDownloadState({ imageId: image.id, status: "downloading" });
      showNotice(
        {
          kind: "info",
          text: isWeChat ? "正在打开原图" : "正在准备原图",
        },
        3600,
      );

      const result = await saveShareImage(image, { isWeChat });
      if (result === "cancelled") {
        setDownloadState({ imageId: image.id, status: "idle" });
        setNotice(null);
        return;
      }

      const success = result !== "opened";
      setDownloadState({ imageId: image.id, status: success ? "success" : "error" });
      showNotice({
        kind: success ? "success" : "error",
        text: downloadResultText(result),
      });
      window.setTimeout(() => {
        setDownloadState((current) =>
          current?.imageId === image.id ? null : current,
        );
      }, 1700);
    },
    [downloadState, isWeChat, showNotice],
  );

  const handleShareLink = useCallback(async () => {
    if (typeof window === "undefined") return;
    const url = window.location.href;
    try {
      if (typeof navigator.share === "function") {
        await navigator.share({
          title: "Lumen 图片分享",
          text: `${images.length} 张 Lumen 图片`,
          url,
        });
        showNotice({ kind: "success", text: "已打开分享菜单" });
      } else {
        await writeClipboardText(url);
        setLinkShared(true);
        showNotice({ kind: "success", text: "分享链接已复制" });
        window.setTimeout(() => setLinkShared(false), 1600);
      }
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      try {
        await writeClipboardText(url);
        setLinkShared(true);
        showNotice({ kind: "success", text: "分享链接已复制" });
        window.setTimeout(() => setLinkShared(false), 1600);
      } catch {
        showNotice({ kind: "error", text: "复制失败" });
      }
    }
  }, [images.length, showNotice]);

  return (
    <div className="mx-auto flex w-full max-w-[min(94vw,1320px)] flex-col items-center gap-5 pb-4 md:gap-7">
      <section className="grid w-full gap-3 md:grid-cols-[minmax(0,1fr)_auto] md:items-end">
        <div className="min-w-0 space-y-2">
          <p className="inline-flex items-center gap-2 rounded-full border border-white/10 bg-white/[0.04] px-3 py-1 text-xs text-neutral-400">
            <Sparkles className="h-3.5 w-3.5 text-[var(--color-lumen-amber)]" />
            Lumen Share
          </p>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs font-mono tabular-nums text-neutral-500">
            <span className="inline-flex items-center gap-1.5">
              <Images className="h-3.5 w-3.5" />
              {images.length} 张图片
            </span>
            <span className="h-1 w-1 rounded-full bg-neutral-700" />
            <span>{shareSizeLabel(images)}</span>
            <span className="h-1 w-1 rounded-full bg-neutral-700" />
            <span>{createdLabel}</span>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2 md:justify-end">
          {expiresLabel && (
            <p className="inline-flex h-10 items-center gap-1.5 rounded-lg border border-white/10 bg-white/[0.04] px-3 text-xs text-neutral-400">
              <Clock className="h-3.5 w-3.5" />
              <span>过期</span>
              <span className="font-mono tabular-nums text-neutral-300">
                {expiresLabel}
              </span>
            </p>
          )}
          <button
            type="button"
            onClick={() => {
              void handleShareLink();
            }}
            className="inline-flex h-10 items-center justify-center gap-1.5 rounded-lg border border-white/10 bg-white/[0.04] px-3 text-xs text-neutral-300 transition-colors hover:border-white/20 hover:bg-white/[0.08] hover:text-white active:scale-[0.98]"
          >
            {linkShared ? (
              <Check className="h-3.5 w-3.5 text-[var(--color-lumen-amber)]" />
            ) : (
              <Share2 className="h-3.5 w-3.5" />
            )}
            分享链接
          </button>
        </div>

        {isWeChat && (
          <div className="rounded-lg border border-[var(--color-lumen-amber)]/20 bg-[var(--color-lumen-amber)]/10 px-3 py-2 text-xs leading-relaxed text-[var(--fg-1)] md:col-span-2">
            微信内保存：打开大图后长按图片；需要最高分辨率时点「原图」。
          </div>
        )}
      </section>

      {images.length === 1 ? (
        <div className="flex w-full justify-center">
          <ShareImageTile
            image={images[0]}
            index={0}
            single
            priority
            downloading={
              downloadState?.imageId === images[0].id
              && downloadState.status === "downloading"
            }
            onOpen={openAt}
            onDownload={handleDownload}
          />
        </div>
      ) : (
        <div className="w-full columns-2 gap-2 sm:columns-3 md:columns-4 md:gap-3 xl:columns-5">
          {images.map((image, index) => (
            <ShareImageTile
              key={image.id}
              image={image}
              index={index}
              priority={index < 6}
              downloading={
                downloadState?.imageId === image.id
                && downloadState.status === "downloading"
              }
              onOpen={openAt}
              onDownload={handleDownload}
            />
          ))}
        </div>
      )}

      <div className="grid w-full max-w-4xl gap-3 md:grid-cols-[minmax(0,1fr)_auto] md:items-start">
        {data.show_prompt && prompts.length > 0 ? (
          <details className="group overflow-hidden rounded-lg border border-white/10 bg-white/[0.04] backdrop-blur-sm transition-colors hover:border-white/20">
            <summary className="flex cursor-pointer list-none items-center justify-between gap-4 px-4 py-3 text-xs uppercase text-[var(--fg-1)] transition-colors hover:text-neutral-100">
              <span className="inline-flex items-center gap-2">
                <Sparkles className="h-3.5 w-3.5 text-[var(--color-lumen-amber)]" />
                Prompt
              </span>
              <ArrowRight className="h-3.5 w-3.5 text-neutral-500 transition-transform group-open:rotate-90" />
            </summary>
            <div className="space-y-3 border-t border-white/8 px-4 pb-4 pt-3 text-sm leading-relaxed text-neutral-200">
              {prompts.map((prompt, index) => (
                <p
                  key={`${index}-${prompt.slice(0, 24)}`}
                  className="whitespace-pre-wrap break-words"
                >
                  {prompt}
                </p>
              ))}
            </div>
          </details>
        ) : (
          <div className="hidden md:block" aria-hidden />
        )}

        <Link
          href="/"
          className="inline-flex h-11 w-full items-center justify-center gap-1.5 rounded-lg bg-[var(--color-lumen-amber)] px-4 text-sm font-medium text-black transition-all hover:brightness-110 active:scale-[0.97] md:w-auto"
        >
          开始生成
          <ArrowRight className="h-3.5 w-3.5" />
        </Link>
      </div>

      {activeImage && activeIndex !== null && (
        <ShareLightbox
          images={images}
          index={activeIndex}
          isWeChat={isWeChat}
          downloading={
            downloadState?.imageId === activeImage.id
            && downloadState.status === "downloading"
          }
          onClose={close}
          onPrev={goPrev}
          onNext={goNext}
          onSelect={setActiveIndex}
          onDownload={handleDownload}
        />
      )}

      <ShareNotice notice={notice} />
    </div>
  );
}

function ShareImageTile({
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
  const frameStyle = single ? singleImageFrameStyle(image) : imageFrameStyle(image);

  return (
    <div
      className={cn(
        "share-tile-shell group relative overflow-hidden rounded-lg border border-white/10 bg-black text-left shadow-[0_18px_50px_-28px_rgba(0,0,0,0.85)] transition-[border-color,transform,box-shadow] duration-200 hover:border-white/20 hover:shadow-[0_22px_60px_-34px_rgba(242,169,58,0.45)]",
        single ? "max-w-full" : "mb-2 w-full break-inside-avoid md:mb-3",
      )}
    >
      <button
        type="button"
        onClick={() => onOpen(index)}
        className="relative block w-full overflow-hidden bg-neutral-950 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-lumen-amber)]/70"
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
        className="absolute right-2 top-2 z-10 inline-flex h-9 w-9 items-center justify-center rounded-full border border-white/15 bg-black/55 text-white/90 backdrop-blur transition-all hover:bg-black/70 active:scale-[0.94] disabled:opacity-60 sm:opacity-0 sm:group-hover:opacity-100"
        aria-label="下载原图"
      >
        {downloading ? (
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
        ) : (
          <Download className="h-4 w-4" aria-hidden />
        )}
      </button>

      <span className="pointer-events-none absolute left-2 top-2 inline-flex h-8 w-8 items-center justify-center rounded-full border border-white/15 bg-black/45 text-white/80 opacity-100 backdrop-blur sm:opacity-0 sm:group-hover:opacity-100">
        <Maximize2 className="h-3.5 w-3.5" aria-hidden />
      </span>
    </div>
  );
}

function ShareLightbox({
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
      if (event.key === "Escape") onClose();
      if (!multiple) return;
      if (event.key === "ArrowLeft") onPrev();
      if (event.key === "ArrowRight") onNext();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [multiple, onClose, onNext, onPrev]);

  if (!image) return null;

  const onPointerDown = (event: PointerEvent<HTMLDivElement>) => {
    if (!multiple || (event.pointerType === "mouse" && event.button !== 0)) return;
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
      className="fixed inset-0 z-50 flex bg-black text-white share-dialog-in"
      role="dialog"
      aria-modal="true"
    >
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_50%_0%,rgba(242,169,58,0.10),transparent_28rem),linear-gradient(180deg,rgba(255,255,255,0.05),transparent_35%)]" />

      <div className="pointer-events-none absolute inset-x-0 top-0 z-20 border-b border-white/10 bg-black/45 px-3 pb-3 pt-[calc(env(safe-area-inset-top,0px)+0.75rem)] backdrop-blur-xl mobile-perf-surface">
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
              className="hidden h-10 items-center justify-center gap-1.5 rounded-full border border-white/15 bg-white/10 px-3 text-xs text-white backdrop-blur transition-colors hover:bg-white/15 disabled:opacity-55 sm:inline-flex"
            >
              {downloading ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Download className="h-4 w-4" />
              )}
              {downloading ? "准备中" : "下载"}
            </button>
            <button
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
        className="relative z-10 flex min-h-0 w-full flex-1 touch-pan-y select-none items-center justify-center px-3 pb-[calc(env(safe-area-inset-bottom,0px)+9.8rem)] pt-[calc(env(safe-area-inset-top,0px)+5rem)] sm:px-16 sm:pb-28 sm:pt-24"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={() => {
          gestureRef.current = null;
          setDragX(0);
        }}
      >
        <div
          className="relative max-w-full transition-transform duration-200 ease-out"
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
        <ShareFilmstrip images={images} activeIndex={index} onSelect={onSelect} />
      )}

      <div className="absolute inset-x-0 bottom-0 z-20 border-t border-white/10 bg-black/[0.72] px-3 pb-[calc(env(safe-area-inset-bottom,0px)+0.75rem)] pt-3 backdrop-blur-xl mobile-perf-surface">
        <div className="mx-auto flex w-full max-w-4xl items-center gap-2">
          <button
            type="button"
            onClick={() => onDownload(image)}
            disabled={downloading}
            className="inline-flex h-11 flex-1 items-center justify-center gap-2 rounded-lg bg-[var(--color-lumen-amber)] px-4 text-sm font-medium text-black transition-all hover:brightness-110 active:scale-[0.98] disabled:opacity-70"
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
            className="inline-flex h-11 items-center justify-center gap-1.5 rounded-lg border border-white/15 bg-white/10 px-3 text-sm text-white transition-colors hover:bg-white/15"
          >
            <ExternalLink className="h-4 w-4" />
            原图
          </a>
        </div>
        <div className="mx-auto mt-2 flex w-full max-w-4xl flex-wrap items-center justify-between gap-x-3 gap-y-1 text-[11px] text-white/[0.62]">
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
    <div className="absolute inset-x-0 bottom-[calc(env(safe-area-inset-bottom,0px)+5.8rem)] z-20">
      <div className="mx-auto flex max-w-4xl gap-2 overflow-x-auto px-3 py-2 no-scrollbar">
        {images.map((image, index) => (
          <button
            key={image.id}
            type="button"
            onClick={() => onSelect(index)}
            className={cn(
              "relative h-14 w-14 flex-none overflow-hidden rounded-md border bg-white/5 transition-all",
              index === activeIndex
                ? "border-[var(--color-lumen-amber)] opacity-100"
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
  const candidates = useMemo(() => candidateUrls(image, surface), [image, surface]);
  const [attempt, setAttempt] = useState(0);
  const [loaded, setLoaded] = useState(false);
  const src = candidates[attempt];
  const failed = !src;
  const lowQualitySrc = lowQualityPlaceholderUrl(image, surface);

  return (
    <>
      {lowQualitySrc && !loaded && !failed && lowQualitySrc !== src && (
        <img
          src={lowQualitySrc}
          alt=""
          aria-hidden
          loading={loading}
          decoding="async"
          className={cn(
            "pointer-events-none absolute inset-0 h-full w-full scale-[1.025] opacity-60 blur-md",
            surface === "lightbox" || surface === "single"
              ? "object-contain"
              : "object-cover",
          )}
        />
      )}
      {src && (
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
      )}
      {!loaded && !failed && (
        <span
          className={cn(
            "pointer-events-none absolute inset-0 flex items-center justify-center bg-[linear-gradient(110deg,rgba(255,255,255,0.05),rgba(255,255,255,0.12),rgba(255,255,255,0.05))] bg-[length:220%_100%] animate-lumen-shimmer",
            surface === "lightbox" ? "bg-black/[0.18]" : "bg-white/[0.035]",
          )}
        >
          {surface === "lightbox" && (
            <Loader2 className="h-5 w-5 animate-spin text-white/55" aria-hidden />
          )}
        </span>
      )}
      {failed && (
        <span className="pointer-events-none absolute inset-0 flex min-h-32 flex-col items-center justify-center gap-2 bg-neutral-950 px-4 text-center text-xs text-neutral-400">
          <ImageOff className="h-6 w-6 text-neutral-500" aria-hidden />
          <span>图片暂时不可用</span>
        </span>
      )}
    </>
  );
}

function ShareNotice({ notice }: { notice: Notice | null }) {
  if (!notice) return null;
  return (
    <div className="pointer-events-none fixed inset-x-0 bottom-4 z-[70] flex justify-center px-4 pb-[env(safe-area-inset-bottom,0px)]">
      <div
        className={cn(
          "rounded-full border px-4 py-2 text-sm shadow-[0_18px_44px_-18px_rgba(0,0,0,0.8)] backdrop-blur-xl",
          notice.kind === "success" &&
            "border-[var(--color-lumen-amber)]/25 bg-[var(--color-lumen-amber)]/18 text-[var(--fg-0)]",
          notice.kind === "error" &&
            "border-red-400/[0.25] bg-red-500/[0.16] text-red-100",
          notice.kind === "info" &&
            "border-white/[0.12] bg-black/[0.68] text-white/[0.86]",
        )}
      >
        {notice.text}
      </div>
    </div>
  );
}

function normalizeShareImages(data: PublicShareOut): PublicShareImageOut[] {
  if (Array.isArray(data.images) && data.images.length > 0) {
    return data.images.map(normalizeShareImage);
  }
  return [
    normalizeShareImage({
      id: data.token,
      image_url: data.image_url,
      width: data.width,
      height: data.height,
      mime: data.mime,
      prompt: data.prompt,
    }),
  ];
}

function normalizeShareImage(image: PublicShareImageOut): PublicShareImageOut {
  return {
    ...image,
    width: Number.isFinite(image.width) ? Math.max(1, image.width) : 1,
    height: Number.isFinite(image.height) ? Math.max(1, image.height) : 1,
    mime: image.mime || "image/png",
    prompt: image.prompt ?? null,
  };
}

function candidateUrls(
  image: PublicShareImageOut,
  surface: ShareImageSurface,
): string[] {
  const bySurface: Record<ShareImageSurface, Array<string | null | undefined>> = {
    grid: [image.preview_url, image.thumb_url, image.display_url, image.image_url],
    single: [image.display_url, image.preview_url, image.image_url, image.thumb_url],
    lightbox: [image.display_url, image.preview_url, image.image_url, image.thumb_url],
    filmstrip: [image.thumb_url, image.preview_url, image.display_url, image.image_url],
  };
  return uniqueUrls(bySurface[surface]);
}

function lowQualityPlaceholderUrl(
  image: PublicShareImageOut,
  surface: ShareImageSurface,
): string | null {
  if (surface === "filmstrip") return null;
  return image.thumb_url || image.preview_url || null;
}

function srcSetForImage(image: PublicShareImageOut): string | undefined {
  const entries: string[] = [];
  if (image.thumb_url) entries.push(`${image.thumb_url} 256w`);
  if (image.preview_url) entries.push(`${image.preview_url} 1024w`);
  if (image.display_url) entries.push(`${image.display_url} 2048w`);
  if (image.image_url && image.width > 2048) entries.push(`${image.image_url} ${image.width}w`);
  return entries.length > 1 ? entries.join(", ") : undefined;
}

function sizesForSurface(surface: ShareImageSurface): string {
  if (surface === "grid") {
    return "(min-width: 1280px) 19vw, (min-width: 768px) 24vw, (min-width: 640px) 32vw, 48vw";
  }
  if (surface === "filmstrip") return "56px";
  return "100vw";
}

function uniqueUrls(urls: Array<string | null | undefined>): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const url of urls) {
    const clean = url?.trim();
    if (!clean || seen.has(clean)) continue;
    seen.add(clean);
    out.push(clean);
  }
  return out;
}

function preloadShareImage(
  image: PublicShareImageOut | undefined,
  surface: ShareImageSurface,
) {
  if (!image || typeof window === "undefined") return;
  const src = candidateUrls(image, surface)[0];
  if (!src) return;
  const probe = new window.Image();
  probe.decoding = "async";
  probe.src = src;
  if (typeof probe.decode === "function") {
    void probe.decode().catch(() => undefined);
  }
}

function scheduleIdle(callback: () => void): () => void {
  if (typeof window === "undefined") return () => undefined;
  if ("requestIdleCallback" in window) {
    const id = window.requestIdleCallback(callback, { timeout: 900 });
    return () => window.cancelIdleCallback(id);
  }
  const id = globalThis.setTimeout(callback, 120);
  return () => globalThis.clearTimeout(id);
}

function imageFrameStyle(image: PublicShareImageOut): React.CSSProperties {
  return {
    aspectRatio: `${image.width} / ${image.height}`,
  };
}

function singleImageFrameStyle(image: PublicShareImageOut): React.CSSProperties {
  return {
    aspectRatio: `${image.width} / ${image.height}`,
    width: `min(94vw, 1240px, ${image.width}px)`,
    maxHeight: "78dvh",
  };
}

function lightboxImageFrameStyle(image: PublicShareImageOut): React.CSSProperties {
  return {
    aspectRatio: `${image.width} / ${image.height}`,
    width: `min(96vw, ${image.width}px)`,
    maxHeight: "calc(100dvh - 11rem)",
  };
}

function sharePrompts(images: PublicShareImageOut[]): string[] {
  return uniqueUrls(images.map((image) => image.prompt));
}

function shareSizeLabel(images: PublicShareImageOut[]): string {
  if (images.length === 1) {
    const image = images[0];
    return `${image.width} x ${image.height} · ${image.mime}`;
  }
  const first = images[0];
  const sameSize = images.every(
    (image) => image.width === first.width && image.height === first.height,
  );
  return sameSize ? `${first.width} x ${first.height}` : "多尺寸";
}

function shareImageAlt(image: PublicShareImageOut): string {
  const prompt = image.prompt?.trim();
  return prompt ? prompt.slice(0, 120) : "Lumen 分享图片";
}

async function saveShareImage(
  image: PublicShareImageOut,
  options: { isWeChat: boolean },
): Promise<DownloadResult> {
  if (typeof window === "undefined") return "cancelled";
  if (options.isWeChat) {
    openImageUrl(image.image_url);
    return "wechat";
  }

  try {
    const blob = await fetchImageBlob(image.image_url);
    const filename = downloadFilename(image, blob.type);

    if (isIosLike() && typeof File !== "undefined") {
      const file = new File([blob], filename, {
        type: blob.type || image.mime || "image/png",
      });
      if (canShareFile(file)) {
        try {
          await navigator.share({
            files: [file],
            title: filename,
          });
          return "shared";
        } catch (error) {
          if (error instanceof DOMException && error.name === "AbortError") {
            return "cancelled";
          }
        }
      }
    }

    const url = URL.createObjectURL(blob);
    triggerAnchorDownload(url, filename);
    window.setTimeout(() => URL.revokeObjectURL(url), 1400);
    return "downloaded";
  } catch {
    openImageUrl(image.image_url);
    return "opened";
  }
}

async function fetchImageBlob(src: string): Promise<Blob> {
  const response = await fetch(src, { credentials: "same-origin" });
  if (!response.ok) {
    throw new Error(`Image download failed: ${response.status}`);
  }
  return response.blob();
}

function triggerAnchorDownload(href: string, filename: string) {
  const anchor = document.createElement("a");
  anchor.href = href;
  anchor.download = filename;
  anchor.rel = "noopener";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
}

function openImageUrl(url: string) {
  const opened = window.open(url, "_blank", "noopener,noreferrer");
  if (!opened) window.location.href = url;
}

function downloadFilename(image: PublicShareImageOut, mime?: string): string {
  return `lumen-${image.id}.${extensionForMime(mime || image.mime)}`;
}

function downloadResultText(result: DownloadResult): string {
  switch (result) {
    case "downloaded":
      return "已开始下载原图";
    case "shared":
      return "已发送到系统分享菜单";
    case "wechat":
      return "已打开原图，可长按保存";
    case "opened":
      return "下载受限，已尝试打开原图";
    case "cancelled":
      return "";
  }
}

function isIosLike(): boolean {
  if (typeof navigator === "undefined") return false;
  return /iPad|iPhone|iPod/.test(navigator.userAgent)
    || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
}

function canShareFile(file: File): boolean {
  return typeof navigator !== "undefined"
    && typeof navigator.share === "function"
    && typeof navigator.canShare === "function"
    && navigator.canShare({ files: [file] });
}

async function writeClipboardText(text: string): Promise<void> {
  if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }

  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.left = "-9999px";
  textarea.style.top = "0";
  document.body.appendChild(textarea);
  textarea.select();
  try {
    document.execCommand("copy");
  } finally {
    textarea.remove();
  }
}

function isWeChatBrowser(): boolean {
  if (typeof navigator === "undefined") return false;
  return /MicroMessenger/i.test(navigator.userAgent);
}

function extensionForMime(mime: string): string {
  if (mime.includes("jpeg")) return "jpg";
  if (mime.includes("webp")) return "webp";
  if (mime.includes("gif")) return "gif";
  return "png";
}

function safeDistanceToNow(iso: string): string {
  try {
    return formatDistanceToNow(new Date(iso), {
      addSuffix: true,
      locale: zhCN,
    });
  } catch {
    return iso;
  }
}

function safeFormat(iso: string, pattern: string): string {
  try {
    return format(new Date(iso), pattern, { locale: zhCN });
  } catch {
    return iso;
  }
}
