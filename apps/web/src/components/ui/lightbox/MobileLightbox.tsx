"use client";

// MobileLightbox —— 极简全屏看图器（2026-04-24 重构）。
//
// 设计原则：
//   1. 按需 mount：state 为空时整棵子树不渲染。
//   2. 单一状态源：内部 state 为准；URL ?img=<id> 只做前向 replace + 反向
//      同步（渲染期 diff，不用 effect setState），不用 router.back。
//      state 为空时 URL 出现 ?img（深链直达 / 前进后退）→ 从 chat store
//      取图单图重开，实现完整深链闭环。
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
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import { flushSync } from "react-dom";

import { useChatStore } from "@/store/useChatStore";
import { useUiStore } from "@/store/useUiStore";
import { imageResultToLightboxItem } from "@/lib/imageResultLightbox";
import { pushMobileToast } from "@/components/ui/primitives/mobile";
import {
  getPrivateIdentitySnapshot,
  isPrivateIdentitySnapshotCurrent,
} from "@/lib/auth/privateIdentityEpoch";
import { useInpaintStore } from "@/store/useInpaintStore";
import { useLightboxGestures } from "./LightboxGestures";
import { MobileLightboxView, type ImgStatus } from "./MobileLightboxView";
import {
  mobileLightboxThumbnailItems,
  mobileLightboxVisibleSlides,
} from "./mobileLightboxCollections";
import {
  currentMobileLightboxIdentity,
  mobileLightboxOpenIdentity,
  mobileLightboxOpenStateMatchesIdentity,
  type MobileLightboxOpenState,
} from "./mobileLightboxIdentity";
import { preloadImage, preloadLightboxItem } from "./mobileLightboxMedia";
import { useMobileLightboxChrome } from "./useMobileLightboxChrome";
import { useMobileLightboxDialog } from "./useMobileLightboxDialog";
import { useMobileLightboxMediaActions } from "./useMobileLightboxMediaActions";
import { useMobileLightboxSwitch } from "./useMobileLightboxSwitch";
import { type LightboxItem } from "./types";

type MotionPlayback = {
  stop: () => void;
  then: (onResolve: () => void) => Promise<void>;
};

const _subscribeNoop = () => () => {};
const _getClientSnapshot = () => true;
const _getServerSnapshot = () => false;
const PRELOAD_NEIGHBOR_RADIUS = 2;
const EMPTY_LIGHTBOX_ITEMS: LightboxItem[] = [];

export function MobileLightbox() {
  const searchParams = useSearchParams();
  // 订阅 useUiStore.lightbox.action：dialog 模式下「设为当前模特」等附加按钮。
  // MobileLightbox 自身仍以本地 OpenState 作为 source of truth，因此这里只读 action。
  const lightboxAction = useUiStore((s) => s.lightbox.action);

  const [state, setState] = useState<MobileLightboxOpenState | null>(null);
  const [paramsOpen, setParamsOpen] = useState(false);
  const [imgStatus, setImgStatus] = useState<ImgStatus>("loading");
  const [useFallback, setUseFallback] = useState(false);
  const gestureTargetRef = useRef<HTMLDivElement | null>(null);
  const downloadAnchorRef = useRef<HTMLAnchorElement | null>(null);
  const items = state?.items ?? EMPTY_LIGHTBOX_ITEMS;
  const idx = state
    ? items.findIndex((item) => item.id === state.currentId)
    : -1;
  const current = idx >= 0 ? items[idx] : null;
  const total = items.length;
  const isFirst = idx <= 0;
  const isLast = idx < 0 || idx === total - 1;
  const mediaIdentity = currentMobileLightboxIdentity(state);
  const {
    downloadStatus,
    actionNotice,
    resetMediaActions,
    handleDownload,
    handleCopyPrompt,
    handleShare,
  } = useMobileLightboxMediaActions({
    current,
    ownerUserId: mediaIdentity.userId,
    identityEpoch: mediaIdentity.epoch,
    downloadAnchorRef,
  });
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
  const [zoomLevel, setZoomLevel] = useState(1);
  const [fallbackItemIds, setFallbackItemIds] = useState<ReadonlySet<string>>(
    () => new Set(),
  );
  const activeThumbRef = useRef<HTMLButtonElement | null>(null);
  const switchSeqRef = useRef(0);
  const preloadAbortRef = useRef<AbortController | null>(null);
  const neighborPreloadAbortRef = useRef<AbortController | null>(null);
  const swipeAnimationRef = useRef<MotionPlayback | null>(null);

  const resetMotion = useCallback(() => {
    dragX.set(0);
    dragY.set(0);
    scale.set(1);
    haloOpacity.set(1);
    setZoomLevel(1);
  }, [dragX, dragY, scale, haloOpacity]);

  const {
    chromeVisible,
    setChromeVisible,
    boundaryHint,
    setBoundaryHint,
    clearChromeTimer,
    clearBoundaryTimer,
    showBoundaryHint,
    scheduleChromeHide,
    handlePointerActivity,
    resetZoom,
    handleCloseParams: showChromeAfterClosingParams,
  } = useMobileLightboxChrome({
    openCurrentId: state?.currentId ?? null,
    imgStatus,
    paramsOpen,
    resetMotion,
  });

  const stopSwipeAnimation = useCallback(() => {
    swipeAnimationRef.current?.stop();
    swipeAnimationRef.current = null;
  }, []);

  const abortPreloads = useCallback(() => {
    preloadAbortRef.current?.abort();
    preloadAbortRef.current = null;
    neighborPreloadAbortRef.current?.abort();
    neighborPreloadAbortRef.current = null;
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

  const { switchToItem } = useMobileLightboxSwitch({
    state,
    setState,
    switchSeqRef,
    preloadAbortRef,
    fallbackItemIds,
    setFallbackItemIds,
    stopSwipeAnimation,
    abortPreloads,
    resetMotion,
    setParamsOpen,
    setChromeVisible,
    resetMediaActions,
    setBoundaryHint,
    setUseFallback,
    setImgStatus,
    markItemFallback,
    replaceRef,
    clearBoundaryTimer,
    clearChromeTimer,
  });

  // 深链重开用的图数据：state 为空且 URL 带 ?img 时订阅目标图，
  // 数据到达（首屏 history 加载完 / 生成完成）→ 组件重渲染 → 下面 effect 重跑 → 开灯箱。
  // 未命中时 selector 恒定返回 undefined，避免随 store 全量 churn 重渲染。
  const deepLinkImage = useChatStore((s) =>
    urlImg && !state ? s.imagesById[urlImg] : undefined,
  );
  const deepLinkGeneration = useChatStore((s) =>
    deepLinkImage ? s.generations[deepLinkImage.from_generation_id] : undefined,
  );

  // —— URL → state 反向同步：只在客户端 effect 同步，避免 SSR/渲染期 setState ——
  // state 为空时 URL 出现 ?img（深链直达、前进后退回到带 ?img 的条目）也视为开灯箱
  // 意图：从 chat store 取该图，以单图重开（与 CanvasNodesPreview 的单图打开一致）。
  useEffect(() => {
    if (urlImg === prevUrlImg) return;
    let canceled = false;
    const identity = state ? mobileLightboxOpenIdentity(state) : null;
    queueMicrotask(() => {
      if (canceled) return;
      if (identity && !isPrivateIdentitySnapshotCurrent(identity)) return;
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
      if (urlImg && !state) {
        // 深链重开：灯箱关闭时 ?img 出现。数据未就绪则保持 pending
        // （不推进 prevUrlImg），deepLinkImage/deepLinkGeneration 就绪后
        // effect 因依赖变化重跑，这里再打开。
        const image = deepLinkImage;
        const generation = deepLinkGeneration;
        if (!image || !generation) return;
        const currentIdentity = getPrivateIdentitySnapshot();
        if (!currentIdentity.userId) return;
        const item = imageResultToLightboxItem(generation, image, {
          createdAt: generation.finished_at ?? generation.started_at,
        });
        switchSeqRef.current += 1;
        stopSwipeAnimation();
        abortPreloads();
        clearBoundaryTimer();
        setState({
          ownerUserId: currentIdentity.userId,
          identityEpoch: currentIdentity.epoch,
          items: [item],
          currentId: urlImg,
        });
        setPrevUrlImg(urlImg);
        setParamsOpen(false);
        setChromeVisible(true);
        resetMotion();
        setImgStatus("loading");
        setUseFallback(false);
        resetMediaActions();
        setBoundaryHint(null);
        return;
      }
      setPrevUrlImg(urlImg);
      setParamsOpen(false);
      setChromeVisible(true);
      resetMotion();
      setImgStatus("loading");
      setUseFallback(false);
      resetMediaActions();
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
  }, [
    abortPreloads,
    clearBoundaryTimer,
    deepLinkGeneration,
    deepLinkImage,
    prevUrlImg,
    resetMediaActions,
    resetMotion,
    setBoundaryHint,
    setChromeVisible,
    state,
    stopSwipeAnimation,
    switchToItem,
    urlImg,
  ]);

  // —— 切图 ——
  const goto = useCallback(
    (delta: 1 | -1) => {
      if (!state) return;
      if (!isPrivateIdentitySnapshotCurrent(mobileLightboxOpenIdentity(state)))
        return;
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
      const identity = mobileLightboxOpenIdentity(state);
      if (!isPrivateIdentitySnapshotCurrent(identity)) return false;
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
          !isPrivateIdentitySnapshotCurrent(identity) ||
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
    const identity = state ? mobileLightboxOpenIdentity(state) : null;
    switchSeqRef.current += 1;
    stopSwipeAnimation();
    abortPreloads();
    clearBoundaryTimer();
    setState(null);
    setParamsOpen(false);
    setChromeVisible(true);
    clearChromeTimer();
    resetMotion();
    setImgStatus("loading");
    setUseFallback(false);
    resetMediaActions();
    setBoundaryHint(null);
    replaceRef.current(null);
    // 同步清空 store：openLightboxFromItems 写入了 open=true / action，
    // 仅清本地 state 会让 MobileTabBar（订阅 lightbox.open）持续隐藏，
    // 下次开 lightbox 还会带出旧 action。store setState 幂等，无回环风险。
    if (identity && isPrivateIdentitySnapshotCurrent(identity)) {
      useUiStore.getState().closeLightbox(identity);
    }
  }, [
    abortPreloads,
    clearBoundaryTimer,
    clearChromeTimer,
    resetMediaActions,
    resetMotion,
    setBoundaryHint,
    setChromeVisible,
    state,
    stopSwipeAnimation,
  ]);

  const isOpen = state !== null;
  const handleCloseParams = useCallback(() => {
    setParamsOpen(false);
    showChromeAfterClosingParams();
  }, [showChromeAfterClosingParams]);
  const { dialogRootRef, closeButtonRef, dialogTitleId } =
    useMobileLightboxDialog({
      open: isOpen,
      paramsOpen,
      onClose: close,
      onGoto: goto,
      onCloseParams: handleCloseParams,
    });

  const handleIterate = useCallback(() => {
    if (!state) return;
    if (!isPrivateIdentitySnapshotCurrent(mobileLightboxOpenIdentity(state)))
      return;
    const id = state.currentId;
    const img = useChatStore.getState().imagesById[id];
    if (!img) return;
    close();
    useChatStore.getState().promoteImageToReference(id);
    pushMobileToast("已设为参考图，可继续迭代", "success");
  }, [state, close]);

  const handleUpscale = useCallback(() => {
    if (!state) return;
    if (!isPrivateIdentitySnapshotCurrent(mobileLightboxOpenIdentity(state)))
      return;
    const id = state.currentId;
    close();
    void useChatStore.getState().upscaleImage(id);
    pushMobileToast("中等质量放大中…", "success");
  }, [state, close]);

  const handleReroll = useCallback(() => {
    if (!state) return;
    if (!isPrivateIdentitySnapshotCurrent(mobileLightboxOpenIdentity(state)))
      return;
    const id = state.currentId;
    close();
    void useChatStore.getState().rerollImage(id);
    pushMobileToast("重新生成中…", "success");
  }, [state, close]);

  const handleInpaint = useCallback(() => {
    if (!state) return;
    if (!isPrivateIdentitySnapshotCurrent(mobileLightboxOpenIdentity(state)))
      return;
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

  useEffect(() => {
    if (!current || idx < 0 || !state) return;
    const identity = mobileLightboxOpenIdentity(state);
    if (!isPrivateIdentitySnapshotCurrent(identity)) return;
    const seq = switchSeqRef.current;
    const controller = new AbortController();
    neighborPreloadAbortRef.current?.abort();
    neighborPreloadAbortRef.current = controller;
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
      const warm = knownFallback
        ? preloadImage(item.url, controller.signal).then(() => true)
        : preloadLightboxItem(item, controller.signal);

      void warm
        .then((usedFallback) => {
          if (disposed || !isPrivateIdentitySnapshotCurrent(identity)) {
            return;
          }
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
      if (neighborPreloadAbortRef.current === controller) {
        neighborPreloadAbortRef.current = null;
      }
    };
  }, [current, fallbackItemIds, idx, items, markItemFallback, state]);

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

  const thumbItems = useMemo(
    () => mobileLightboxThumbnailItems(items, idx, total),
    [idx, items, total],
  );
  const visibleSlides = useMemo(
    () => mobileLightboxVisibleSlides(items, current, idx, total),
    [current, idx, items, total],
  );
  const guardedLightboxAction = useMemo(() => {
    if (!lightboxAction || !state) return null;
    const identity = mobileLightboxOpenIdentity(state);
    return {
      ...lightboxAction,
      onClick: (item: LightboxItem) => {
        const currentLightbox = useUiStore.getState().lightbox;
        if (
          !isPrivateIdentitySnapshotCurrent(identity) ||
          !mobileLightboxOpenStateMatchesIdentity(state, identity) ||
          currentLightbox.ownerUserId !== identity.userId ||
          currentLightbox.identityEpoch !== identity.epoch ||
          state.currentId !== item.id
        ) {
          return;
        }
        lightboxAction.onClick(item);
      },
    };
  }, [lightboxAction, state]);

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
      lightboxAction={guardedLightboxAction}
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
      onCloseParams={handleCloseParams}
    />
  );
}
