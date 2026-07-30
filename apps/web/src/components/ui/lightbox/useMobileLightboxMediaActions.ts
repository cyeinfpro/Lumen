"use client";

import {
  type RefObject,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { copyTextToClipboard } from "@/lib/clipboard";
import { useCreateShareMutation } from "@/lib/queries";
import type {
  ActionNotice,
  DownloadStatus,
} from "./MobileLightboxView";
import type { LightboxItem } from "./types";

function extensionFromMime(
  mime: string | null | undefined,
): string | null {
  if (!mime) return null;
  const normalized = mime.split(";")[0]?.trim().toLowerCase();
  if (!normalized?.startsWith("image/")) return null;
  const extension = normalized.slice("image/".length);
  if (!extension) return null;
  if (extension === "jpeg") return "jpg";
  if (extension === "svg+xml") return "svg";
  return extension;
}

function extensionFromSrc(src: string): string | null {
  if (src.startsWith("data:")) {
    const mimeMatch = src.match(/^data:([^;]+);/);
    return extensionFromMime(mimeMatch?.[1]);
  }
  try {
    const pathname = new URL(src, window.location.href).pathname;
    const match = pathname.match(/\.([a-z0-9]+)$/i);
    return match?.[1]?.toLowerCase() ?? null;
  } catch {
    return null;
  }
}

function downloadFilename(
  id: string,
  src: string,
  mime?: string,
  preferred?: string,
): string {
  if (preferred?.trim()) return preferred.trim();
  const extension =
    extensionFromMime(mime) ?? extensionFromSrc(src) ?? "png";
  return `lumen-${id}.${extension}`;
}

async function fetchImageBlob(src: string): Promise<Blob> {
  const response = src.startsWith("data:")
    ? await fetch(src)
    : await fetch(src, { credentials: "include" });
  if (!response.ok) {
    throw new Error(`Image download failed: ${response.status}`);
  }
  return response.blob();
}

function isIosLike(): boolean {
  if (typeof navigator === "undefined") return false;
  return (
    /iPad|iPhone|iPod/.test(navigator.userAgent) ||
    (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1)
  );
}

function canShareFile(file: File): boolean {
  return (
    typeof navigator !== "undefined" &&
    typeof navigator.share === "function" &&
    typeof navigator.canShare === "function" &&
    navigator.canShare({ files: [file] })
  );
}

async function shareDownloadedFile(
  blob: Blob,
  filename: string,
  fallbackMime: string | undefined,
): Promise<"shared" | "canceled" | "unavailable"> {
  if (!isIosLike() || typeof File === "undefined") return "unavailable";
  const file = new File([blob], filename, {
    type: blob.type || fallbackMime || "image/png",
  });
  if (!canShareFile(file)) return "unavailable";
  try {
    await navigator.share({
      files: [file],
      title: filename,
    });
    return "shared";
  } catch (error) {
    return error instanceof DOMException && error.name === "AbortError"
      ? "canceled"
      : "unavailable";
  }
}

function triggerAnchorDownload(
  anchor: HTMLAnchorElement,
  href: string,
  filename: string,
) {
  anchor.href = href;
  anchor.download = filename;
  anchor.removeAttribute("target");
  anchor.removeAttribute("rel");
  anchor.click();
}

async function writeClipboardText(text: string): Promise<void> {
  await copyTextToClipboard(text);
}

interface UseMobileLightboxMediaActionsOptions {
  current: LightboxItem | null;
  downloadAnchorRef: RefObject<HTMLAnchorElement | null>;
}

export function useMobileLightboxMediaActions({
  current,
  downloadAnchorRef,
}: UseMobileLightboxMediaActionsOptions) {
  const createShareMutation = useCreateShareMutation();
  const [downloadStatus, setDownloadStatus] =
    useState<DownloadStatus>("idle");
  const [actionNotice, setActionNotice] =
    useState<ActionNotice>(null);
  const feedbackTimerRef = useRef<number | null>(null);
  const downloadResetTimerRef = useRef<number | null>(null);

  const clearFeedbackTimer = useCallback(() => {
    if (feedbackTimerRef.current !== null) {
      window.clearTimeout(feedbackTimerRef.current);
      feedbackTimerRef.current = null;
    }
  }, []);

  const clearDownloadResetTimer = useCallback(() => {
    if (downloadResetTimerRef.current !== null) {
      window.clearTimeout(downloadResetTimerRef.current);
      downloadResetTimerRef.current = null;
    }
  }, []);

  const showNotice = useCallback(
    (notice: NonNullable<ActionNotice>) => {
      clearFeedbackTimer();
      setActionNotice(notice);
      feedbackTimerRef.current = window.setTimeout(() => {
        setActionNotice(null);
        feedbackTimerRef.current = null;
      }, 1700);
    },
    [clearFeedbackTimer],
  );

  const resetMediaActions = useCallback(() => {
    clearFeedbackTimer();
    clearDownloadResetTimer();
    setDownloadStatus("idle");
    setActionNotice(null);
  }, [clearDownloadResetTimer, clearFeedbackTimer]);

  useEffect(() => {
    return resetMediaActions;
  }, [resetMediaActions]);

  const handleDownload = useCallback(() => {
    const anchor = downloadAnchorRef.current;
    if (!current || !anchor) return;

    void (async () => {
      let objectUrl: string | null = null;
      clearDownloadResetTimer();
      setDownloadStatus("downloading");
      setActionNotice({ kind: "info", text: "正在下载原图" });
      try {
        const blob = await fetchImageBlob(current.url);
        const fallbackMime =
          current.mime ?? current.mime_type ?? current.content_type;
        const filename = downloadFilename(
          current.id,
          current.url,
          blob.type || fallbackMime,
          current.filename ?? current.file_name,
        );

        const shareResult = await shareDownloadedFile(
          blob,
          filename,
          fallbackMime,
        );
        if (shareResult === "shared") {
          setDownloadStatus("success");
          showNotice({ kind: "success", text: "已发送到分享菜单" });
          return;
        }
        if (shareResult === "canceled") {
          setDownloadStatus("idle");
          setActionNotice(null);
          return;
        }

        objectUrl = URL.createObjectURL(blob);
        triggerAnchorDownload(anchor, objectUrl, filename);
        setDownloadStatus("success");
        showNotice({ kind: "success", text: "已开始下载" });
      } catch {
        triggerAnchorDownload(
          anchor,
          current.url,
          downloadFilename(
            current.id,
            current.url,
            current.mime ?? current.mime_type ?? current.content_type,
            current.filename ?? current.file_name,
          ),
        );
        setDownloadStatus("error");
        showNotice({
          kind: "error",
          text: "下载失败，已尝试打开原图",
        });
      } finally {
        if (objectUrl) {
          const urlToRevoke = objectUrl;
          window.setTimeout(() => URL.revokeObjectURL(urlToRevoke), 1000);
        }
        downloadResetTimerRef.current = window.setTimeout(() => {
          setDownloadStatus("idle");
          downloadResetTimerRef.current = null;
        }, 1800);
      }
    })();
  }, [clearDownloadResetTimer, current, downloadAnchorRef, showNotice]);

  const handleCopyPrompt = useCallback(() => {
    if (!current?.prompt) return;
    void writeClipboardText(current.prompt)
      .then(() => showNotice({ kind: "success", text: "Prompt 已复制" }))
      .catch(() => showNotice({ kind: "error", text: "复制失败" }));
  }, [current, showNotice]);

  const handleShare = useCallback(() => {
    if (!current || typeof window === "undefined") return;
    if (createShareMutation.isPending) {
      showNotice({ kind: "info", text: "正在生成分享链接" });
      return;
    }

    void (async () => {
      setActionNotice({ kind: "info", text: "正在生成分享链接" });
      let link: string;
      try {
        const share = await createShareMutation.mutateAsync({
          imageId: current.id,
          show_prompt: false,
        });
        link = share.url;
      } catch {
        showNotice({ kind: "error", text: "分享链接生成失败" });
        return;
      }

      if (
        typeof navigator !== "undefined" &&
        typeof navigator.share === "function"
      ) {
        try {
          await navigator.share({
            title: "Lumen image",
            text: "Lumen image",
            url: link,
          });
          showNotice({ kind: "success", text: "已打开分享菜单" });
          return;
        } catch (error) {
          if (
            error instanceof DOMException &&
            error.name === "AbortError"
          ) {
            return;
          }
        }
      }

      try {
        await writeClipboardText(link);
        showNotice({ kind: "success", text: "分享链接已复制" });
      } catch {
        showNotice({ kind: "error", text: "复制失败，请手动复制" });
      }
    })();
  }, [createShareMutation, current, showNotice]);

  return {
    downloadStatus,
    actionNotice,
    resetMediaActions,
    handleDownload,
    handleCopyPrompt,
    handleShare,
  };
}
