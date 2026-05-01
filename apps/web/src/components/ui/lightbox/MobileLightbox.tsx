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

import {
  animate,
  motion,
  useMotionValue,
  useMotionValueEvent,
} from "framer-motion";
import {
  X,
  ChevronLeft,
  ChevronRight,
  Download,
  Info,
  RotateCcw,
  Copy,
  Share2,
  Check,
  AlertCircle,
  Pencil,
  ArrowUpRight,
  RefreshCw,
} from "lucide-react";
import { useSearchParams } from "next/navigation";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import { flushSync } from "react-dom";

import { cn } from "@/lib/utils";
import { DURATION, EASE, SPRING } from "@/lib/motion";
import { Spinner } from "@/components/ui/primitives/Spinner";
import { MobileIconButton } from "@/components/ui/primitives/mobile/MobileIconButton";
import { useChatStore } from "@/store/useChatStore";
import { pushMobileToast } from "@/components/ui/primitives/mobile";
import { useCreateShareMutation } from "@/lib/queries";
import { LightboxParamsPanel } from "./LightboxParamsPanel";
import { useLightboxGestures } from "./LightboxGestures";
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

type ImgStatus = "loading" | "loaded" | "error";
type DownloadStatus = "idle" | "downloading" | "success" | "error";
type ActionNotice = {
  kind: "success" | "error" | "info";
  text: string;
} | null;
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
const decodedImageSources = new Set<string>();
const decodePromises = new Map<string, Promise<void>>();
const EMPTY_LIGHTBOX_ITEMS: LightboxItem[] = [];

function markImageDecoded(src: string) {
  if (src) decodedImageSources.add(src);
}

function isImageDecoded(src: string | null | undefined): boolean {
  return Boolean(src && decodedImageSources.has(src));
}

function displayUrlForItem(item: LightboxItem, useOriginal: boolean): string {
  return useOriginal ? item.url : (item.previewUrl || item.url);
}

function posterUrlForItem(item: LightboxItem): string {
  return item.thumbUrl ?? item.previewUrl ?? item.url;
}

function abortable<T>(promise: Promise<T>, signal?: AbortSignal): Promise<T> {
  if (!signal) return promise;
  if (signal.aborted) {
    return Promise.reject(signal.reason ?? new Error("Aborted"));
  }
  const abortSignal = signal;
  return new Promise((resolve, reject) => {
    function cleanup() {
      abortSignal.removeEventListener("abort", onAbort);
    }
    function onAbort() {
      cleanup();
      reject(abortSignal.reason ?? new Error("Aborted"));
    }
    abortSignal.addEventListener("abort", onAbort, { once: true });
    promise.then(
      (value) => {
        cleanup();
        resolve(value);
      },
      (error) => {
        cleanup();
        reject(error);
      },
    );
  });
}

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

function downloadFilename(id: string, src: string, mime?: string): string {
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

function preloadImage(src: string, signal?: AbortSignal): Promise<void> {
  if (typeof window === "undefined") return Promise.resolve();
  if (decodedImageSources.has(src)) return Promise.resolve();

  let promise = decodePromises.get(src);
  if (!promise) {
    promise = new Promise((resolve, reject) => {
      const img = new Image();
      let settled = false;
      const cleanup = () => {
        img.onload = null;
        img.onerror = null;
      };
      const finish = () => {
        if (settled) return;
        settled = true;
        const decode =
          typeof img.decode === "function"
            ? img.decode().catch(() => undefined)
            : Promise.resolve();
        void decode.then(() => {
          markImageDecoded(src);
          cleanup();
          resolve();
        });
      };
      img.decoding = "async";
      img.onload = finish;
      img.onerror = () => {
        if (settled) return;
        settled = true;
        cleanup();
        decodePromises.delete(src);
        reject(new Error("Image preload failed"));
      };
      img.src = src;
      if (img.complete && img.naturalWidth > 0) finish();
    });
    decodePromises.set(src, promise);
  }

  return abortable(promise, signal);
}

async function preloadLightboxItem(
  item: LightboxItem,
  signal?: AbortSignal,
): Promise<boolean> {
  const previewSrc = item.previewUrl || item.url;
  try {
    await preloadImage(previewSrc, signal);
    return false;
  } catch {
    if (signal?.aborted) throw signal.reason;
    if (item.previewUrl && item.previewUrl !== item.url) {
      await preloadImage(item.url, signal);
      return true;
    }
    throw new Error("Lightbox item preload failed");
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
  if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }

  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.left = "-9999px";
  document.body.appendChild(textarea);
  textarea.select();
  try {
    const ok = document.execCommand("copy");
    if (!ok) throw new Error("copy command failed");
  } finally {
    document.body.removeChild(textarea);
  }
}

export function MobileLightbox() {
  const searchParams = useSearchParams();
  const createShareMutation = useCreateShareMutation();

  const [state, setState] = useState<OpenState | null>(null);
  const [paramsOpen, setParamsOpen] = useState(false);
  const [imgStatus, setImgStatus] = useState<ImgStatus>("loading");
  const [useFallback, setUseFallback] = useState(false);
  const gestureTargetRef = useRef<HTMLDivElement | null>(null);
  const downloadAnchorRef = useRef<HTMLAnchorElement | null>(null);
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
  const [boundaryHint, setBoundaryHint] = useState<"first" | "last" | null>(null);
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

  const showNotice = useCallback((notice: NonNullable<ActionNotice>) => {
    clearFeedbackTimer();
    setActionNotice(notice);
    feedbackTimerRef.current = window.setTimeout(() => {
      setActionNotice(null);
      feedbackTimerRef.current = null;
    }, 1700);
  }, [clearFeedbackTimer]);

  const showBoundaryHint = useCallback((edge: "first" | "last") => {
    clearBoundaryTimer();
    setBoundaryHint(edge);
    setChromeVisible(true);
    boundaryTimerRef.current = window.setTimeout(() => {
      setBoundaryHint(null);
      boundaryTimerRef.current = null;
    }, 1100);
  }, [clearBoundaryTimer]);

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
  const replaceUrlWithImg = useCallback(
    (id: string | null) => {
      if (typeof window === "undefined") return;
      const url = new URL(window.location.href);
      if (id === null) url.searchParams.delete("img");
      else url.searchParams.set("img", id);
      const href = `${url.pathname}${url.search}${url.hash}`;
      window.history.replaceState(window.history.state, "", href);
    },
    [],
  );
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
      const initialItem = detail.items.find((x) => x.id === initialId) ?? detail.items[0];
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
        isImageDecoded(displayUrlForItem(initialItem, knownFallback)) ? "loaded" : "loading",
      );
      setUseFallback(knownFallback);
      setDownloadStatus("idle");
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
      setActionNotice(null);
      setBoundaryHint(null);
      replaceRef.current(null);
    };
    window.addEventListener(OPEN_EVENT, onOpen as EventListener);
    window.addEventListener(CLOSE_EVENT, onCloseEvt);
    return () => {
      window.removeEventListener(OPEN_EVENT, onOpen as EventListener);
      window.removeEventListener(CLOSE_EVENT, onCloseEvt);
    };
  }, [clearChromeTimer, fallbackItemIds, resetMotion, stopSwipeAnimation]);

  useEffect(() => {
    return () => {
      clearFeedbackTimer();
      clearBoundaryTimer();
      stopSwipeAnimation();
      preloadAbortRef.current?.abort();
      preloadAbortRef.current = null;
    };
  }, [clearBoundaryTimer, clearFeedbackTimer, stopSwipeAnimation]);

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
  const goto = useCallback((delta: 1 | -1) => {
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
  }, [showBoundaryHint, state, switchToItem]);

  const commitSwipe = useCallback((delta: 1 | -1): boolean => {
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
      if (switchSeqRef.current !== seq || swipeAnimationRef.current !== controls) {
        return;
      }
      swipeAnimationRef.current = null;
      flushSync(() => {
        switchToItem(nextItem);
      });
    });
    return true;
  }, [dragX, showBoundaryHint, state, switchToItem]);

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
    setActionNotice(null);
    setBoundaryHint(null);
    replaceRef.current(null);
  }, [clearChromeTimer, resetMotion, stopSwipeAnimation]);

  // —— 键盘：Esc 关 / ←→ 切 ——
  useEffect(() => {
    if (!state) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
      else if (e.key === "ArrowLeft") goto(-1);
      else if (e.key === "ArrowRight") goto(1);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [state, close, goto]);

  // —— body 滚动锁（防 iOS 橡皮筋穿透）——
  const isOpen = state !== null;
  useEffect(() => {
    if (!isOpen) return;
    const prev = document.body.style.overflow;
    const prevOverscroll = document.documentElement.style.overscrollBehavior;
    document.body.style.overflow = "hidden";
    document.documentElement.style.overscrollBehavior = "none";
    return () => {
      document.body.style.overflow = prev;
      document.documentElement.style.overscrollBehavior = prevOverscroll;
    };
  }, [isOpen]);

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
        );

        if (isIosLike() && typeof File !== "undefined") {
          const file = new File([blob], filename, {
            type: blob.type || fallbackMime || "image/png",
          });
          if (canShareFile(file)) {
            try {
              await navigator.share({
                files: [file],
                title: filename,
              });
              setDownloadStatus("success");
              showNotice({ kind: "success", text: "已发送到分享菜单" });
              return;
            } catch (error) {
              if (error instanceof DOMException && error.name === "AbortError") {
                setDownloadStatus("idle");
                setActionNotice(null);
                return;
              }
            }
          }
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
            currentItem.mime ?? currentItem.mime_type ?? currentItem.content_type,
          ),
        );
        setDownloadStatus("error");
        showNotice({ kind: "error", text: "下载失败，已尝试打开原图" });
      } finally {
        if (objectUrl) {
          const urlToRevoke = objectUrl;
          window.setTimeout(() => URL.revokeObjectURL(urlToRevoke), 1000);
        }
        window.setTimeout(() => setDownloadStatus("idle"), 1800);
      }
    })();
  }, [showNotice, state]);

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

      if (typeof navigator !== "undefined" && typeof navigator.share === "function") {
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
        window.prompt("复制分享链接", link);
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

  const thumbItems = useMemo(() => {
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

  const visibleSlides = useMemo(() => {
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

  // —— 按需 mount ——
  if (!state || !current) return null;

  const currentUseFallback = useFallback || fallbackItemIds.has(current.id);
  const displayUrl = displayUrlForItem(current, currentUseFallback);
  const posterUrl = posterUrlForItem(current);
  const showPoster = imgStatus === "loading" && posterUrl !== displayUrl;
  const sourceLabel = !currentUseFallback && current.previewUrl ? "预览" : "原图";
  const isZoomed = zoomLevel > 1.02;
  const zoomPercent = `${Math.round(zoomLevel * 100)}%`;

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="图片查看器"
      className="fixed inset-0 overflow-hidden"
      style={{
        zIndex: "var(--z-lightbox, 80)" as unknown as number,
        touchAction: "none",
      }}
    >
      <motion.div
        aria-hidden
        className="absolute inset-0 bg-black"
        style={{ opacity: haloOpacity }}
      />
      <a ref={downloadAnchorRef} className="hidden" aria-hidden="true" />

      {/* 图片层：pointer 全部绑这里 */}
      <div
        ref={gestureTargetRef}
        className="absolute inset-0 overflow-hidden flex items-center justify-center"
        style={{ touchAction: "none" }}
      >
        {imgStatus === "error" ? (
          <div className="rounded-2xl border border-white/10 bg-black/50 px-8 py-10 text-center max-w-[280px]">
            <p className="text-base text-white/90">图片加载失败</p>
            <p className="text-xs text-white/50 mt-2">
              数据可能已过期或网络异常，可关闭后重试。
            </p>
          </div>
        ) : (
          <>
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
                const slideUseFallback =
                  active ? currentUseFallback : fallbackItemIds.has(item.id);
                const slideDisplayUrl = displayUrlForItem(item, slideUseFallback);
                const slideCanFallback =
                  !slideUseFallback && item.previewUrl && item.previewUrl !== item.url;
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
                    {active && showPoster && (
                      // eslint-disable-next-line @next/next/no-img-element
                      <img
                        src={posterUrl}
                        alt=""
                        aria-hidden
                        draggable={false}
                        className="pointer-events-none absolute max-h-full max-w-full select-none object-contain opacity-60"
                      />
                    )}
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
                          markItemFallback(item.id);
                          if (active) {
                            setUseFallback(true);
                            setImgStatus(isImageDecoded(item.url) ? "loaded" : "loading");
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
            {imgStatus === "loading" && (
              <div className="absolute inset-0 flex items-center justify-center pointer-events-none bg-black/10">
                <Spinner size={24} className="text-white/50" />
              </div>
            )}
          </>
        )}
      </div>

      {/* 顶部条：关闭 + 状态 + 下载（单击画面可隐藏） */}
      <motion.div
        aria-hidden={!chromeVisible}
        animate={chromeVisible ? { opacity: 1, y: 0 } : { opacity: 0, y: -10 }}
        transition={{ duration: DURATION.normal, ease: EASE.shutter }}
        className={cn(
          "absolute inset-x-0 top-0 flex items-center justify-between",
          "px-3 pt-[calc(env(safe-area-inset-top)+8px)] pb-4",
          "bg-gradient-to-b from-black/55 to-transparent",
          "pointer-events-none",
        )}
      >
        <MobileIconButton
          icon={<X className="w-5 h-5" />}
          label="关闭"
          variant="plain"
          onPress={close}
          tabIndex={chromeVisible ? undefined : -1}
          className="pointer-events-auto bg-black/55 border border-white/10 text-white"
        />
        <div className="pointer-events-none flex items-center gap-2 rounded-full border border-white/10 bg-black/50 px-3.5 py-2 font-mono text-[13px] text-white/85 tabular-nums">
          <span>{total > 1 ? `${idx + 1} / ${total}` : sourceLabel}</span>
          {isZoomed && (
            <>
              <span className="h-3 w-px bg-white/18" />
              <span>{zoomPercent}</span>
            </>
          )}
        </div>
        <MobileIconButton
          icon={
            downloadStatus === "downloading"
              ? <Spinner size={16} className="text-white" />
              : downloadStatus === "success"
                ? <Check className="w-5 h-5" />
                : downloadStatus === "error"
                  ? <AlertCircle className="w-5 h-5" />
                  : <Download className="w-5 h-5" />
          }
          label={downloadStatus === "downloading" ? "正在下载" : "下载原图"}
          variant="plain"
          onPress={handleDownload}
          disabled={downloadStatus === "downloading"}
          tabIndex={chromeVisible ? undefined : -1}
          className={cn(
            "pointer-events-auto w-11 h-11 inline-flex items-center justify-center",
            "rounded-full bg-black/55 border border-white/10 text-white",
            "active:scale-95 transition-transform",
          )}
        />
      </motion.div>

      {(actionNotice || boundaryHint) && (
        <motion.div
          key={actionNotice?.text ?? boundaryHint}
          initial={{ opacity: 0, y: -8, scale: 0.98 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          exit={{ opacity: 0, y: -8, scale: 0.98 }}
          transition={SPRING.snap}
          className={cn(
            "pointer-events-none absolute left-1/2 top-[calc(env(safe-area-inset-top)+4.25rem)]",
            "-translate-x-1/2 rounded-full border px-3 py-1.5",
            "bg-black/62 text-[12px] text-white/86 shadow-lg",
            actionNotice?.kind === "error" ? "border-red-400/35" : "border-white/12",
          )}
        >
          {boundaryHint === "first"
            ? "已经是第一张"
            : boundaryHint === "last"
              ? "已经是最后一张"
              : actionNotice?.text}
        </motion.div>
      )}

      {/* 左右切图按钮（可点；横滑也可） */}
      {total > 1 && (
        <>
          <motion.button
            type="button"
            onClick={() => goto(-1)}
            disabled={isFirst}
            tabIndex={chromeVisible ? undefined : -1}
            aria-hidden={!chromeVisible}
            aria-label="上一张"
            animate={chromeVisible ? { opacity: 1, x: 0 } : { opacity: 0, x: -8 }}
            transition={{ duration: DURATION.normal, ease: EASE.shutter }}
            className={cn(
              "absolute left-3 top-1/2 -translate-y-1/2 w-11 h-11",
              "inline-flex items-center justify-center rounded-full",
              "bg-black/50 border border-white/10 text-white",
              "disabled:opacity-25 active:scale-95 transition-transform",
              !chromeVisible && "pointer-events-none",
            )}
          >
            <ChevronLeft className="w-5 h-5" />
          </motion.button>
          <motion.button
            type="button"
            onClick={() => goto(1)}
            disabled={isLast}
            tabIndex={chromeVisible ? undefined : -1}
            aria-hidden={!chromeVisible}
            aria-label="下一张"
            animate={chromeVisible ? { opacity: 1, x: 0 } : { opacity: 0, x: 8 }}
            transition={{ duration: DURATION.normal, ease: EASE.shutter }}
            className={cn(
              "absolute right-3 top-1/2 -translate-y-1/2 w-11 h-11",
              "inline-flex items-center justify-center rounded-full",
              "bg-black/50 border border-white/10 text-white",
              "disabled:opacity-25 active:scale-95 transition-transform",
              !chromeVisible && "pointer-events-none",
            )}
          >
            <ChevronRight className="w-5 h-5" />
          </motion.button>
        </>
      )}

      {isZoomed && (
        <motion.button
          type="button"
          onClick={resetZoom}
          aria-label="重置缩放"
          initial={{ opacity: 0, y: -6 }}
          animate={{ opacity: chromeVisible ? 1 : 0.82, y: 0 }}
          exit={{ opacity: 0, y: -6 }}
          transition={{ duration: DURATION.normal, ease: EASE.shutter }}
          className={cn(
            "absolute left-1/2 top-[calc(env(safe-area-inset-top)+4rem)] -translate-x-1/2",
            "inline-flex h-9 items-center gap-1.5 rounded-full px-3",
            "border border-white/10 bg-black/55 text-xs font-mono text-white/82",
            " active:scale-95 transition-transform",
          )}
        >
          <RotateCcw className="h-3.5 w-3.5" />
          {zoomPercent}
        </motion.button>
      )}

      {/* 底部 prompt + 紧凑参数；上拉或点 Info 展开完整参数面板 */}
      <motion.div
        aria-hidden={!chromeVisible}
        animate={chromeVisible ? { opacity: 1, y: 0 } : { opacity: 0, y: 14 }}
        transition={{ duration: DURATION.normal, ease: EASE.shutter }}
        className={cn(
          "absolute inset-x-0 bottom-0 px-3 pt-6",
          "pb-[max(env(safe-area-inset-bottom),0.75rem)]",
          "bg-gradient-to-t from-black/65 via-black/30 to-transparent",
          "pointer-events-none",
          !chromeVisible && "pointer-events-none",
        )}
      >
        {total > 1 && (
          <div className="mx-auto mb-3.5 flex max-w-[34rem] gap-2.5 overflow-x-auto px-1 py-1 no-scrollbar pointer-events-auto">
            {thumbItems.map(({ item, itemIdx }) => {
              const active = item.id === current.id;
              return (
                <button
                  key={item.id}
                  ref={active ? activeThumbRef : undefined}
                  type="button"
                  onClick={() => {
                    if (item.id === current.id) return;
                    switchToItem(item);
                  }}
                  tabIndex={chromeVisible ? undefined : -1}
                  aria-label={`第 ${itemIdx + 1} 张`}
                  aria-current={active}
                  className={cn(
                    "relative h-12 w-12 shrink-0 overflow-hidden rounded-xl border",
                    "bg-black/45 shadow-sm transition-all duration-200",
                    active
                      ? "border-white ring-2 ring-[var(--color-lumen-amber)]/80 opacity-100 scale-105"
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
                  {active && (
                    <span
                      aria-hidden
                      className="absolute inset-x-1.5 bottom-1 h-[2px] rounded-full bg-[var(--color-lumen-amber)]"
                    />
                  )}
                </button>
              );
            })}
          </div>
        )}

        {/* 创作操作行：迭代 / 放大 / 重画 */}
        <div className="mx-auto mt-2 flex max-w-[34rem] justify-center gap-2.5">
          <button
            type="button"
            onClick={handleIterate}
            tabIndex={chromeVisible ? undefined : -1}
            className="pointer-events-auto inline-flex items-center gap-1.5 h-10 px-4 rounded-full bg-[rgba(242,169,58,0.2)] border border-[rgba(242,169,58,0.35)] text-[var(--amber-300)] text-[13px] font-medium active:scale-95 transition-transform"
          >
            <Pencil className="w-3.5 h-3.5" aria-hidden />
            迭代
          </button>
          <button
            type="button"
            onClick={handleUpscale}
            tabIndex={chromeVisible ? undefined : -1}
            className="pointer-events-auto inline-flex items-center gap-1.5 h-10 px-4 rounded-full bg-[rgba(242,169,58,0.2)] border border-[rgba(242,169,58,0.35)] text-[var(--amber-300)] text-[13px] font-medium active:scale-95 transition-transform"
          >
            <ArrowUpRight className="w-3.5 h-3.5" aria-hidden />
            放大
          </button>
          <button
            type="button"
            onClick={handleReroll}
            tabIndex={chromeVisible ? undefined : -1}
            className="pointer-events-auto inline-flex items-center gap-1.5 h-10 px-4 rounded-full bg-[rgba(242,169,58,0.2)] border border-[rgba(242,169,58,0.35)] text-[var(--amber-300)] text-[13px] font-medium active:scale-95 transition-transform"
          >
            <RefreshCw className="w-3.5 h-3.5" aria-hidden />
            重画
          </button>
        </div>

        {/* 辅助操作行：Prompt / 分享 / 参数 */}
        <div className="mx-auto mt-2 flex max-w-[34rem] justify-center gap-2.5">
          {current.prompt && (
            <button
              type="button"
              onClick={handleCopyPrompt}
              tabIndex={chromeVisible ? undefined : -1}
              className="pointer-events-auto inline-flex items-center gap-1.5 h-10 px-4 rounded-full bg-black/50 border border-white/12 text-white text-[13px] font-medium active:scale-95 transition-transform"
            >
              <Copy className="w-3.5 h-3.5" aria-hidden />
              Prompt
            </button>
          )}
          <button
            type="button"
            onClick={handleShare}
            tabIndex={chromeVisible ? undefined : -1}
            className="pointer-events-auto inline-flex items-center gap-1.5 h-10 px-4 rounded-full bg-black/50 border border-white/12 text-white text-[13px] font-medium active:scale-95 transition-transform"
          >
            <Share2 className="w-3.5 h-3.5" aria-hidden />
            分享
          </button>
          <button
            type="button"
            onClick={() => {
              setParamsOpen(true);
              setChromeVisible(true);
            }}
            tabIndex={chromeVisible ? undefined : -1}
            className="pointer-events-auto inline-flex items-center gap-1.5 h-10 px-4 rounded-full bg-black/50 border border-white/12 text-white text-[13px] font-medium active:scale-95 transition-transform"
          >
            <Info className="w-3.5 h-3.5" aria-hidden />
            参数
          </button>
        </div>
      </motion.div>

      <LightboxParamsPanel
        open={paramsOpen}
        onClose={() => {
          setParamsOpen(false);
          setChromeVisible(true);
          scheduleChromeHide();
        }}
        item={current}
        onCopyPrompt={current.prompt ? handleCopyPrompt : undefined}
      />
    </div>
  );
}
