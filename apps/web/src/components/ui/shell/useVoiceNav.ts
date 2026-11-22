"use client";

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";

import { apiFetch } from "@/lib/api/http";
import { isPrivateIdentitySnapshotCurrent } from "@/lib/auth/privateIdentityEpoch";
import { userScopedQueryKey } from "@/lib/queries/userScope";
import { useChatStore } from "@/store/useChatStore";
import { useUiStore } from "@/store/useUiStore";
import { getAppNavItems, type AppNavItem } from "./navigation";

export const VOICE_NAV_ROUTE = "/voice";
type VoiceNavItem = Pick<AppNavItem, "label" | "route"> & { key: "voice" };
export type ShellNavItem = AppNavItem | VoiceNavItem;

const VOICE_NAV_ITEM: VoiceNavItem = {
  key: "voice",
  route: VOICE_NAV_ROUTE,
  label: "声音",
};
const VOICE_NAV_STALE_MS = 60_000;

export function useShellNavItems(): readonly ShellNavItem[] {
  const navVisibility = useUiStore((state) => state.navVisibility);
  const authoritative = useUiStore((state) => state.runtimeDefaultsAuthoritative);
  const userId = useChatStore((state) => state.currentUserId);
  // Private session cleanup updates this epoch synchronously, before a replacement
  // user's bootstrap query can become authoritative.
  const epoch = useUiStore((state) => state.lightbox.identityEpoch);
  const identity = { userId, epoch };
  const identityReady = authoritative && isPrivateIdentitySnapshotCurrent(identity);
  const capabilities = useQuery({
    queryKey: userScopedQueryKey(userId, ["speech", "nav-capabilities", epoch] as const),
    queryFn: async ({ signal }) => {
      const result = await apiFetch<{ enabled?: unknown }>("/speech/capabilities", {
        signal,
        cache: "no-store",
      });
      return isPrivateIdentitySnapshotCurrent(identity) && result?.enabled === true;
    },
    enabled: identityReady,
    retry: false,
    staleTime: VOICE_NAV_STALE_MS,
    refetchOnWindowFocus: true,
    refetchOnReconnect: true,
  });
  const showVoice = identityReady && capabilities.isSuccess && capabilities.data === true;

  return useMemo(() => {
    const items = getAppNavItems(navVisibility);
    if (!showVoice) return items;
    return items.flatMap<ShellNavItem>((item) => item.key === "me" ? [VOICE_NAV_ITEM, item] : [item]);
  }, [navVisibility, showVoice]);
}
