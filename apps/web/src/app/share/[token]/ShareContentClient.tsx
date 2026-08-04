"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import {
  ArrowRight,
  Check,
  Clock,
  Images,
  Share2,
  Sparkles,
} from "lucide-react";

import { cn } from "@/lib/utils";
import type { PublicShareImageOut, PublicShareOut } from "@/lib/types";
import {
  ShareImageTile,
  ShareLightbox,
} from "./ShareContentClientGallery";
import {
  expirationLabel,
  normalizeShareImages,
  preloadShareImage,
  safeDistanceToNow,
  scheduleIdle,
  sharePrompts,
  shareSizeLabel,
} from "./share-content-utils";
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

function isDownloadInProgress(
  downloadState: DownloadState | null,
  imageId: string,
): boolean {
  return (
    downloadState?.imageId === imageId && downloadState.status === "downloading"
  );
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
  // 一次性 setTimeout（download tail / linkShared reset）的句柄集合；unmount
  // 统一清理，避免 React 19 strict mode 在 setState-on-unmounted 时 warn。
  const transientTimersRef = useRef<Set<number>>(new Set());
  const createdLabel = safeDistanceToNow(data.created_at);
  const expiresLabel = expirationLabel(data.expires_at);
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
    const transientTimers = transientTimersRef.current;
    return () => {
      if (noticeTimerRef.current !== null) {
        window.clearTimeout(noticeTimerRef.current);
      }
      for (const id of transientTimers) {
        window.clearTimeout(id);
      }
      transientTimers.clear();
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
      if (isDownloadInProgress(downloadState, image.id)) {
        return;
      }

      setDownloadState({ imageId: image.id, status: "downloading" });
      showNotice(
        {
          kind: "info",
          text: isWeChat ? "打开原图中" : "准备原图中",
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
      const timerId = window.setTimeout(() => {
        transientTimersRef.current.delete(timerId);
        setDownloadState((current) =>
          current?.imageId === image.id ? null : current,
        );
      }, 1700);
      transientTimersRef.current.add(timerId);
    },
    [downloadState, isWeChat, showNotice],
  );

  const handleShareLink = useCallback(async () => {
    if (typeof window === "undefined") return;
    const url = window.location.href;
    const flashCopied = () => {
      const timerId = window.setTimeout(() => {
        transientTimersRef.current.delete(timerId);
        setLinkShared(false);
      }, 1600);
      transientTimersRef.current.add(timerId);
    };
    try {
      if (typeof navigator.share === "function") {
        await navigator.share({
          title: "图片分享",
          text: `${images.length} 张图片`,
          url,
        });
        showNotice({ kind: "success", text: "已打开分享菜单" });
      } else {
        await writeClipboardText(url);
        setLinkShared(true);
        showNotice({ kind: "success", text: "分享链接已复制" });
        flashCopied();
      }
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      try {
        await writeClipboardText(url);
        setLinkShared(true);
        showNotice({ kind: "success", text: "分享链接已复制" });
        flashCopied();
      } catch {
        showNotice({ kind: "error", text: "复制失败" });
      }
    }
  }, [images.length, showNotice]);

  return (
    <div className="mx-auto flex w-full flex-col items-center gap-5 pb-[calc(env(safe-area-inset-bottom,0px)+1rem)] md:gap-7">
      <section className="page-header w-full">
        <div className="page-header-copy">
          <p className="type-caption">公开画廊</p>
          <h1 className="type-page-title">图片分享</h1>
          <div className="type-caption flex flex-wrap items-center gap-x-2.5 gap-y-1 tabular-nums">
            <span className="inline-flex items-center gap-1.5">
              <Images className="h-3.5 w-3.5" />
              {images.length} 张图片
            </span>
            <span className="h-1 w-1 rounded-full bg-[var(--fg-3)]" />
            <span>{shareSizeLabel(images)}</span>
            <span className="h-1 w-1 rounded-full bg-[var(--fg-3)]" />
            <span>{createdLabel}</span>
          </div>
        </div>

        <div className="page-header-actions">
          {expiresLabel && (
            <p className="type-caption inline-flex min-h-10 items-center gap-1.5 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-1)]/72 px-3 text-[var(--fg-1)]">
              <Clock className="h-3.5 w-3.5" />
              <span>过期</span>
              <span className="tabular-nums text-[var(--fg-0)]">
                {expiresLabel}
              </span>
            </p>
          )}
          <button
            type="button"
            onClick={() => {
              void handleShareLink();
            }}
            className="type-control inline-flex min-h-10 items-center justify-center gap-1.5 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-3 text-[var(--fg-1)] transition-[transform,background-color,border-color] hover:border-[var(--border-strong)] hover:bg-[var(--bg-3)] hover:text-[var(--fg-0)] active:scale-[var(--press-scale-soft)]"
          >
            {linkShared ? (
              <Check className="h-3.5 w-3.5 text-[var(--success)]" />
            ) : (
              <Share2 className="h-3.5 w-3.5" />
            )}
            分享链接
          </button>
        </div>

        {isWeChat && (
          <div className="type-caption border-l-2 border-info-border bg-info-soft px-3 py-2 text-[var(--info-fg)] md:col-span-2">
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
        <div className="w-full columns-2 gap-1.5 min-[390px]:gap-2 sm:columns-3 md:columns-4 md:gap-3 xl:columns-5">
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
          <details className="group overflow-hidden border-y border-[var(--border-subtle)] bg-transparent transition-colors hover:border-[var(--border-strong)]">
            <summary className="type-caption flex min-h-11 cursor-pointer list-none items-center justify-between gap-4 px-4 py-3 text-[var(--fg-1)] transition-colors hover:text-[var(--fg-0)]">
              <span className="inline-flex items-center gap-2">
                <Sparkles className="h-3.5 w-3.5 text-[var(--info)]" />
                提示词
              </span>
              <ArrowRight className="h-3.5 w-3.5 text-[var(--fg-2)] transition-transform group-open:rotate-90" />
            </summary>
            <div className="type-body-sm space-y-3 border-t border-[var(--border)] px-4 pb-4 pt-3 text-[var(--fg-0)]">
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
          className="type-control inline-flex h-11 w-full items-center justify-center gap-1.5 rounded-[var(--radius-control)] bg-[var(--accent)] px-4 text-[var(--accent-on)] shadow-[var(--shadow-1)] transition-[transform,background-color] hover:bg-[var(--accent-hover)] active:scale-[var(--press-scale-soft)] md:w-auto"
        >
          打开主页
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

function ShareNotice({ notice }: { notice: Notice | null }) {
  if (!notice) return null;
  return (
    <div className="pointer-events-none fixed inset-x-0 bottom-4 z-[var(--z-toast)] flex justify-center px-4 pb-[env(safe-area-inset-bottom,0px)]">
      <div
        className={cn(
          "type-body-sm rounded-full border px-4 py-2 shadow-[var(--shadow-3)] backdrop-blur-xl",
          notice.kind === "success" &&
            "border-success-border bg-success-soft text-[var(--success-fg)]",
          notice.kind === "error" &&
            "border-danger-border bg-danger-soft text-[var(--danger-fg)]",
          notice.kind === "info" &&
            "border-info-border bg-info-soft text-[var(--info-fg)]",
        )}
      >
        {notice.text}
      </div>
    </div>
  );
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
    throw new Error(`图片下载失败：${response.status}`);
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
