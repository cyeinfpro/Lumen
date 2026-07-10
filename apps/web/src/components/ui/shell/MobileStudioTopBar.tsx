"use client";

import { ChevronDown, PanelLeft, Plus, Settings, Zap } from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { MobileTopBar } from "./MobileTopBar";
import { MobileConversationDrawer } from "./MobileConversationDrawer";
import { MobileIconButton } from "@/components/ui/primitives/mobile/MobileIconButton";
import { Pressable } from "@/components/ui/primitives/mobile/Pressable";
import { useChatStore } from "@/store/useChatStore";
import {
  useCreateConversationMutation,
  useConversationContextQuery,
  useListConversationsQuery,
} from "@/lib/queries";
import { pushMobileToast } from "@/components/ui/primitives/mobile";
import { ContextWindowMeter } from "@/components/ui/chat/ContextWindowMeter";
import { ConversationMemoryButton } from "@/components/ui/chat/ConversationMemoryButton";

export function MobileStudioTopBar() {
  const [drawerOpen, setDrawerOpen] = useState(false);

  const currentConvId = useChatStore((s) => s.currentConvId);
  const messages = useChatStore((s) => s.messages);
  const setCurrentConv = useChatStore((s) => s.setCurrentConv);
  const mode = useChatStore((s) => s.composer.mode);
  const setMode = useChatStore((s) => s.setMode);
  const fast = useChatStore((s) => s.composer.fast);
  const setFast = useChatStore((s) => s.setFast);

  const createMut = useCreateConversationMutation({
    onSuccess: (conv) => {
      setCurrentConv(conv.id);
      // 新会话里 messages 为空，Studio 会渲染 MobileEmptyStudio。
    },
    onError: (err) => {
      pushMobileToast(
        err?.message ? `新建失败：${err.message}` : "新建失败，稍后重试",
        "danger",
      );
    },
  });

  const handleNewConv = () => {
    if (createMut.isPending) return;
    createMut.mutate({});
  };

  const convsQuery = useListConversationsQuery({ limit: 30 });
  const {
    data: contextStats,
    refetch: refetchContextStats,
  } = useConversationContextQuery(currentConvId, { refetchInterval: 30_000 });

  useEffect(() => {
    if (!currentConvId) return;
    void refetchContextStats();
  }, [currentConvId, messages.length, refetchContextStats]);

  const currentTitle = useMemo(() => {
    const items = convsQuery.data?.items ?? [];
    const cur = items.find((c) => c.id === currentConvId);
    if (cur?.title) return cur.title;
    // fallback：用第一条用户消息
    const firstUser = messages.find((m) => m.role === "user");
    if (firstUser && "text" in firstUser && typeof firstUser.text === "string") {
      return firstUser.text.slice(0, 20) || "新对话";
    }
    return "新对话";
  }, [convsQuery.data, currentConvId, messages]);

  return (
    <>
      <MobileTopBar
        showWallet={false}
        left={
          <div className="flex min-w-0 items-center gap-1">
            <Pressable
              size="default"
              minHit
              pressScale="tight"
              haptic="light"
              onPress={() => setDrawerOpen(true)}
              aria-label="打开会话列表"
              className="-ml-1 h-10 w-10 shrink-0 rounded-[var(--radius-control)] text-[var(--fg-1)]"
            >
              <PanelLeft className="w-[18px] h-[18px]" />
            </Pressable>
            <Pressable
              size="default"
              minHit
              pressScale="soft"
              haptic="light"
              onPress={() => setDrawerOpen(true)}
              aria-label="切换会话"
              className="flex h-10 min-w-0 max-w-full items-center gap-1 rounded-[var(--radius-control)] px-1.5"
            >
              <span className="max-w-[58vw] truncate text-[15px] font-medium text-[var(--fg-0)] [@media(max-width:390px)]:max-w-[52vw]">
                {currentTitle}
              </span>
              <ChevronDown className="w-4 h-4 text-[var(--fg-2)] shrink-0" />
            </Pressable>
          </div>
        }
        right={
          <MobileIconButton
            icon={<Plus className="w-5 h-5" />}
            label="新建对话"
            onPress={handleNewConv}
            disabled={createMut.isPending}
            minHit
            className="h-10 w-10 rounded-[var(--radius-control)] disabled:opacity-50"
          />
        }
        below={
          <div className="flex min-h-10 items-center gap-1.5 overflow-x-auto no-scrollbar">
            <Pressable
              size="default"
              minHit
              pressScale="soft"
              haptic="light"
              onPress={() => setMode(mode === "image" ? "chat" : "image")}
              aria-label={mode === "image" ? "切换到对话" : "切换到生图"}
              className="h-10 shrink-0 gap-1.5 rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-1)] px-3 text-[12px] font-medium text-[var(--fg-0)]"
            >
              {mode === "image" ? "生图" : "对话"}
              <ChevronDown className="h-3.5 w-3.5 text-[var(--fg-2)]" aria-hidden />
            </Pressable>
            <ContextWindowMeter stats={contextStats} compact />
            <ConversationMemoryButton compact />
            <Pressable
              size="default"
              minHit
              pressScale="tight"
              haptic="light"
              onPress={() => setFast(!fast)}
              aria-label={fast ? "关闭 Fast 模式" : "开启 Fast 模式"}
              aria-pressed={fast}
              className={[
                "h-10 w-10 shrink-0 rounded-[var(--radius-control)] border",
                fast
                  ? "border-[var(--accent-border)] bg-[var(--accent-soft)]"
                  : "border-[var(--border-subtle)] bg-[var(--bg-1)]",
              ].join(" ")}
            >
              <FastLamp on={fast} />
            </Pressable>
            <Link
              href="/me"
              aria-label="会话与账户设置"
              className="inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-[var(--radius-control)] text-[var(--fg-1)] transition-colors active:bg-[var(--bg-2)]"
            >
              <Settings className="w-[18px] h-[18px]" />
            </Link>
          </div>
        }
      />

      <MobileConversationDrawer
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
      />
    </>
  );
}

function FastLamp({ on }: { on: boolean }) {
  return (
    <span
      className={[
        "inline-flex items-center justify-center",
        on ? "text-[var(--amber-400)]" : "text-[var(--fg-3)]",
      ].join(" ")}
    >
      <Zap className="w-4 h-4" fill={on ? "currentColor" : "none"} strokeWidth={1.8} />
    </span>
  );
}
