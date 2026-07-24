"use client";

import { useEffect } from "react";

import { logWarn } from "@/lib/logger";

const SW_URL = "/sw.js";
const SW_SCOPE = "/";
// requestIdleCallback 兜底超时：弱网 / 低端机可能很久没空闲，宁可抢一点首屏
// 资源也不能让 SW 永远不注册。
const IDLE_TIMEOUT_MS = 4000;

// 在 production 注册 /sw.js；dev 主动 unregister 任何遗留 SW，避免 prod 装过
// PWA 的浏览器切到 localhost 调试时被旧 SW 拦截请求。
//
// SW 本身是 passthrough（见 public/sw.js），不提供离线缓存，也不是浏览器
// 安装应用的前置条件。这里保留注册仅用于已安装客户端的生命周期钩子和未来扩展。
export function ServiceWorkerRegister() {
  useEffect(() => {
    if (typeof navigator === "undefined" || !("serviceWorker" in navigator)) {
      return;
    }

    if (process.env.NODE_ENV !== "production") {
      // dev 兜底：清掉曾经注册过的 SW，避免污染 HMR / API 调试。
      void navigator.serviceWorker
        .getRegistrations()
        .then((regs) => Promise.all(regs.map((r) => r.unregister())))
        .catch(() => {
          /* dev 兜底，失败也无所谓 */
        });
      return;
    }

    let cancelled = false;
    let removeVisibilityListener: (() => void) | null = null;

    const register = async () => {
      if (cancelled) return;
      try {
        const reg = await navigator.serviceWorker.register(SW_URL, {
          scope: SW_SCOPE,
          // updateViaCache: 'none' 让浏览器即便给 sw.js 设了长缓存也强制回源
          // 校验，配合 next.config.ts 的 no-cache header 双保险。
          updateViaCache: "none",
        });
        if (cancelled) return;

        // 监听新版本：waiting 出现时让它立即激活。当前 SW 不缓存任何页面
        // 或静态资源，因此接管无需刷新正在创作的页面。
        const promoteWaiting = (worker: ServiceWorker | null) => {
          if (!worker) return;
          worker.addEventListener("statechange", () => {
            if (worker.state === "installed" && navigator.serviceWorker.controller) {
              worker.postMessage({ type: "SKIP_WAITING" });
            }
          });
        };
        if (reg.waiting && navigator.serviceWorker.controller) {
          reg.waiting.postMessage({ type: "SKIP_WAITING" });
        }
        reg.addEventListener("updatefound", () => promoteWaiting(reg.installing));

        // 页面回到前台时主动检查更新（用户长时间挂着 PWA 的常见情况）。
        const onVisibilityChange = () => {
          if (document.visibilityState === "visible") {
            reg.update().catch(() => {
              /* 网络抖动 / 离线，下次再试 */
            });
          }
        };
        document.addEventListener("visibilitychange", onVisibilityChange);
        removeVisibilityListener = () =>
          document.removeEventListener("visibilitychange", onVisibilityChange);
      } catch (error) {
        logWarn("service worker register failed", {
          scope: "pwa",
          extra: { message: error instanceof Error ? error.message : String(error) },
        });
      }
    };

    // 推迟到 idle，避免抢首屏 JS 解析；带超时兜底。
    const win = window as Window & {
      requestIdleCallback?: (
        cb: () => void,
        opts?: { timeout: number },
      ) => number;
      cancelIdleCallback?: (id: number) => void;
    };
    let idleId: number | null = null;
    let timeoutId: number | null = null;
    if (typeof win.requestIdleCallback === "function") {
      idleId = win.requestIdleCallback(register, { timeout: IDLE_TIMEOUT_MS });
    } else {
      timeoutId = window.setTimeout(register, 1500);
    }

    return () => {
      cancelled = true;
      if (idleId !== null && typeof win.cancelIdleCallback === "function") {
        win.cancelIdleCallback(idleId);
      }
      if (timeoutId !== null) {
        window.clearTimeout(timeoutId);
      }
      removeVisibilityListener?.();
      removeVisibilityListener = null;
    };
  }, []);
  return null;
}
