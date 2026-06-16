"use client";

import { usePathname } from "next/navigation";
import { useEffect, useLayoutEffect, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";

import { getMe, type AuthUser } from "@/lib/apiClient";
import { isPublicPath } from "@/lib/auth/publicPaths";
import { useChatStore } from "@/store/useChatStore";

export type RuntimeDefaults = {
  fast?: boolean;
  upload_max_source_bytes?: number;
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
  return next;
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
  const isPublicAuthPath = isPublicPath(pathname);
  const defaultFast = defaults.fast;
  const defaultUploadMaxSourceBytes = defaults.upload_max_source_bytes;

  const initialDefaults = useMemo(
    () =>
      pickRuntimeDefaults({
        fast: defaultFast,
        upload_max_source_bytes: defaultUploadMaxSourceBytes,
      }),
    [defaultFast, defaultUploadMaxSourceBytes],
  );

  useLayoutEffect(() => {
    useChatStore.getState().applyRuntimeDefaults(initialDefaults);
  }, [initialDefaults]);

  const meQuery = useQuery<AuthUser>({
    queryKey: ["me"],
    queryFn: getMe,
    retry: false,
    staleTime: 5 * 60_000,
    refetchOnMount: false,
    refetchOnWindowFocus: false,
    enabled: !isPublicAuthPath,
  });
  const serverRuntimeDefaults = meQuery.data?.runtime_defaults;
  const serverFast = serverRuntimeDefaults?.fast;
  const serverUploadMaxSourceBytes = serverRuntimeDefaults?.upload_max_source_bytes;

  const runtimeDefaults = useMemo(
    () =>
      pickRuntimeDefaults({
        fast: serverFast,
        upload_max_source_bytes: serverUploadMaxSourceBytes,
      }),
    [serverFast, serverUploadMaxSourceBytes],
  );

  useLayoutEffect(() => {
    if (!meQuery.data) return;
    useChatStore.getState().applyRuntimeDefaults(runtimeDefaults);
  }, [meQuery.data, runtimeDefaults]);

  useEffect(() => {
    if (!meQuery.data) return;
    writeRuntimeDefaultsCookie(runtimeDefaults);
  }, [meQuery.data, runtimeDefaults]);

  return null;
}
