"use client";

import { usePathname } from "next/navigation";
import { useLayoutEffect } from "react";
import { useQuery } from "@tanstack/react-query";

import { getMe, type AuthUser } from "@/lib/apiClient";
import { isPublicPath } from "@/lib/auth/publicPaths";
import { useChatStore } from "@/store/useChatStore";

export type RuntimeDefaults = {
  fast?: boolean;
};

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

  useLayoutEffect(() => {
    useChatStore.getState().applyRuntimeDefaults({ fast: defaults.fast });
  }, [defaults.fast]);

  const meQuery = useQuery<AuthUser>({
    queryKey: ["me"],
    queryFn: getMe,
    retry: false,
    staleTime: 60_000,
    enabled: !isPublicAuthPath,
  });
  const fast = meQuery.data?.runtime_defaults?.fast;
  useLayoutEffect(() => {
    if (typeof fast !== "boolean") return;
    useChatStore.getState().applyRuntimeDefaults({ fast });
  }, [fast]);

  return null;
}
