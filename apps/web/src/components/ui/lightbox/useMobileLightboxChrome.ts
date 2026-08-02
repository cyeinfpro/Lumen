"use client";

import {
  type Dispatch,
  type SetStateAction,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

import type { ImgStatus } from "./MobileLightboxView";

const CHROME_HIDE_MS = 2600;
const CHROME_ACTIVITY_THROTTLE_MS = 320;

interface UseMobileLightboxChromeOptions {
  openCurrentId: string | null;
  imgStatus: ImgStatus;
  paramsOpen: boolean;
  resetMotion: () => void;
}

interface UseMobileLightboxChromeResult {
  chromeVisible: boolean;
  setChromeVisible: Dispatch<SetStateAction<boolean>>;
  boundaryHint: "first" | "last" | null;
  setBoundaryHint: Dispatch<SetStateAction<"first" | "last" | null>>;
  clearChromeTimer: () => void;
  clearBoundaryTimer: () => void;
  showBoundaryHint: (edge: "first" | "last") => void;
  scheduleChromeHide: () => void;
  handlePointerActivity: () => void;
  resetZoom: () => void;
  handleCloseParams: () => void;
}

export function useMobileLightboxChrome({
  openCurrentId,
  imgStatus,
  paramsOpen,
  resetMotion,
}: UseMobileLightboxChromeOptions): UseMobileLightboxChromeResult {
  const [chromeVisible, setChromeVisible] = useState(true);
  const [boundaryHint, setBoundaryHint] = useState<"first" | "last" | null>(
    null,
  );
  const chromeTimerRef = useRef<number | null>(null);
  const boundaryTimerRef = useRef<number | null>(null);
  const chromeVisibleRef = useRef(true);
  const imgStatusRef = useRef<ImgStatus>("loading");
  const paramsOpenRef = useRef(false);
  const lastChromeActivityRef = useRef(0);

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

  const clearBoundaryTimer = useCallback(() => {
    if (boundaryTimerRef.current !== null) {
      window.clearTimeout(boundaryTimerRef.current);
      boundaryTimerRef.current = null;
    }
  }, []);

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

  const handleCloseParams = useCallback(() => {
    setChromeVisible(true);
    scheduleChromeHide();
  }, [scheduleChromeHide]);

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

  return {
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
    handleCloseParams,
  };
}
