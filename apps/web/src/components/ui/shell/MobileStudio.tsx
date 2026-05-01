"use client";

import { useEffect, useMemo, useRef } from "react";
import { useSearchParams } from "next/navigation";
import { LandscapeBanner } from "./LandscapeBanner";
import { MobileStudioTopBar } from "./MobileStudioTopBar";
import { MobileTabBar } from "./MobileTabBar";
import { MobileConversationCanvas } from "@/components/ui/chat/mobile/MobileConversationCanvas";
import { MobileComposerPill } from "@/components/ui/composer/mobile/MobileComposerPill";
import { MobileEmptyStudio } from "@/components/ui/chat/mobile/MobileEmptyStudio";
import { useChatStore } from "@/store/useChatStore";
import { useListConversationsQuery } from "@/lib/queries";
import { cn } from "@/lib/utils";

export function MobileStudio() {
  const messages = useChatStore((s) => s.messages);
  const generations = useChatStore((s) => s.generations);
  const currentConvId = useChatStore((s) => s.currentConvId);
  const setCurrentConv = useChatStore((s) => s.setCurrentConv);
  const loadHistoricalMessages = useChatStore((s) => s.loadHistoricalMessages);
  const sendMessage = useChatStore((s) => s.sendMessage);
  const retryAssistant = useChatStore((s) => s.retryAssistant);
  const retryGeneration = useChatStore((s) => s.retryGeneration);
  const regenerateAssistant = useChatStore((s) => s.regenerateAssistant);
  const promoteImageToReference = useChatStore((s) => s.promoteImageToReference);
  const setText = useChatStore((s) => s.setText);
  const setMode = useChatStore((s) => s.setMode);

  const scrollRef = useRef<HTMLDivElement | null>(null);
  const search = useSearchParams();
  const scrollTo = search.get("scrollTo");
  const scrollSignature = useMemo(() => {
    const last = messages[messages.length - 1];
    if (!last) return "empty";

    if (last.role === "assistant") {
      return [
        messages.length,
        last.id,
        last.status,
        last.text?.length ?? 0,
        last.thinking?.length ?? 0,
        last.last_delta_at ?? 0,
      ].join(":");
    }

    return [
      messages.length,
      last.id,
      last.role,
      last.text?.length ?? 0,
      last.attachments?.length ?? 0,
    ].join(":");
  }, [messages]);
  const latestIsStreaming = useMemo(() => {
    const last = messages[messages.length - 1];
    return last?.role === "assistant" && last.status === "streaming";
  }, [messages]);

  // 首次进入自动挂到最近一条活跃会话，与 DesktopStudio 对齐。
  const convsQuery = useListConversationsQuery({ limit: 30 });
  useEffect(() => {
    if (currentConvId) return;
    const items = convsQuery.data?.items ?? [];
    const first = items.find((c) => !c.archived);
    if (!first) return;
    setCurrentConv(first.id);
    void loadHistoricalMessages(first.id).catch(() => {});
  }, [currentConvId, convsQuery.data, setCurrentConv, loadHistoricalMessages]);

  // 若来自灵感流 "在对话中定位"，滚到目标 message
  useEffect(() => {
    if (!scrollTo) return;
    const el = document.getElementById(`msg-${scrollTo}`);
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }, [scrollTo, messages.length]);

  // Stick-to-bottom：切换会话 / 新消息到达时滚到底，除非用户向上滚了一段。
  // 与 DesktopStudio 的 ConversationCanvas 行为一致（stickToBottomRef）。
  const stickToBottomRef = useRef(true);
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const onScroll = () => {
      const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
      stickToBottomRef.current = distance < 120;
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
    return () => el.removeEventListener("scroll", onScroll);
  }, []);

  // 切换会话：强制回到底部；之后新消息到达只在"贴底"状态下才滚。
  useEffect(() => {
    stickToBottomRef.current = true;
  }, [currentConvId]);

  useEffect(() => {
    if (scrollTo) return; // deep-link 定位时不干扰
    const el = scrollRef.current;
    if (!el) return;
    if (!stickToBottomRef.current) return;
    const prefersReduced =
      typeof window !== "undefined" &&
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    requestAnimationFrame(() => {
      el.scrollTo({
        top: el.scrollHeight,
        behavior: prefersReduced || latestIsStreaming ? "auto" : "smooth",
      });
    });
  }, [currentConvId, generations, latestIsStreaming, scrollSignature, scrollTo]);

  const isEmpty = messages.length === 0;

  return (
    <div className="relative flex h-[100dvh] w-full min-w-0 flex-col bg-[var(--bg-0)]">
      <div data-topbar-sentinel className="absolute top-0 h-1 w-full" aria-hidden />
      <LandscapeBanner />
      <MobileStudioTopBar />

      <main
        ref={scrollRef}
        className="flex-1 overflow-y-auto overscroll-contain"
        style={{
          paddingBottom:
            "calc(48px + 48px + 12px + env(safe-area-inset-bottom, 0px))",
        }}
      >
        <div
          className={cn(
            "mx-auto max-w-[640px] px-3",
            isEmpty ? "min-h-full flex flex-col justify-center" : "pt-1",
          )}
        >
          {isEmpty ? (
            <MobileEmptyStudio
              onPick={(text, mode) => {
                setText(text);
                setMode(mode);
              }}
            />
          ) : (
            <MobileConversationCanvas
              messages={messages}
              generations={generations}
              scrollRef={scrollRef}
              onEditImage={promoteImageToReference}
              onRetryGen={(gid) => void retryGeneration(gid)}
              onRetryText={(id) => void retryAssistant(id)}
              onRegenerate={(id, intent) => {
                if (intent) void regenerateAssistant(id, intent);
              }}
            />
          )}
        </div>
      </main>

      <MobileComposerPill onSubmit={() => sendMessage()} />
      <MobileTabBar />
    </div>
  );
}
