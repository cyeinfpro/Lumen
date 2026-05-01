"use client";

import { AnimatePresence, motion } from "framer-motion";
import { ChevronDown, PanelLeft, Plus, Zap } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { MobileTopBar } from "./MobileTopBar";
import { MobileConversationDrawer } from "./MobileConversationDrawer";
import { MobileIconButton } from "@/components/ui/primitives/mobile/MobileIconButton";
import { useChatStore } from "@/store/useChatStore";
import { useUiStore } from "@/store/useUiStore";
import {
  useCreateConversationMutation,
  useConversationContextQuery,
  useListConversationsQuery,
} from "@/lib/queries";
import { pushMobileToast } from "@/components/ui/primitives/mobile";
import { ContextWindowMeter } from "@/components/ui/chat/ContextWindowMeter";

export function MobileStudioTopBar() {
  const [drawerOpen, setDrawerOpen] = useState(false);
  const setTaskTrayMinimized = useUiStore((s) => s.setTaskTrayMinimized);

  const currentConvId = useChatStore((s) => s.currentConvId);
  const messages = useChatStore((s) => s.messages);
  const generations = useChatStore((s) => s.generations);
  const setCurrentConv = useChatStore((s) => s.setCurrentConv);
  const fast = useChatStore((s) => s.composer.fast);
  const setFast = useChatStore((s) => s.setFast);

  const createMut = useCreateConversationMutation({
    onSuccess: (conv) => {
      setCurrentConv(conv.id);
      // 新会话里 messages 为空，Studio 会渲染 MobileEmptyStudio。
    },
    onError: (err) => {
      pushMobileToast(
        err?.message ? `新建失败：${err.message}` : "新建失败，请稍后重试",
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

  // 生成环
  const running = useMemo(() => {
    const list = Object.values(generations);
    const r = list.filter((g) => g.status === "running" || g.status === "queued");
    const done = list.filter((g) => g.status === "succeeded").length;
    return {
      total: r.length,
      pct: r.length ? Math.round((done / (done + r.length)) * 100) : 0,
      any: r.length > 0,
    };
  }, [generations]);

  return (
    <>
      <MobileTopBar
        left={
          <div className="flex items-center gap-1 min-w-0">
            <button
              type="button"
              onClick={() => setDrawerOpen(true)}
              aria-label="打开会话列表"
              className="inline-flex items-center justify-center w-9 h-9 -ml-1 rounded-full text-[var(--fg-1)] active:bg-[var(--bg-2)] active:scale-[0.94] transition-[background-color,transform] duration-150"
            >
              <PanelLeft className="w-[18px] h-[18px]" />
            </button>
            <button
              type="button"
              onClick={() => setDrawerOpen(true)}
              className="flex items-center gap-1 min-w-0 cursor-pointer pl-1 pr-1.5 -mx-1 h-9 rounded-md active:bg-[var(--bg-2)]"
              aria-label="切换会话"
            >
              <span className="truncate text-[15px] font-medium text-[var(--fg-0)] max-w-[55vw]">
                {currentTitle}
              </span>
              <ChevronDown className="w-4 h-4 text-[var(--fg-2)] shrink-0" />
            </button>
          </div>
        }
        right={
          <>
            <button
              type="button"
              onClick={() => setFast(!fast)}
              aria-label={fast ? "关闭 Fast 模式" : "开启 Fast 模式"}
              className="inline-flex items-center justify-center w-9 h-9 rounded-full active:bg-[var(--bg-2)] cursor-pointer"
            >
              <FastLamp on={fast} />
            </button>
            <ContextWindowMeter stats={contextStats} compact />
            <AnimatePresence>
              {running.any && (
                <motion.button
                  type="button"
                  initial={{ opacity: 0, scale: 0.8 }}
                  animate={{ opacity: 1, scale: 1 }}
                  exit={{ opacity: 0, scale: 0.8 }}
                  onClick={() => setTaskTrayMinimized(false)}
                  aria-label={`生成中 ${running.total} 张，点击查看任务面板`}
                  className="ml-0.5 inline-flex items-center justify-center w-9 h-9 rounded-full active:bg-[var(--bg-2)] cursor-pointer"
                >
                  <GenerationRing pct={running.pct} total={running.total} />
                </motion.button>
              )}
            </AnimatePresence>
            <MobileIconButton
              icon={<Plus className="w-5 h-5" />}
              label="新建对话"
              onPress={handleNewConv}
              disabled={createMut.isPending}
              className="ml-0.5 disabled:opacity-50"
            />
          </>
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
      style={on ? { filter: "drop-shadow(0 0 6px var(--amber-glow-strong))" } : undefined}
    >
      <Zap className="w-4 h-4" fill={on ? "currentColor" : "none"} strokeWidth={1.8} />
    </span>
  );
}

function GenerationRing({ pct, total }: { pct: number; total: number }) {
  const R = 10;
  const C = 2 * Math.PI * R;
  const off = C * (1 - Math.max(0, Math.min(pct, 100)) / 100);
  return (
    <span
      className="inline-flex relative w-6 h-6 items-center justify-center text-[9px] font-mono text-[var(--amber-300)]"
      aria-label={`生成中 ${total} 张`}
    >
      <svg width={24} height={24} viewBox="0 0 24 24" className="absolute inset-0">
        <circle cx={12} cy={12} r={R} stroke="var(--border-subtle)" strokeWidth={2} fill="none" />
        <circle
          cx={12}
          cy={12}
          r={R}
          stroke="var(--amber-400)"
          strokeWidth={2}
          fill="none"
          strokeLinecap="round"
          strokeDasharray={C}
          strokeDashoffset={off}
          transform="rotate(-90 12 12)"
          style={{ transition: "stroke-dashoffset 300ms ease" }}
        />
      </svg>
      <span className="relative">{total}</span>
    </span>
  );
}
