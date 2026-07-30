"use client";

import {
  type RefObject,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { copyTextToClipboard } from "@/lib/clipboard";
import { useCreateShareMutation } from "@/lib/queries";
import {
  downloadDesktopImage,
  shareDesktopImage,
} from "./desktopLightboxMediaActions";
import {
  desktopActionPresentation,
  type DesktopImageMeta,
  type DownloadStatus,
  type ShareStatus,
} from "./desktopLightboxModel";

interface UseDesktopLightboxMediaActionsOptions {
  open: boolean;
  imageId: string | null;
  imageSrc: string | null;
  imageStateKey: string;
  activeImageStateKeyRef: RefObject<string>;
  currentImageMeta: DesktopImageMeta | null;
  downloadAnchorRef: RefObject<HTMLAnchorElement | null>;
}

async function writeClipboardText(text: string): Promise<void> {
  await copyTextToClipboard(text);
}

export function useDesktopLightboxMediaActions({
  open,
  imageId,
  imageSrc,
  imageStateKey,
  activeImageStateKeyRef,
  currentImageMeta,
  downloadAnchorRef,
}: UseDesktopLightboxMediaActionsOptions) {
  const createShareMutation = useCreateShareMutation();
  const [downloadStatus, setDownloadStatus] =
    useState<DownloadStatus>("idle");
  const [shareStatus, setShareStatus] =
    useState<ShareStatus>("idle");
  const downloadSeqRef = useRef(0);
  const shareSeqRef = useRef(0);
  const openRef = useRef(open);

  useLayoutEffect(() => {
    openRef.current = open;
  }, [open]);

  useEffect(() => {
    downloadSeqRef.current += 1;
    shareSeqRef.current += 1;
    let canceled = false;
    queueMicrotask(() => {
      if (canceled) return;
      setDownloadStatus("idle");
      setShareStatus("idle");
    });
    return () => {
      canceled = true;
    };
  }, [imageStateKey, open]);

  const handleDownload = useCallback(() => {
    if (!imageSrc || downloadStatus === "downloading") return;
    const operationKey = imageStateKey;
    const operationSeq = downloadSeqRef.current + 1;
    downloadSeqRef.current = operationSeq;
    const operationIsCurrent = () =>
      openRef.current &&
      activeImageStateKeyRef.current === operationKey &&
      downloadSeqRef.current === operationSeq;
    setDownloadStatus("downloading");

    void (async () => {
      const anchor = downloadAnchorRef.current;
      if (!anchor) {
        if (operationIsCurrent()) setDownloadStatus("idle");
        return;
      }
      const status = await downloadDesktopImage({
        src: imageSrc,
        id: imageId,
        mime: currentImageMeta?.mime,
        filename: currentImageMeta?.filename,
        anchor,
        operationIsCurrent,
      });
      if (!status || !operationIsCurrent()) return;
      setDownloadStatus(status);
      const delay = status === "success" ? 1400 : 1800;
      window.setTimeout(() => {
        if (operationIsCurrent()) setDownloadStatus("idle");
      }, delay);
    })();
  }, [
    activeImageStateKeyRef,
    currentImageMeta,
    downloadAnchorRef,
    downloadStatus,
    imageId,
    imageSrc,
    imageStateKey,
  ]);

  const resetShareStatusSoon = useCallback(
    (operationKey: string, operationSeq: number) => {
      window.setTimeout(() => {
        if (
          openRef.current &&
          activeImageStateKeyRef.current === operationKey &&
          shareSeqRef.current === operationSeq
        ) {
          setShareStatus("idle");
        }
      }, 1600);
    },
    [activeImageStateKeyRef],
  );

  const handleShare = useCallback(() => {
    if (!imageId || shareStatus === "creating") return;
    const operationKey = imageStateKey;
    const operationSeq = shareSeqRef.current + 1;
    shareSeqRef.current = operationSeq;
    const operationIsCurrent = () =>
      openRef.current &&
      activeImageStateKeyRef.current === operationKey &&
      shareSeqRef.current === operationSeq;
    setShareStatus("creating");

    void (async () => {
      const status = await shareDesktopImage({
        imageId,
        createShare: createShareMutation.mutateAsync,
        writeClipboard: writeClipboardText,
        operationIsCurrent,
      });
      if (!status || !operationIsCurrent()) return;
      setShareStatus(status);
      if (status !== "idle") {
        resetShareStatusSoon(operationKey, operationSeq);
      }
    })();
  }, [
    activeImageStateKeyRef,
    createShareMutation.mutateAsync,
    imageId,
    imageStateKey,
    resetShareStatusSoon,
    shareStatus,
  ]);

  const handleOpenOriginal = useCallback(() => {
    if (!imageSrc) return;
    window.open(imageSrc, "_blank", "noopener,noreferrer");
  }, [imageSrc]);

  return {
    ...desktopActionPresentation(downloadStatus, shareStatus),
    downloadStatus,
    shareStatus,
    handleDownload,
    handleShare,
    handleOpenOriginal,
  };
}
