"use client";

// MobileLightbox —— 极简全屏看图器（2026-04-24 重构）。
//
// 设计原则：
//   1. 按需 mount：state 为空时整棵子树不渲染。
//   2. 单一状态源：内部 state 为准；URL ?img=<id> 只做前向 replace + 反向
//      同步（渲染期 diff，不用 effect setState），不用 router.back。
//   3. 手势层复用 LightboxGestures：左右滑切、下拉关闭、上拉参数、
//      pinch-zoom、双击缩放、放大后拖拽。
//   4. 展示层走 `previewUrl`（display2048）避免 4K 原图 decode 卡死；
//      下载 / 原图走 `url`（binary）。
//
// 对外契约保持：监听 `lumen:open-lightbox` CustomEvent（items/initialId/fromRect）。

import { animate, useMotionValue, useMotionValueEvent } from "framer-motion";
import { useSearchParams } from "next/navigation";
import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import { flushSync } from "react-dom";

import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import { copyTextToClipboard } from "@/lib/clipboard";
import { useChatStore } from "@/store/useChatStore";
import { useUiStore } from "@/store/useUiStore";
import { pushMobileToast } from "@/components/ui/primitives/mobile";
import { useCreateShareMutation } from "@/lib/queries";
import { useInpaintStore } from "@/store/useInpaintStore";
import { useLightboxGestures } from "./LightboxGestures";
import {
  MobileLightboxView,
  type ActionNotice,
  type DownloadStatus,
  type ImgStatus,
  type ThumbnailItem,
  type VisibleSlide,
} from "./MobileLightboxView";
import {
  displayUrlForItem,
  isImageDecoded,
  preloadImage,
  preloadLightboxItem,
} from "./mobileLightboxMedia";
import {
  CLOSE_EVENT,
  OPEN_EVENT,
  type LightboxItem,
  type OpenLightboxDetail,
} from "./types";

interface OpenState {
  items: LightboxItem[];
  currentId: string;
}

type MotionPlayback = {
  stop: () => void;
  then: (onResolve: () => void) => Promise<void>;
};

const _subscribeNoop = () => () => {};
const _getClientSnapshot = () => true;
const _getServerSnapshot = () => false;
const CHROME_HIDE_MS = 2600;
const CHROME_ACTIVITY_THROTTLE_MS = 320;
const PRELOAD_NEIGHBOR_RADIUS = 2;
const THUMB_WINDOW_SIZE = 17;
const EMPTY_LIGHTBOX_ITEMS: LightboxItem[] = [];

function extensionFromMime(mime: string | null | undefined): string | null {
  if (!mime) return null;
  const normalized = mime.split(";")[0]?.trim().toLowerCase();
  if (!normalized?.startsWith("image/")) return null;
  const ext = normalized.slice("image/".length);
  if (!ext) return null;
  if (ext === "jpeg") return "jpg";
  if (ext === "svg+xml") return "svg";
  return ext;
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
  const ext = extensionFromMime(mime) ?? extensionFromSrc(src) ?? "png";
  return `lumen-${id}.${ext}`;
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

export function MobileLightbox() {
  const searchParams = useSearchParams();
  const createShareMutation = useCreateShareMutation();
  // 订阅 useUiStore.lightbox.action：dialog 模式下「设为当前模特」等附加按钮。
  // MobileLightbox 自身仍以本地 OpenState 作为 source of truth，因此这里只读 action。
  const lightboxAction = useUiStore((s) => s.lightbox.action);

  const [state, setState] = useState<OpenState | null>(null);
  const [paramsOpen, setParamsOpen] = useState(false);
  const [imgStatus, setImgStatus] = useState<ImgStatus>("loading");
  const [useFallback, setUseFallback] = useState(false);
  const gestureTargetRef = useRef<HTMLDivElement | null>(null);
  const downloadAnchorRef = useRef<HTMLAnchorElement | null>(null);
  const dialogRootRef = useRef<HTMLDivElement | null>(null);
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  const previouslyFocusedRef = useRef<HTMLElement | null>(null);
  const dialogTitleId = useId();
  const dragX = useMotionValue(0);
  const dragY = useMotionValue(0);
  const scale = useMotionValue(1);
  const haloOpacity = useMotionValue(1);
  // SSR 保护：服务端 useSearchParams 可能返回 null/空；首屏不读 URL，
  // 客户端 hydration 后再同步，避免 hydration mismatch（useSyncExternalStore 双快照）。
  const mounted = useSyncExternalStore(
    _subscribeNoop,
    _getClientSnapshot,
    _getServerSnapshot,
  );
  const urlImg = mounted ? (searchParams?.get("img") ?? null) : null;
  const [prevUrlImg, setPrevUrlImg] = useState<string | null>(null);
  const [chromeVisible, setChromeVisible] = useState(true);
  const [zoomLevel, setZoomLevel] = useState(1);
  const [downloadStatus, setDownloadStatus] = useState<DownloadStatus>("idle");
  const [actionNotice, setActionNotice] = useState<ActionNotice>(null);
  const [boundaryHint, setBoundaryHint] = useState<"first" | "last" | null>(
    null,
  );
  const [fallbackItemIds, setFallbackItemIds] = useState<ReadonlySet<string>>(
    () => new Set(),
  );
  const chromeTimerRef = useRef<number | null>(null);
  const chromeVisibleRef = useRef(true);
  const imgStatusRef = useRef<ImgStatus>("loading");
  const paramsOpenRef = useRef(false);
  const lastChromeActivityRef = useRef(0);
  const feedbackTimerRef = useRef<number | null>(null);
  const boundaryTimerRef = useRef<number | null>(null);
  const downloadResetTimerRef = useRef<number | null>(null);
  const activeThumbRef = useRef<HTMLButtonElement | null>(null);
  const switchSeqRef = useRef(0);
  const preloadAbortRef = useRef<AbortController | null>(null);
  const swipeAnimationRef = useRef<MotionPlayback | null>(null);

  const resetMotion = useCallback(() => {
    dragX.set(0);
    dragY.set(0);
    scale.set(1);
    haloOpacity.set(1);
    setZoomLevel(1);
  }, [dragX, dragY, scale, haloOpacity]);

  const stopSwipeAnimation = useCallback(() => {
    swipeAnimationRef.current?.stop();
    swipeAnimationRef.current = null;
  }, []);

  const markItemFallback = useCallback((id: string) => {
    setFallbackItemIds((prev) => {
      if (prev.has(id)) return prev;
      const next = new Set(prev);
      next.add(id);
      return next;
    });
  }, []);

  useMotionValueEvent(scale, "change", (latest) => {
    setZoomLevel(latest);
  });

  useEffect(() => {
    chromeVisibleRef.current = chromeVisible;
  }, [chromeVisible]);

  useEffect(() => {
    imgStatusRef.current = imgStatus;
  }, [imgStatus]);

  useEffect(() => {
    paramsOpenRef.current = paramsOpen;
  }, [paramsOpen]);

  const clearChromeTimer = useCallback(() => {
    if (chromeTimerRef.current !== null) {
      window.clearTimeout(chromeTimerRef.current);
      chromeTimerRef.current = null;
    }
  }, []);

  const clearFeedbackTimer = useCallback(() => {
    if (feedbackTimerRef.current !== null) {
      window.clearTimeout(feedbackTimerRef.current);
      feedbackTimerRef.current = null;
    }
  }, []);

  const clearBoundaryTimer = useCallback(() => {
    if (boundaryTimerRef.current !== null) {
      window.clearTimeout(boundaryTimerRef.current);
      boundaryTimerRef.current = null;
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

  const showBoundaryHint = useCallback(
    (edge: "first" | "last") => {
      clearBoundaryTimer();
      setBoundaryHint(edge);
      setChromeVisible(true);
      boundaryTimerRef.current = window.setTimeout(() => {
        setBoundaryHint(null);
        boundaryTimerRef.current = null;
      }, 1100);
    },
    [clearBoundaryTimer],
  );

  const scheduleChromeHide = useCallback(() => {
    clearChromeTimer();
    if (paramsOpenRef.current || imgStatusRef.current !== "loaded") return;
    chromeTimerRef.current = window.setTimeout(() => {
      chromeVisibleRef.current = false;
      setChromeVisible(false);
      chromeTimerRef.current = null;
    }, CHROME_HIDE_MS);
  }, [clearChromeTimer]);

  const handlePointerActivity = useCallback(() => {
    const now = performance.now();
    const shouldReschedule =
      now - lastChromeActivityRef.current >= CHROME_ACTIVITY_THROTTLE_MS;
    if (!chromeVisibleRef.current) {
      chromeVisibleRef.current = true;
      setChromeVisible(true);
    } else if (!shouldReschedule) {
      return;
    }
    lastChromeActivityRef.current = now;
    scheduleChromeHide();
  }, [scheduleChromeHide]);

  const resetZoom = useCallback(() => {
    resetMotion();
    setChromeVisible(true);
    scheduleChromeHide();
  }, [resetMotion, scheduleChromeHide]);

  // —— URL 写入：单向 replace ?img=<id>；id=null 删除 ?img ——
  const replaceUrlWithImg = useCallback((id: string | null) => {
    if (typeof window === "undefined") return;
    const url = new URL(window.location.href);
    if (id === null) url.searchParams.delete("img");
    else url.searchParams.set("img", id);
    const href = `${url.pathname}${url.search}${url.hash}`;
    window.history.replaceState(window.history.state, "", href);
  }, []);
  // event listener 里要拿最新版；但 ref 更新放到 effect 里（render 期禁写 ref）。
  const replaceRef = useRef(replaceUrlWithImg);
  useEffect(() => {
    replaceRef.current = replaceUrlWithImg;
  }, [replaceUrlWithImg]);

  const switchToItem = useCallback(
    (nextItem: LightboxItem, options: { replaceUrl?: boolean } = {}) => {
      const replaceUrl = options.replaceUrl !== false;
      const seq = switchSeqRef.current + 1;
      switchSeqRef.current = seq;
      stopSwipeAnimation();
      preloadAbortRef.current?.abort();
      const preloadAbort = new AbortController();
      preloadAbortRef.current = preloadAbort;
      const knownFallback = fallbackItemIds.has(nextItem.id);
      const nextDisplayUrl = displayUrlForItem(nextItem, knownFallback);
      resetMotion();
      setParamsOpen(false);
      setChromeVisible(true);
      setDownloadStatus("idle");
      setBoundaryHint(null);
      setActionNotice(null);
      setUseFallback(knownFallback);
      setImgStatus(isImageDecoded(nextDisplayUrl) ? "loaded" : "loading");
      setState((prev) => (prev ? { ...prev, currentId: nextItem.id } : prev));
      if (replaceUrl) {
        replaceRef.current(nextItem.id);
      }

      void (async () => {
        let useOriginalFallback = knownFallback;
        try {
          if (knownFallback) {
            await preloadImage(nextItem.url, preloadAbort.signal);
          } else {
            useOriginalFallback = await preloadLightboxItem(
              nextItem,
              preloadAbort.signal,
            );
          }
        } catch {
          if (preloadAbort.signal.aborted) return;
          if (preloadAbortRef.current === preloadAbort) {
            preloadAbortRef.current = null;
          }
          return;
        }
        if (switchSeqRef.current !== seq) return;
        if (preloadAbortRef.current === preloadAbort) {
          preloadAbortRef.current = null;
        }
        if (useOriginalFallback) {
          markItemFallback(nextItem.id);
          setUseFallback(true);
        }
        setImgStatus("loaded");
      })();
    },
    [fallbackItemIds, markItemFallback, resetMotion, stopSwipeAnimation],
  );

  // —— event listener：按依赖更新，避免 handler 读旧状态 ——
  useEffect(() => {
    const onOpen = (e: Event) => {
      const ce = e as CustomEvent<OpenLightboxDetail>;
      const detail = ce.detail;
      if (!detail || !detail.items || detail.items.length === 0) return;
      const initialId =
        detail.items.find((x) => x.id === detail.initialId)?.id ??
        detail.items[0].id;
      const initialItem =
        detail.items.find((x) => x.id === initialId) ?? detail.items[0];
      const knownFallback = fallbackItemIds.has(initialId);
      switchSeqRef.current += 1;
      stopSwipeAnimation();
      preloadAbortRef.current?.abort();
      preloadAbortRef.current = null;
      setState({ items: detail.items, currentId: initialId });
      setParamsOpen(false);
      setChromeVisible(true);
      resetMotion();
      setImgStatus(
        isImageDecoded(displayUrlForItem(initialItem, knownFallback))
          ? "loaded"
          : "loading",
      );
      setUseFallback(knownFallback);
      setDownloadStatus("idle");
      clearDownloadResetTimer();
      setActionNotice(null);
      setBoundaryHint(null);
      replaceRef.current(initialId);
    };
    const onCloseEvt = () => {
      switchSeqRef.current += 1;
      stopSwipeAnimation();
      preloadAbortRef.current?.abort();
      preloadAbortRef.current = null;
      setState(null);
      setParamsOpen(false);
      setChromeVisible(true);
      clearChromeTimer();
      resetMotion();
      setImgStatus("loading");
      setUseFallback(false);
      setDownloadStatus("idle");
      clearDownloadResetTimer();
      setActionNotice(null);
      setBoundaryHint(null);
      replaceRef.current(null);
      useUiStore.getState().closeLightbox();
    };
    window.addEventListener(OPEN_EVENT, onOpen as EventListener);
    window.addEventListener(CLOSE_EVENT, onCloseEvt);
    return () => {
      window.removeEventListener(OPEN_EVENT, onOpen as EventListener);
      window.removeEventListener(CLOSE_EVENT, onCloseEvt);
    };
  }, [
    clearChromeTimer,
    clearDownloadResetTimer,
    fallbackItemIds,
    resetMotion,
    stopSwipeAnimation,
  ]);

  useEffect(() => {
    return () => {
      clearFeedbackTimer();
      clearBoundaryTimer();
      clearDownloadResetTimer();
      stopSwipeAnimation();
      preloadAbortRef.current?.abort();
      preloadAbortRef.current = null;
    };
  }, [
    clearBoundaryTimer,
    clearDownloadResetTimer,
    clearFeedbackTimer,
    stopSwipeAnimation,
  ]);

  // —— URL → state 反向同步：只在客户端 effect 同步，避免 SSR/渲染期 setState ——
  useEffect(() => {
    if (urlImg === prevUrlImg) return;
    let canceled = false;
    queueMicrotask(() => {
      if (canceled) return;
      if (state?.currentId === urlImg) {
        setPrevUrlImg(urlImg);
        return;
      }
      if (urlImg && state) {
        const target = state.items.find((x) => x.id === urlImg);
        if (target) {
          setPrevUrlImg(urlImg);
          switchToItem(target, { replaceUrl: false });
          return;
        }
        setPrevUrlImg(state.currentId);
        replaceRef.current(state.currentId);
        return;
      }
      setPrevUrlImg(urlImg);
      setParamsOpen(false);
      setChromeVisible(true);
      resetMotion();
      setImgStatus("loading");
      setUseFallback(false);
      setDownloadStatus("idle");
      setActionNotice(null);
      setBoundaryHint(null);
      setState((prev) => {
        if (!prev) return prev;
        if (!urlImg) {
          return null;
        }
        if (urlImg === prev.currentId) return prev;
        const exists = prev.items.some((x) => x.id === urlImg);
        if (!exists) return prev;
        return { ...prev, currentId: urlImg };
      });
    });
    return () => {
      canceled = true;
    };
  }, [prevUrlImg, resetMotion, state, switchToItem, urlImg]);

  // —— 切图 ——
  const goto = useCallback(
    (delta: 1 | -1) => {
      if (!state) return;
      const idx = state.items.findIndex((x) => x.id === state.currentId);
      if (idx < 0) return;
      const next = idx + delta;
      if (next < 0 || next >= state.items.length) {
        showBoundaryHint(delta < 0 ? "first" : "last");
        return;
      }
      const nextItem = state.items[next];
      switchToItem(nextItem);
    },
    [showBoundaryHint, state, switchToItem],
  );

  const commitSwipe = useCallback(
    (delta: 1 | -1): boolean => {
      if (!state || swipeAnimationRef.current) return false;
      const idx = state.items.findIndex((x) => x.id === state.currentId);
      if (idx < 0) return false;
      const next = idx + delta;
      if (next < 0 || next >= state.items.length) {
        showBoundaryHint(delta < 0 ? "first" : "last");
        return false;
      }

      const nextItem = state.items[next];
      const width =
        gestureTargetRef.current?.clientWidth ||
        (typeof window !== "undefined" ? window.innerWidth : 0);
      if (!width) {
        switchToItem(nextItem);
        return true;
      }

      const seq = switchSeqRef.current;
      setParamsOpen(false);
      const controls = animate(dragX, -delta * width, {
        type: "spring",
        stiffness: 520,
        damping: 48,
        mass: 0.85,
        restDelta: 0.5,
        restSpeed: 24,
      }) as MotionPlayback;
      swipeAnimationRef.current = controls;
      void controls.then(() => {
        if (
          switchSeqRef.current !== seq ||
          swipeAnimationRef.current !== controls
        ) {
          return;
        }
        swipeAnimationRef.current = null;
        flushSync(() => {
          switchToItem(nextItem);
        });
      });
      return true;
    },
    [dragX, showBoundaryHint, state, switchToItem],
  );

  const close = useCallback(() => {
    switchSeqRef.current += 1;
    stopSwipeAnimation();
    preloadAbortRef.current?.abort();
    preloadAbortRef.current = null;
    setState(null);
    setParamsOpen(false);
    setChromeVisible(true);
    clearChromeTimer();
    resetMotion();
    setImgStatus("loading");
    setUseFallback(false);
    setDownloadStatus("idle");
    clearDownloadResetTimer();
    setActionNotice(null);
    setBoundaryHint(null);
    replaceRef.current(null);
    // 同步清空 store：openLightboxFromItems 写入了 open=true / action，
    // 仅清本地 state 会让 MobileTabBar（订阅 lightbox.open）持续隐藏，
    // 下次开 lightbox 还会带出旧 action。store setState 幂等，无回环风险。
    useUiStore.getState().closeLightbox();
  }, [
    clearChromeTimer,
    clearDownloadResetTimer,
    resetMotion,
    stopSwipeAnimation,
  ]);

  // —— 键盘：Esc 关 / ←→ 切 / Tab 焦点循环 ——
  useEffect(() => {
    if (!state) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        if (paramsOpen) {
          setParamsOpen(false);
          setChromeVisible(true);
          scheduleChromeHide();
          return;
        }
        close();
        return;
      }
      if (e.key === "ArrowLeft") {
        goto(-1);
        return;
      }
      if (e.key === "ArrowRight") {
        goto(1);
        return;
      }
      if (e.key === "Tab") {
        const root = dialogRootRef.current;
        if (!root) return;
        const focusables = Array.from(
          root.querySelectorAll<HTMLElement>(
            'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
          ),
        ).filter((el) => !el.hasAttribute("data-focus-skip"));
        if (focusables.length === 0) {
          e.preventDefault();
          return;
        }
        const first = focusables[0];
        const last = focusables[focusables.length - 1];
        const active = document.activeElement as HTMLElement | null;
        if (e.shiftKey) {
          if (active === first || !root.contains(active)) {
            e.preventDefault();
            last.focus();
          }
        } else if (active === last) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [state, close, goto, paramsOpen, scheduleChromeHide]);

  const isOpen = state !== null;

  // —— 打开时焦点移到关闭按钮，关闭时还原 ——
  useEffect(() => {
    if (!isOpen) return;
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
  }, [isOpen]);

  // —— body 滚动锁（防 iOS 橡皮筋穿透）——
  useBodyScrollLock(isOpen, { documentOverscrollBehavior: "none" });

  const openCurrentId = state?.currentId ?? null;
  useEffect(() => {
    if (!openCurrentId) {
      clearChromeTimer();
      return;
    }
    let canceled = false;
    queueMicrotask(() => {
      if (!canceled) setChromeVisible(true);
    });
    scheduleChromeHide();
    return () => {
      canceled = true;
      clearChromeTimer();
    };
  }, [
    clearChromeTimer,
    imgStatus,
    openCurrentId,
    paramsOpen,
    scheduleChromeHide,
  ]);

  const handleDownload = useCallback(() => {
    const currentState = state;
    if (!currentState) return;
    const currentItem = currentState.items.find(
      (item) => item.id === currentState.currentId,
    );
    const anchor = downloadAnchorRef.current;
    if (!currentItem || !anchor) return;

    void (async () => {
      let objectUrl: string | null = null;
      clearDownloadResetTimer();
      setDownloadStatus("downloading");
      setActionNotice({ kind: "info", text: "正在下载原图" });
      try {
        const blob = await fetchImageBlob(currentItem.url);
        const fallbackMime =
          currentItem.mime ?? currentItem.mime_type ?? currentItem.content_type;
        const filename = downloadFilename(
          currentItem.id,
          currentItem.url,
          blob.type || fallbackMime,
          currentItem.filename ?? currentItem.file_name,
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
          currentItem.url,
          downloadFilename(
            currentItem.id,
            currentItem.url,
            currentItem.mime ??
              currentItem.mime_type ??
              currentItem.content_type,
            currentItem.filename ?? currentItem.file_name,
          ),
        );
        setDownloadStatus("error");
        showNotice({ kind: "error", text: "下载失败，已尝试打开原图" });
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
  }, [clearDownloadResetTimer, showNotice, state]);

  const handleCopyPrompt = useCallback(() => {
    const currentState = state;
    if (!currentState) return;
    const currentItem = currentState.items.find(
      (item) => item.id === currentState.currentId,
    );
    if (!currentItem?.prompt) return;
    void writeClipboardText(currentItem.prompt)
      .then(() => showNotice({ kind: "success", text: "Prompt 已复制" }))
      .catch(() => showNotice({ kind: "error", text: "复制失败" }));
  }, [showNotice, state]);

  const handleShare = useCallback(() => {
    const currentState = state;
    if (!currentState || typeof window === "undefined") return;
    if (createShareMutation.isPending) {
      showNotice({ kind: "info", text: "正在生成分享链接" });
      return;
    }
    const currentItem = currentState.items.find(
      (item) => item.id === currentState.currentId,
    );
    if (!currentItem) return;

    void (async () => {
      setActionNotice({ kind: "info", text: "正在生成分享链接" });
      let link: string;
      try {
        const share = await createShareMutation.mutateAsync({
          imageId: currentItem.id,
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
          if (error instanceof DOMException && error.name === "AbortError") {
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
  }, [createShareMutation, showNotice, state]);

  const handleIterate = useCallback(() => {
    if (!state) return;
    const id = state.currentId;
    const img = useChatStore.getState().imagesById[id];
    if (!img) return;
    close();
    useChatStore.getState().promoteImageToReference(id);
    pushMobileToast("已设为参考图，可继续迭代", "success");
  }, [state, close]);

  const handleUpscale = useCallback(() => {
    if (!state) return;
    const id = state.currentId;
    close();
    void useChatStore.getState().upscaleImage(id);
    pushMobileToast("正在以中等质量放大…", "success");
  }, [state, close]);

  const handleReroll = useCallback(() => {
    if (!state) return;
    const id = state.currentId;
    close();
    void useChatStore.getState().rerollImage(id);
    pushMobileToast("正在重新生成…", "success");
  }, [state, close]);

  const handleInpaint = useCallback(() => {
    if (!state) return;
    const id = state.currentId;
    const img = useChatStore.getState().imagesById[id];
    if (!img) return;
    close();
    useInpaintStore.getState().openInpaint({
      imageId: img.id,
      src: img.data_url,
      width: img.width,
      height: img.height,
    });
  }, [state, close]);

  const items = state?.items ?? EMPTY_LIGHTBOX_ITEMS;
  const idx = state ? items.findIndex((x) => x.id === state.currentId) : -1;
  const current = idx >= 0 ? items[idx] : null;
  const total = items.length;
  const isFirst = idx <= 0;
  const isLast = idx < 0 || idx === total - 1;

  useEffect(() => {
    if (!current || idx < 0) return;
    const seq = switchSeqRef.current;
    const controller = new AbortController();
    let disposed = false;
    const preloadTargets: LightboxItem[] = [];
    for (
      let i = Math.max(0, idx - PRELOAD_NEIGHBOR_RADIUS);
      i <= Math.min(items.length - 1, idx + PRELOAD_NEIGHBOR_RADIUS);
      i += 1
    ) {
      preloadTargets.push(items[i]);
    }

    preloadTargets.forEach((item) => {
      const isActive = item.id === current.id;
      const knownFallback = fallbackItemIds.has(item.id);
      const signal = isActive ? controller.signal : undefined;
      const warm = knownFallback
        ? preloadImage(item.url, signal).then(() => true)
        : preloadLightboxItem(item, signal);

      void warm
        .then((usedFallback) => {
          if (disposed) return;
          if (usedFallback) markItemFallback(item.id);
          if (!isActive || switchSeqRef.current !== seq) return;
          if (usedFallback) setUseFallback(true);
          setImgStatus("loaded");
        })
        .catch(() => {
          if (
            disposed ||
            !isActive ||
            controller.signal.aborted ||
            switchSeqRef.current !== seq
          ) {
            return;
          }
          setImgStatus("error");
        });
    });

    return () => {
      disposed = true;
      controller.abort();
    };
  }, [current, fallbackItemIds, idx, items, markItemFallback]);

  useEffect(() => {
    if (!current?.id || total <= 1) return;
    const raf = requestAnimationFrame(() => {
      activeThumbRef.current?.scrollIntoView({
        behavior: "auto",
        block: "nearest",
        inline: "center",
      });
    });
    return () => cancelAnimationFrame(raf);
  }, [current?.id, total]);

  const thumbItems = useMemo<ThumbnailItem[]>(() => {
    if (idx < 0 || total <= THUMB_WINDOW_SIZE) {
      return items.map((item, itemIdx) => ({ item, itemIdx }));
    }
    const radius = Math.floor(THUMB_WINDOW_SIZE / 2);
    let start = Math.max(0, idx - radius);
    const end = Math.min(total, start + THUMB_WINDOW_SIZE);
    start = Math.max(0, end - THUMB_WINDOW_SIZE);
    return items
      .slice(start, end)
      .map((item, offset) => ({ item, itemIdx: start + offset }));
  }, [idx, items, total]);

  const visibleSlides = useMemo<VisibleSlide[]>(() => {
    if (!current || idx < 0) return [];
    const slides: Array<{ item: LightboxItem; offset: -1 | 0 | 1 }> = [];
    if (idx > 0) slides.push({ item: items[idx - 1], offset: -1 });
    slides.push({ item: current, offset: 0 });
    if (idx < total - 1) slides.push({ item: items[idx + 1], offset: 1 });
    return slides;
  }, [current, idx, items, total]);

  useLightboxGestures(
    gestureTargetRef,
    {
      onSwipeLeft: () => commitSwipe(1),
      onSwipeRight: () => commitSwipe(-1),
      onDismiss: close,
      onRevealOpen: () => setParamsOpen(true),
      onRevealClose: () => setParamsOpen(false),
      onTap: () => {
        if (paramsOpen) {
          setParamsOpen(false);
          setChromeVisible(true);
          scheduleChromeHide();
          return;
        }
        setChromeVisible((visible) => {
          if (visible) {
            clearChromeTimer();
            return false;
          }
          scheduleChromeHide();
          return true;
        });
      },
      onDoubleTap: () => {
        setChromeVisible(true);
        if (scale.get() > 1.01) {
          resetMotion();
        } else {
          dragX.set(0);
          dragY.set(0);
          haloOpacity.set(1);
          scale.set(2);
        }
        scheduleChromeHide();
      },
      onPointerActivity: handlePointerActivity,
      onBoundarySwipe: showBoundaryHint,
    },
    {
      enabled: Boolean(current),
      revealOpen: paramsOpen,
      isFirst,
      isLast,
      dragX,
      dragY,
      scale,
      haloOpacity,
    },
  );

  return (
    <MobileLightboxView
      current={current}
      idx={idx}
      total={total}
      isFirst={isFirst}
      isLast={isLast}
      paramsOpen={paramsOpen}
      imgStatus={imgStatus}
      useFallback={useFallback}
      fallbackItemIds={fallbackItemIds}
      chromeVisible={chromeVisible}
      zoomLevel={zoomLevel}
      downloadStatus={downloadStatus}
      actionNotice={actionNotice}
      boundaryHint={boundaryHint}
      lightboxAction={lightboxAction}
      visibleSlides={visibleSlides}
      thumbItems={thumbItems}
      gestureTargetRef={gestureTargetRef}
      downloadAnchorRef={downloadAnchorRef}
      dialogRootRef={dialogRootRef}
      closeButtonRef={closeButtonRef}
      activeThumbRef={activeThumbRef}
      dialogTitleId={dialogTitleId}
      dragX={dragX}
      dragY={dragY}
      scale={scale}
      haloOpacity={haloOpacity}
      onClose={close}
      onGoto={goto}
      onResetZoom={resetZoom}
      onDownload={handleDownload}
      onSwitchItem={switchToItem}
      onMarkFallback={markItemFallback}
      setUseFallback={setUseFallback}
      setImgStatus={setImgStatus}
      onIterate={handleIterate}
      onInpaint={handleInpaint}
      onUpscale={handleUpscale}
      onReroll={handleReroll}
      onCopyPrompt={handleCopyPrompt}
      onShare={handleShare}
      onOpenParams={() => {
        setParamsOpen(true);
        setChromeVisible(true);
      }}
      onCloseParams={() => {
        setParamsOpen(false);
        setChromeVisible(true);
        scheduleChromeHide();
      }}
    />
  );
}
