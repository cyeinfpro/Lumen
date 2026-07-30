"use client";

import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";

interface UseDesktopLightboxDialogOptions {
  open: boolean;
  displaySrc?: string | null;
}

export function useDesktopLightboxDialog({
  open,
  displaySrc,
}: UseDesktopLightboxDialogOptions) {
  const [detailsOpen, setDetailsOpen] = useState(false);
  const downloadAnchorRef = useRef<HTMLAnchorElement>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const imageWrapRef = useRef<HTMLDivElement | null>(null);
  const imageRef = useRef<HTMLImageElement | null>(null);
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  const previouslyFocusedRef = useRef<HTMLElement | null>(null);
  const dialogTitleId = useId();
  const containerElementId = `${dialogTitleId}-container`;
  const downloadAnchorElementId = `${dialogTitleId}-download`;
  const imageWrapElementId = `${dialogTitleId}-image-wrap`;
  const imageElementId = `${dialogTitleId}-image`;
  const closeButtonElementId = `${dialogTitleId}-close`;

  useLayoutEffect(() => {
    if (!open) {
      containerRef.current = null;
      downloadAnchorRef.current = null;
      imageWrapRef.current = null;
      imageRef.current = null;
      closeButtonRef.current = null;
      return;
    }
    containerRef.current = document.getElementById(
      containerElementId,
    ) as HTMLDivElement | null;
    downloadAnchorRef.current = document.getElementById(
      downloadAnchorElementId,
    ) as HTMLAnchorElement | null;
    imageWrapRef.current = document.getElementById(
      imageWrapElementId,
    ) as HTMLDivElement | null;
    imageRef.current = document.getElementById(
      imageElementId,
    ) as HTMLImageElement | null;
    closeButtonRef.current = document.getElementById(
      closeButtonElementId,
    ) as HTMLButtonElement | null;
  }, [
    closeButtonElementId,
    containerElementId,
    displaySrc,
    downloadAnchorElementId,
    imageElementId,
    imageWrapElementId,
    open,
  ]);

  useEffect(() => {
    if (!open) return;
    previouslyFocusedRef.current =
      document.activeElement as HTMLElement | null;
    const frame = requestAnimationFrame(() => {
      const target =
        closeButtonRef.current ?? containerRef.current;
      target?.focus({ preventScroll: true });
    });
    return () => {
      cancelAnimationFrame(frame);
      const previous = previouslyFocusedRef.current;
      if (previous && typeof previous.focus === "function") {
        try {
          previous.focus({ preventScroll: true });
        } catch {
          // The previous element may have unmounted.
        }
      }
      previouslyFocusedRef.current = null;
    };
  }, [open]);

  useEffect(() => {
    if (open) return;
    let canceled = false;
    queueMicrotask(() => {
      if (!canceled) setDetailsOpen(false);
    });
    return () => {
      canceled = true;
    };
  }, [open]);

  useBodyScrollLock(open, {
    bodyOverscrollBehavior: "contain",
    documentOverscrollBehavior: "contain",
  });

  const toggleDetails = useCallback(() => {
    setDetailsOpen((current) => !current);
  }, []);
  const hideDetails = useCallback(() => {
    setDetailsOpen(false);
  }, []);
  const hideActiveImageLayer = useCallback(() => {
    const wrap = imageWrapRef.current;
    if (wrap) {
      wrap.style.transition = "opacity 80ms linear";
      wrap.style.opacity = "0";
    }
    const image = imageRef.current;
    if (!image) return;
    image.style.transition = "opacity 80ms linear";
    image.style.opacity = "0";
    image.style.visibility = "hidden";
  }, []);

  return {
    detailsOpen,
    toggleDetails,
    hideDetails,
    hideActiveImageLayer,
    downloadAnchorRef,
    containerRef,
    imageWrapRef,
    imageRef,
    dialogTitleId,
    containerElementId,
    downloadAnchorElementId,
    imageWrapElementId,
    imageElementId,
    closeButtonElementId,
  };
}
