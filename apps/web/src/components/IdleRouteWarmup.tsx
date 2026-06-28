"use client";

import { useEffect } from "react";
import { usePathname, useRouter } from "next/navigation";

import { getAppNavItems } from "@/components/ui/shell/navigation";
import { useUiStore } from "@/store/useUiStore";

const STATIC_HOT_ROUTES = ["/settings/usage", "/admin"] as const;

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
  const navVisibility = useUiStore((s) => s.navVisibility);

  useEffect(() => {
    if (shouldSkipWarmup()) return;

    let cancelled = false;
    const idleWindow = window as unknown as IdleWindow;
    const requestIdleCallback = idleWindow.requestIdleCallback;
    const cancelIdleCallback = idleWindow.cancelIdleCallback;
    const run = () => {
      if (cancelled) return;
      const hotRoutes = [
        ...getAppNavItems(navVisibility).map((item) => item.route),
        ...STATIC_HOT_ROUTES,
      ];
      for (const route of hotRoutes) {
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
  }, [navVisibility, pathname, router]);

  return null;
}
