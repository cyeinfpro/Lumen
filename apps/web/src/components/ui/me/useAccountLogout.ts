"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useCallback, useState } from "react";

import { logout } from "@/lib/apiClient";
import { logWarn } from "@/lib/logger";
import { useChatStore } from "@/store/useChatStore";

export interface AccountLogoutController {
  isOpen: boolean;
  isPending: boolean;
  request: () => void;
  dismiss: () => void;
  confirm: () => Promise<void>;
}

export function useAccountLogout(): AccountLogoutController {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [isOpen, setIsOpen] = useState(false);
  const [isPending, setIsPending] = useState(false);

  const request = useCallback(() => setIsOpen(true), []);
  const dismiss = useCallback(() => setIsOpen(false), []);
  const confirm = useCallback(async () => {
    if (isPending) return;
    setIsPending(true);
    try {
      await logout();
    } catch (err) {
      logWarn("mobile_me.logout_failed", {
        scope: "mobile-me",
        extra: { err: String(err) },
      });
    } finally {
      useChatStore.getState().reset();
      queryClient.clear();
      setIsPending(false);
      router.push("/login");
    }
  }, [isPending, queryClient, router]);

  return {
    isOpen,
    isPending,
    request,
    dismiss,
    confirm,
  };
}
