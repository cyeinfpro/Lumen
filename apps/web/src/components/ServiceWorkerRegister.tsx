"use client";

import { useEffect } from "react";

import { logWarn } from "@/lib/logger";

const SW_URL = "/sw.js";
const SW_SCOPE = "/";
// requestIdleCallback 兜底超时：弱网 / 低端机可能很久没空闲，宁可抢一点首屏
// 资源也不能让 SW 永远不注册（会破坏 PWA installability）。
const IDLE_TIMEOUT_MS = 4000;

// 在 production 注册 /sw.js；dev 主动 unregister 任何遗留 SW，避免 prod 装过
// PWA 的浏览器切到 localhost 调试时被旧 SW 拦截请求。
//
// SW 本身是 passthrough（见 public/sw.js），存在的唯一目的是满足 PWA
// installability，让浏览器允许"添加到主屏"。
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

    const onControllerChange = () => {
      // 新 SW 接管页面 → 重新加载，让用户拿到与新 SW 一致的资源版本。
      // 避免在已经 reload 过的页面再 reload（某些极端场景会循环）。
      if (sessionStorage.getItem("__lumen_sw_reloaded") === "1") return;
      sessionStorage.setItem("__lumen_sw_reloaded", "1");
      window.location.reload();
    };

    const register = async () => {
      if (cancelled) return;
      try {
        const reg = await navigator.serviceWorker.register(SW_URL, {
          scope: SW_SCOPE,
          // updateViaCache: 'none' 让浏览器即便给 sw.js 设了长缓存也强制回源
          // 校验，配合 next.config.ts 的 no-cache header 双保险。
          updateViaCache: "none",
        });

        // 监听新版本：waiting 出现时让它立即激活，触发 controllerchange。
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

        navigator.serviceWorker.addEventListener(
          "controllerchange",
          onControllerChange,
        );

        // 页面回到前台时主动检查更新（用户长时间挂着 PWA 的常见情况）。
        document.addEventListener("visibilitychange", () => {
          if (document.visibilityState === "visible") {
            reg.update().catch(() => {
              /* 网络抖动 / 离线，下次再试 */
            });
          }
        });
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
      navigator.serviceWorker.removeEventListener(
        "controllerchange",
        onControllerChange,
      );
    };
  }, []);
  return null;
}
