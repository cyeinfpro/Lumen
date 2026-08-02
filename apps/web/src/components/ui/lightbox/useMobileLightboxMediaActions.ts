"use client";

import {
  type RefObject,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { isPrivateIdentitySnapshotCurrent } from "@/lib/auth/privateIdentityEpoch";
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
  ownerUserId: string | null;
  identityEpoch: number;
  downloadAnchorRef: RefObject<HTMLAnchorElement | null>;
}

interface MobileDownloadOperation {
  current: LightboxItem;
  anchor: HTMLAnchorElement;
  operationIsCurrent: () => boolean;
  setDownloadStatus: (status: DownloadStatus) => void;
  setActionNotice: (notice: ActionNotice) => void;
  showNotice: (
    notice: NonNullable<ActionNotice>,
    operationIsCurrent: () => boolean,
  ) => void;
}

async function performMobileDownload(
  operation: MobileDownloadOperation,
): Promise<string | null> {
  const { current, anchor, operationIsCurrent } = operation;
  const blob = await fetchImageBlob(current.url);
  if (!operationIsCurrent()) return null;
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
  if (!operationIsCurrent()) return null;
  if (shareResult === "shared") {
    operation.setDownloadStatus("success");
    operation.showNotice({
      kind: "success",
      text: "已发送到分享菜单",
    }, operationIsCurrent);
    return null;
  }
  if (shareResult === "canceled") {
    operation.setDownloadStatus("idle");
    operation.setActionNotice(null);
    return null;
  }
  const objectUrl = URL.createObjectURL(blob);
  triggerAnchorDownload(anchor, objectUrl, filename);
  operation.setDownloadStatus("success");
  operation.showNotice(
    { kind: "success", text: "已开始下载" },
    operationIsCurrent,
  );
  return objectUrl;
}

function fallbackMobileDownload(operation: MobileDownloadOperation): void {
  if (!operation.operationIsCurrent()) return;
  const { current, anchor } = operation;
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
  operation.setDownloadStatus("error");
  operation.showNotice(
    {
      kind: "error",
      text: "下载失败，已尝试打开原图",
    },
    operation.operationIsCurrent,
  );
}

async function runMobileDownload(
  operation: MobileDownloadOperation,
  scheduleReset: () => void,
): Promise<void> {
  let objectUrl: string | null = null;
  try {
    objectUrl = await performMobileDownload(operation);
  } catch {
    fallbackMobileDownload(operation);
  } finally {
    if (objectUrl) {
      const urlToRevoke = objectUrl;
      window.setTimeout(() => URL.revokeObjectURL(urlToRevoke), 1000);
    }
    if (operation.operationIsCurrent()) scheduleReset();
  }
}

type CreateShareLink = (input: {
  imageId: string;
  show_prompt: boolean;
}) => Promise<{ url: string }>;

async function shareLinkWithNavigator(
  link: string,
): Promise<"shared" | "canceled" | "unavailable"> {
  if (
    typeof navigator === "undefined" ||
    typeof navigator.share !== "function"
  ) {
    return "unavailable";
  }
  try {
    await navigator.share({
      title: "Lumen image",
      text: "Lumen image",
      url: link,
    });
    return "shared";
  } catch (error) {
    return error instanceof DOMException && error.name === "AbortError"
      ? "canceled"
      : "unavailable";
  }
}

async function runMobileShare({
  imageId,
  createShare,
  operationIsCurrent,
  showNotice,
  setActionNotice,
}: {
  imageId: string;
  createShare: CreateShareLink;
  operationIsCurrent: () => boolean;
  showNotice: (
    notice: NonNullable<ActionNotice>,
    operationIsCurrent: () => boolean,
  ) => void;
  setActionNotice: (notice: ActionNotice) => void;
}): Promise<void> {
  setActionNotice({ kind: "info", text: "正在生成分享链接" });
  let link: string;
  try {
    link = (await createShare({ imageId, show_prompt: false })).url;
  } catch {
    if (operationIsCurrent()) {
      showNotice(
        { kind: "error", text: "分享链接生成失败" },
        operationIsCurrent,
      );
    }
    return;
  }
  if (!operationIsCurrent()) return;
  const nativeShare = await shareLinkWithNavigator(link);
  if (!operationIsCurrent()) return;
  if (nativeShare === "shared") {
    showNotice(
      { kind: "success", text: "已打开分享菜单" },
      operationIsCurrent,
    );
    return;
  }
  if (nativeShare === "canceled") return;
  try {
    await writeClipboardText(link);
    if (operationIsCurrent()) {
      showNotice(
        { kind: "success", text: "分享链接已复制" },
        operationIsCurrent,
      );
    }
  } catch {
    if (operationIsCurrent()) {
      showNotice(
        { kind: "error", text: "复制失败，请手动复制" },
        operationIsCurrent,
      );
    }
  }
}

export function useMobileLightboxMediaActions({
  current,
  ownerUserId,
  identityEpoch,
  downloadAnchorRef,
}: UseMobileLightboxMediaActionsOptions) {
  const createShareMutation = useCreateShareMutation();
  const [downloadStatus, setDownloadStatus] =
    useState<DownloadStatus>("idle");
  const [actionNotice, setActionNotice] =
    useState<ActionNotice>(null);
  const currentItemKey = current ? `${current.id}\n${current.url}` : "";
  const feedbackTimerRef = useRef<number | null>(null);
  const downloadResetTimerRef = useRef<number | null>(null);
  const activeItemKeyRef = useRef(currentItemKey);
  const downloadSeqRef = useRef(0);
  const shareSeqRef = useRef(0);
  const copyPromptSeqRef = useRef(0);
  const feedbackSeqRef = useRef(0);
  const identityIsCurrent = useCallback(
    () =>
      isPrivateIdentitySnapshotCurrent({
        userId: ownerUserId,
        epoch: identityEpoch,
      }),
    [identityEpoch, ownerUserId],
  );

  const clearFeedbackTimer = useCallback(() => {
    feedbackSeqRef.current += 1;
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
    (
      notice: NonNullable<ActionNotice>,
      operationIsCurrent: () => boolean = () => true,
    ) => {
      clearFeedbackTimer();
      const feedbackSeq = feedbackSeqRef.current;
      setActionNotice(notice);
      feedbackTimerRef.current = window.setTimeout(() => {
        if (
          feedbackSeqRef.current === feedbackSeq &&
          operationIsCurrent()
        ) {
          setActionNotice(null);
        }
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

  useLayoutEffect(() => {
    activeItemKeyRef.current = currentItemKey;
    downloadSeqRef.current += 1;
    shareSeqRef.current += 1;
    copyPromptSeqRef.current += 1;
  }, [currentItemKey]);

  useEffect(() => {
    return resetMediaActions;
  }, [resetMediaActions]);

  const handleDownload = useCallback(() => {
    const anchor = downloadAnchorRef.current;
    if (!current || !anchor || !identityIsCurrent()) return;

    const operationKey = currentItemKey;
    const operationSeq = downloadSeqRef.current + 1;
    downloadSeqRef.current = operationSeq;
    const operationIsCurrent = () =>
      identityIsCurrent() &&
      activeItemKeyRef.current === operationKey &&
      downloadSeqRef.current === operationSeq;
    clearDownloadResetTimer();
    setDownloadStatus("downloading");
    setActionNotice({ kind: "info", text: "正在下载原图" });
    void runMobileDownload(
      {
        current,
        anchor,
        operationIsCurrent,
        setDownloadStatus,
        setActionNotice,
        showNotice,
      },
      () => {
        downloadResetTimerRef.current = window.setTimeout(() => {
          if (operationIsCurrent()) setDownloadStatus("idle");
          downloadResetTimerRef.current = null;
        }, 1800);
      },
    );
  }, [
    clearDownloadResetTimer,
    current,
    currentItemKey,
    downloadAnchorRef,
    identityIsCurrent,
    showNotice,
  ]);

  const handleCopyPrompt = useCallback(() => {
    if (!current?.prompt || !identityIsCurrent()) return;
    const operationKey = currentItemKey;
    const operationSeq = copyPromptSeqRef.current + 1;
    copyPromptSeqRef.current = operationSeq;
    const operationIsCurrent = () =>
      identityIsCurrent() &&
      activeItemKeyRef.current === operationKey &&
      copyPromptSeqRef.current === operationSeq;
    void writeClipboardText(current.prompt)
      .then(() => {
        if (operationIsCurrent()) {
          showNotice(
            { kind: "success", text: "Prompt 已复制" },
            operationIsCurrent,
          );
        }
      })
      .catch(() => {
        if (operationIsCurrent()) {
          showNotice(
            { kind: "error", text: "复制失败" },
            operationIsCurrent,
          );
        }
      });
  }, [current, currentItemKey, identityIsCurrent, showNotice]);

  const handleShare = useCallback(() => {
    if (
      !current ||
      typeof window === "undefined" ||
      !identityIsCurrent()
    ) {
      return;
    }
    if (createShareMutation.isPending) {
      showNotice({ kind: "info", text: "正在生成分享链接" });
      return;
    }
    const operationKey = currentItemKey;
    const operationSeq = shareSeqRef.current + 1;
    shareSeqRef.current = operationSeq;
    const operationIsCurrent = () =>
      identityIsCurrent() &&
      activeItemKeyRef.current === operationKey &&
      shareSeqRef.current === operationSeq;
    void runMobileShare({
      imageId: current.id,
      createShare: createShareMutation.mutateAsync,
      operationIsCurrent,
      showNotice,
      setActionNotice,
    });
  }, [
    createShareMutation,
    current,
    currentItemKey,
    identityIsCurrent,
    showNotice,
  ]);

  return {
    downloadStatus,
    actionNotice,
    resetMediaActions,
    handleDownload,
    handleCopyPrompt,
    handleShare,
  };
}
