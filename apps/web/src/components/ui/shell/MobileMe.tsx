"use client";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { ChevronRight, Mail } from "lucide-react";

import { MeTopBar } from "@/components/ui/me/MeTopBar";
import { ConversationList } from "@/components/ui/me/ConversationList";
import { AccountSheet } from "@/components/ui/me/AccountSheet";
import { MobileTabBar } from "@/components/ui/shell/MobileTabBar";
import { useHaptic } from "@/hooks/useHaptic";
import { getMe, type AuthUser } from "@/lib/apiClient";
import { useCreateConversationMutation } from "@/lib/queries";
import { useChatStore } from "@/store/useChatStore";
import { pushMobileToast } from "@/components/ui/primitives/mobile";
import { cn } from "@/lib/utils";
import { useRouter } from "next/navigation";

export function MobileMe() {
  const [query, setQuery] = useState("");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const { haptic } = useHaptic();
  const router = useRouter();
  const setCurrentConv = useChatStore((s) => s.setCurrentConv);

  const createMut = useCreateConversationMutation({
    onSuccess: (conv) => {
      setCurrentConv(conv.id);
      router.push("/");
    },
    onError: (err) => {
      pushMobileToast(
        err?.message ? `新建失败：${err.message}` : "新建失败，请稍后重试",
        "danger",
      );
    },
  });

  const meQuery = useQuery<AuthUser>({
    queryKey: ["me"],
    queryFn: getMe,
    retry: false,
    staleTime: 60_000,
  });

  const userLabel = meQuery.data?.name || meQuery.data?.email || "";
  const avatarChar = userLabel ? userLabel.slice(0, 1).toUpperCase() : "U";

  const openSettings = () => {
    haptic("light");
    setSettingsOpen(true);
  };

  return (
    <div className="relative flex h-[100dvh] w-full min-w-0 flex-col bg-[var(--bg-0)]">
      <MeTopBar
        query={query}
        onQueryChange={setQuery}
        userLabel={userLabel}
        onSettingsTap={openSettings}
        onCreateConversation={() => {
          if (createMut.isPending) return;
          haptic("medium");
          createMut.mutate({});
        }}
        createPending={createMut.isPending}
      />

      <main
        className="flex-1 overflow-y-auto overscroll-contain"
        style={{
          paddingBottom:
            "calc(56px + 12px + env(safe-area-inset-bottom, 0px))",
        }}
      >
        <div className="mx-auto max-w-[640px]">
          {/* 紧凑用户卡：整卡可点 → 打开设置 sheet（iOS"个人中心"模式） */}
          <div className="px-4 pt-2 pb-3">
            <button
              type="button"
              onClick={openSettings}
              aria-label="打开账户与设置"
              className={cn(
                "w-full flex items-center gap-3 p-3 rounded-2xl",
                "bg-[var(--bg-1)] border border-[var(--border-subtle)]",
                "shadow-[var(--shadow-1)]",
                "text-left active:bg-[var(--bg-2)] active:scale-[0.995]",
                "transition-[background-color,transform] duration-150",
              )}
            >
              <div
                className={cn(
                  "w-12 h-12 rounded-xl shrink-0",
                  "bg-gradient-to-br from-[var(--amber-300)] via-[var(--amber-400)] to-[var(--amber-600)]",
                  "flex items-center justify-center",
                  "text-[18px] font-bold text-[var(--bg-0)]",
                  "shadow-[0_0_18px_-4px_var(--amber-glow)]",
                )}
              >
                {avatarChar}
              </div>
              <div className="flex-1 min-w-0">
                {meQuery.isLoading ? (
                  <>
                    <div className="h-4 w-24 rounded bg-[var(--bg-2)] animate-pulse" />
                    <div className="h-3 w-32 rounded bg-[var(--bg-2)] animate-pulse mt-1.5" />
                  </>
                ) : (
                  <>
                    <p className="text-[16px] font-semibold text-[var(--fg-0)] truncate leading-tight">
                      {meQuery.data?.name || "Lumen 用户"}
                    </p>
                    {meQuery.data?.email && (
                      <p className="flex items-center gap-1.5 text-[12.5px] text-[var(--fg-2)] truncate mt-0.5">
                        <Mail className="w-3 h-3 shrink-0" />
                        {meQuery.data.email}
                      </p>
                    )}
                  </>
                )}
              </div>
              <ChevronRight
                className="w-4 h-4 text-[var(--fg-2)]/70 shrink-0"
                aria-hidden
              />
            </button>
          </div>

          <ConversationList query={query} />
        </div>
      </main>

      <AccountSheet
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        user={meQuery.data ?? null}
        loading={meQuery.isLoading}
      />

      <MobileTabBar />
    </div>
  );
}
