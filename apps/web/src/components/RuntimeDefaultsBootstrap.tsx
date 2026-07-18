"use client";

import { usePathname, useRouter } from "next/navigation";
import { useEffect, useLayoutEffect, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";

import { getMe, type AuthUser } from "@/lib/apiClient";
import { isPublicPath } from "@/lib/auth/publicPaths";
import { AUTH_USER_QUERY_KEY } from "@/components/QueryProvider";
import {
  getRedirectForHiddenNavPath,
  normalizeNavVisibility,
  type NavVisibility,
} from "@/components/ui/shell/navigation";
import { useIdentityRevalidation } from "@/components/useIdentityRevalidation";
import { useChatStore } from "@/store/useChatStore";
import { useUiStore } from "@/store/useUiStore";

export type RuntimeDefaults = {
  fast?: boolean;
  upload_max_source_bytes?: number;
  canvas_enabled?: boolean;
  nav_visibility?: NavVisibility;
};

const RUNTIME_DEFAULTS_COOKIE = "lumen_runtime_defaults_v1";
const RUNTIME_DEFAULTS_COOKIE_MAX_AGE_S = 5 * 60;

function pickRuntimeDefaults(
  value: RuntimeDefaults | undefined | null,
): RuntimeDefaults {
  const next: RuntimeDefaults = {};
  if (typeof value?.fast === "boolean") next.fast = value.fast;
  if (
    typeof value?.upload_max_source_bytes === "number" &&
    Number.isFinite(value.upload_max_source_bytes) &&
    value.upload_max_source_bytes > 0
  ) {
    next.upload_max_source_bytes = value.upload_max_source_bytes;
  }
  if (typeof value?.canvas_enabled === "boolean") {
    next.canvas_enabled = value.canvas_enabled;
  }
  if (value?.nav_visibility && typeof value.nav_visibility === "object") {
    next.nav_visibility = normalizeNavVisibility(value.nav_visibility);
  }
  return next;
}

function applyRuntimeDefaultsToStores(defaults: RuntimeDefaults): void {
  useChatStore.getState().applyRuntimeDefaults(defaults);
  useUiStore.getState().setNavVisibility(defaults.nav_visibility);
  useUiStore.getState().setCanvasEnabled(defaults.canvas_enabled === true);
}

function writeRuntimeDefaultsCookie(defaults: RuntimeDefaults) {
  try {
    const payload = encodeURIComponent(JSON.stringify(defaults));
    document.cookie = `${RUNTIME_DEFAULTS_COOKIE}=${payload}; Max-Age=${RUNTIME_DEFAULTS_COOKIE_MAX_AGE_S}; Path=/; SameSite=Lax`;
  } catch {
    // Cookie warm cache is best-effort; React Query remains authoritative.
  }
}

// SSR 阶段把 layout.tsx 抓到的 defaults 同步到 store（首屏不闪烁），
// 客户端再订阅 me query 持续同步运行时变化（管理员后台改 generation.fast_default 后无须刷新）。
// 之前由 DesktopStudio / MobileStudio 各自 useEffect 重复调用，现统一在此处。
// 公开页面（/login、/reset-password、/invite/*）禁用 me 拉取，避免未登录用户在登陆页打 401。
export function RuntimeDefaultsBootstrap({
  defaults,
}: {
  defaults: RuntimeDefaults;
}) {
  const pathname = usePathname();
  const router = useRouter();
  const isPublicAuthPath = isPublicPath(pathname);
  const defaultFast = defaults.fast;
  const defaultUploadMaxSourceBytes = defaults.upload_max_source_bytes;
  const defaultCanvasEnabled = defaults.canvas_enabled;
  const defaultNavVisibility = defaults.nav_visibility;

  const initialDefaults = useMemo(
    () =>
      pickRuntimeDefaults({
        fast: defaultFast,
        upload_max_source_bytes: defaultUploadMaxSourceBytes,
        canvas_enabled: defaultCanvasEnabled,
        nav_visibility: defaultNavVisibility,
      }),
    [
      defaultFast,
      defaultUploadMaxSourceBytes,
      defaultCanvasEnabled,
      defaultNavVisibility,
    ],
  );

  useLayoutEffect(() => {
    applyRuntimeDefaultsToStores(initialDefaults);
  }, [initialDefaults]);

  const meQuery = useQuery<AuthUser>({
    queryKey: AUTH_USER_QUERY_KEY,
    queryFn: getMe,
    retry: false,
    networkMode: "online",
    refetchOnReconnect: false,
    staleTime: 5 * 60_000,
    refetchOnMount: false,
    refetchOnWindowFocus: false,
    enabled: !isPublicAuthPath,
  });
  const { identityUnavailable } = useIdentityRevalidation({
    isPublicAuthPath,
    query: meQuery,
  });
  const serverRuntimeDefaults = meQuery.data?.runtime_defaults;
  const serverFast = serverRuntimeDefaults?.fast;
  const serverUploadMaxSourceBytes =
    serverRuntimeDefaults?.upload_max_source_bytes;
  const serverCanvasEnabled = serverRuntimeDefaults?.canvas_enabled;
  const serverNavVisibility = serverRuntimeDefaults?.nav_visibility;

  const runtimeDefaults = useMemo(
    () =>
      pickRuntimeDefaults({
        fast: serverFast,
        upload_max_source_bytes: serverUploadMaxSourceBytes,
        canvas_enabled: serverCanvasEnabled,
        nav_visibility: serverNavVisibility,
      }),
    [
      serverFast,
      serverUploadMaxSourceBytes,
      serverCanvasEnabled,
      serverNavVisibility,
    ],
  );

  useLayoutEffect(() => {
    if (!meQuery.data || identityUnavailable) return;
    useChatStore.getState().setCurrentUser(meQuery.data.id);
    applyRuntimeDefaultsToStores(runtimeDefaults);
  }, [identityUnavailable, meQuery.data, runtimeDefaults]);

  useEffect(() => {
    if (!meQuery.data || identityUnavailable) return;
    writeRuntimeDefaultsCookie(runtimeDefaults);
  }, [identityUnavailable, meQuery.data, runtimeDefaults]);

  const effectiveNavVisibility =
    meQuery.data && !identityUnavailable && runtimeDefaults.nav_visibility
      ? runtimeDefaults.nav_visibility
      : initialDefaults.nav_visibility;

  useEffect(() => {
    if (isPublicAuthPath) return;
    const redirectTo = getRedirectForHiddenNavPath(
      pathname,
      effectiveNavVisibility,
    );
    if (!redirectTo || redirectTo === pathname) return;
    router.replace(redirectTo);
  }, [effectiveNavVisibility, isPublicAuthPath, pathname, router]);

  useEffect(() => {
    if (
      !meQuery.data ||
      identityUnavailable ||
      runtimeDefaults.canvas_enabled === true ||
      !pathname.startsWith("/projects/canvas")
    ) {
      return;
    }
    router.replace("/projects");
  }, [
    identityUnavailable,
    meQuery.data,
    pathname,
    router,
    runtimeDefaults.canvas_enabled,
  ]);

  return null;
}
