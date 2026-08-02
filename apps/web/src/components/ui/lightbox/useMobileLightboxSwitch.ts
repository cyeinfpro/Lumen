"use client";

import {
  useCallback,
  useEffect,
} from "react";
import type { Dispatch, RefObject, SetStateAction } from "react";

import {
  getPrivateIdentitySnapshot,
  isPrivateIdentitySnapshotCurrent,
} from "@/lib/auth/privateIdentityEpoch";
import {
  mobileLightboxOpenIdentity,
  mobileLightboxOpenStateMatchesIdentity,
  type MobileLightboxOpenState,
} from "./mobileLightboxIdentity";
import { type ImgStatus } from "./MobileLightboxViewTypes";
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

interface UseMobileLightboxSwitchOptions {
  state: MobileLightboxOpenState | null;
  setState: Dispatch<SetStateAction<MobileLightboxOpenState | null>>;
  switchSeqRef: RefObject<number>;
  preloadAbortRef: RefObject<AbortController | null>;
  fallbackItemIds: ReadonlySet<string>;
  setFallbackItemIds: Dispatch<SetStateAction<ReadonlySet<string>>>;
  stopSwipeAnimation: () => void;
  abortPreloads: () => void;
  resetMotion: () => void;
  setParamsOpen: Dispatch<SetStateAction<boolean>>;
  setChromeVisible: Dispatch<SetStateAction<boolean>>;
  resetMediaActions: () => void;
  setBoundaryHint: Dispatch<SetStateAction<"first" | "last" | null>>;
  setUseFallback: Dispatch<SetStateAction<boolean>>;
  setImgStatus: Dispatch<SetStateAction<ImgStatus>>;
  markItemFallback: (id: string) => void;
  replaceRef: RefObject<(id: string | null) => void>;
  clearBoundaryTimer: () => void;
  clearChromeTimer: () => void;
}

/**
 * 切图与开/关灯箱的事件接线:持有 switchSeq 防竞态,统一重置 motion/
 * preloads/chrome,并监听 lumen:open-lightbox / lumen:close-lightbox。
 * 从 MobileLightbox 拆出,保持组件在 React page/component 行数上限内。
 */
export function useMobileLightboxSwitch({
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
}: UseMobileLightboxSwitchOptions) {
  const switchToItem = useCallback(
    (nextItem: LightboxItem, options: { replaceUrl?: boolean } = {}) => {
      if (!state) return;
      const identity = mobileLightboxOpenIdentity(state);
      if (!isPrivateIdentitySnapshotCurrent(identity)) return;
      const replaceUrl = options.replaceUrl !== false;
      const seq = switchSeqRef.current + 1;
      switchSeqRef.current = seq;
      stopSwipeAnimation();
      abortPreloads();
      const preloadAbort = new AbortController();
      preloadAbortRef.current = preloadAbort;
      const knownFallback = fallbackItemIds.has(nextItem.id);
      const nextDisplayUrl = displayUrlForItem(nextItem, knownFallback);
      resetMotion();
      setParamsOpen(false);
      setChromeVisible(true);
      resetMediaActions();
      setBoundaryHint(null);
      setUseFallback(knownFallback);
      setImgStatus(isImageDecoded(nextDisplayUrl) ? "loaded" : "loading");
      setState((prev) =>
        prev && mobileLightboxOpenStateMatchesIdentity(prev, identity)
          ? { ...prev, currentId: nextItem.id }
          : prev,
      );
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
        if (!isPrivateIdentitySnapshotCurrent(identity)) return;
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
    // eslint-disable-next-line react-hooks/exhaustive-deps -- setter/ref 稳定,依赖数组与拆分前一致
    [
      abortPreloads,
      fallbackItemIds,
      markItemFallback,
      resetMediaActions,
      resetMotion,
      setBoundaryHint,
      setChromeVisible,
      state,
      stopSwipeAnimation,
    ],
  );

  // —— event listener：按依赖更新，避免 handler 读旧状态 ——
  useEffect(() => {
    const onOpen = (e: Event) => {
      const ce = e as CustomEvent<OpenLightboxDetail>;
      const detail = ce.detail;
      if (!detail || !detail.items || detail.items.length === 0) return;
      const currentIdentity = getPrivateIdentitySnapshot();
      const identity =
        detail.ownerUserId && detail.identityEpoch !== undefined
          ? {
              userId: detail.ownerUserId,
              epoch: detail.identityEpoch,
            }
          : currentIdentity;
      if (!isPrivateIdentitySnapshotCurrent(identity)) return;
      const initialId =
        detail.items.find((x) => x.id === detail.initialId)?.id ??
        detail.items[0].id;
      const initialItem =
        detail.items.find((x) => x.id === initialId) ?? detail.items[0];
      const knownFallback = fallbackItemIds.has(initialId);
      switchSeqRef.current += 1;
      stopSwipeAnimation();
      abortPreloads();
      clearBoundaryTimer();
      setState({
        ownerUserId: identity.userId!,
        identityEpoch: identity.epoch,
        items: detail.items,
        currentId: initialId,
      });
      setParamsOpen(false);
      setChromeVisible(true);
      resetMotion();
      setImgStatus(
        isImageDecoded(displayUrlForItem(initialItem, knownFallback))
          ? "loaded"
          : "loading",
      );
      setUseFallback(knownFallback);
      resetMediaActions();
      setBoundaryHint(null);
      replaceRef.current(initialId);
    };
    const onCloseEvt = () => {
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
      setFallbackItemIds(new Set());
      replaceRef.current(null);
    };
    window.addEventListener(OPEN_EVENT, onOpen as EventListener);
    window.addEventListener(CLOSE_EVENT, onCloseEvt);
    return () => {
      window.removeEventListener(OPEN_EVENT, onOpen as EventListener);
      window.removeEventListener(CLOSE_EVENT, onCloseEvt);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- setter/ref 稳定,依赖数组与拆分前一致
  }, [
    abortPreloads,
    clearBoundaryTimer,
    clearChromeTimer,
    fallbackItemIds,
    resetMediaActions,
    resetMotion,
    setBoundaryHint,
    setChromeVisible,
    stopSwipeAnimation,
  ]);

  useEffect(() => {
    return () => {
      clearBoundaryTimer();
      stopSwipeAnimation();
      abortPreloads();
    };
  }, [
    abortPreloads,
    clearBoundaryTimer,
    stopSwipeAnimation,
  ]);

  return { switchToItem };
}
