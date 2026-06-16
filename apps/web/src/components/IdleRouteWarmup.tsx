"use client";

import { useEffect } from "react";
import { usePathname, useRouter } from "next/navigation";

const HOT_ROUTES = [
  "/",
  "/projects",
  "/library",
  "/settings/usage",
  "/admin",
] as const;

type IdleWindow = {
  requestIdleCallback?: (
    callback: () => void,
    options?: { timeout?: number },
  ) => number;
  cancelIdleCallback?: (handle: number) => void;
};

function shouldSkipWarmup() {
  const nav = navigator as Navigator & {
    connection?: { saveData?: boolean; effectiveType?: string };
  };
  if (nav.connection?.saveData) return true;
  return nav.connection?.effectiveType === "2g";
}

export function IdleRouteWarmup() {
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    if (shouldSkipWarmup()) return;

    let cancelled = false;
    const idleWindow = window as unknown as IdleWindow;
    const requestIdleCallback = idleWindow.requestIdleCallback;
    const cancelIdleCallback = idleWindow.cancelIdleCallback;
    const run = () => {
      if (cancelled) return;
      for (const route of HOT_ROUTES) {
        if (route === pathname) continue;
        try {
          router.prefetch(route);
        } catch {
          // Prefetch is opportunistic; navigation must not depend on it.
        }
      }
    };

    const handle =
      typeof requestIdleCallback === "function"
      ? requestIdleCallback(run, { timeout: 2_000 })
      : window.setTimeout(run, 900);

    return () => {
      cancelled = true;
      if (
        typeof requestIdleCallback === "function" &&
        typeof cancelIdleCallback === "function"
      ) {
        cancelIdleCallback(handle);
      } else {
        window.clearTimeout(handle);
      }
    };
  }, [pathname, router]);

  return null;
}
