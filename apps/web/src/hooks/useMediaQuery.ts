"use client";

import { useCallback, useSyncExternalStore } from "react";

// cookie 名：上次访问解析过的视口断点（mobile/desktop），供服务端壳层读取。
const VIEWPORT_COOKIE = "lumen.viewport";
const MOBILE_QUERY = "(max-width: 767px)";

function writeViewportCookie(v: "mobile" | "desktop"): void {
  if (typeof document === "undefined") return;
  // 1 年；同源即可，不需要 secure 属性（仅给 SSR 提示）
  document.cookie = `${VIEWPORT_COOKIE}=${v}; max-age=31536000; path=/; SameSite=Lax`;
}

function readMediaQuery(query: string): boolean {
  if (typeof window === "undefined") return false;
  if (typeof window.matchMedia !== "function") {
    const width = window.innerWidth;
    const min = query.match(/\(\s*min-width\s*:\s*(\d+(?:\.\d+)?)px\s*\)/);
    const max = query.match(/\(\s*max-width\s*:\s*(\d+(?:\.\d+)?)px\s*\)/);
    if (min && width < Number(min[1])) return false;
    if (max && width > Number(max[1])) return false;
    return Boolean(min || max);
  }
  return window.matchMedia(query).matches;
}

/**
 * SSR 安全的 matchMedia hook。
 * 首次 hydration 使用 server snapshot = null；客户端路由切换时同步读取
 * matchMedia，避免每次切 Tab 都先进全屏骨架。
 * 典型用法：
 *   const isMobile = useMediaQuery("(max-width: 767px)");
 *   if (isMobile === null) return <Skeleton />;
 */
export function useMediaQuery(query: string): boolean | null {
  const subscribe = useCallback(
    (onStoreChange: () => void) =>
      subscribeMediaQuery(query, onStoreChange),
    [query],
  );
  const getSnapshot = useCallback(
    () => getMediaQuerySnapshot(query),
    [query],
  );
  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
}

/**
 * 移动端断点检测，使用 cookie 缓存上次结果，避免页面切换的骨架闪烁。
 * 第一次 hydration 仍返回 null；客户端路由切换同步读取当前断点。
 */
export function useIsMobile(): boolean | null {
  return useMediaQuery(MOBILE_QUERY);
}

function getServerSnapshot(): boolean | null {
  return null;
}

// useSyncExternalStore 要求 getSnapshot 在两次 publish 之间返回稳定引用，
// 因此用 module 级 cache 兜底；subscribe 时同步刷新 cache 再 onStoreChange，确保后续 read 读到新值。
const mediaQueryCache = new Map<string, boolean>();

function syncMediaQuerySnapshot(query: string, next: boolean): void {
  mediaQueryCache.set(query, next);
  if (query === MOBILE_QUERY) {
    writeViewportCookie(next ? "mobile" : "desktop");
  }
}

function getMediaQuerySnapshot(query: string): boolean | null {
  if (typeof window === "undefined") return null;
  const next = readMediaQuery(query);
  // Always reconcile against the live viewport. A cached desktop value can
  // otherwise survive a resize/navigation and keep the desktop shell mounted
  // on a narrow viewport until another MediaQueryList event happens to fire.
  mediaQueryCache.set(query, next);
  return next;
}

function subscribeMediaQuery(
  query: string,
  onStoreChange: () => void,
): () => void {
  if (typeof window === "undefined") return () => {};

  const publish = (next: boolean) => {
    syncMediaQuerySnapshot(query, next);
    onStoreChange();
  };

  if (typeof window.matchMedia !== "function") {
    const update = () => publish(readMediaQuery(query));
    // 订阅时立即同步一次 cache，避免首屏从 null 跳过初值。
    syncMediaQuerySnapshot(query, readMediaQuery(query));
    window.addEventListener("resize", update);
    return () => window.removeEventListener("resize", update);
  }

  const mql = window.matchMedia(query);
  // 订阅瞬间立即同步 cache（不触发 onStoreChange，让首次 getSnapshot 返回真值）。
  syncMediaQuerySnapshot(query, mql.matches);
  const update = () => publish(mql.matches);
  mql.addEventListener("change", update);
  return () => mql.removeEventListener("change", update);
}
