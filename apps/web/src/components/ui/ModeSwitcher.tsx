"use client";

// 两模式 pill：image / chat。
// - 之前的 auto 启发式命中率太低（关键词表 + 附件判断，很多自然表达误判成 chat），
//   V1 直接删掉，intent 由用户显式选；附件存在时 store.resolveIntent 自动派生 i2i / vqa。
// - layoutId 的 motion.span 负责滑动背景色块（spring）
// - 选中 image 时高亮 amber；其它选中态用白色；未选中文字 text-[var(--fg-1)]
// - 移动端 <sm：图标 only，通过 title 提示模式名

import { motion } from "framer-motion";
import { Image as ImageIcon, MessageSquare } from "lucide-react";
import { useChatStore } from "@/store/useChatStore";
import { cn } from "@/lib/utils";

type Mode = "image" | "chat";

const MODES: ReadonlyArray<{
  id: Mode;
  label: string;
  icon: typeof ImageIcon;
}> = [
  { id: "chat", label: "Chat", icon: MessageSquare },
  { id: "image", label: "Image", icon: ImageIcon },
];

export function ModeSwitcher() {
  const mode = useChatStore((s) => s.composer.mode);
  const setMode = useChatStore((s) => s.setMode);

  return (
    <div
      role="tablist"
      aria-label="生成模式"
      className={cn(
        "relative inline-flex items-center p-0.5 rounded-full",
        "bg-white/5 border border-white/10",
      )}
    >
      {MODES.map((m) => {
        const active = mode === m.id;
        const isImageActive = active && m.id === "image";
        const Icon = m.icon;
        return (
          <button
            key={m.id}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => setMode(m.id)}
            title={m.label}
            aria-label={m.label}
            className={cn(
              "relative z-10 inline-flex items-center justify-center gap-1",
              "px-2 sm:px-3 h-7 rounded-full text-xs font-medium",
              "transition-colors duration-200",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-lumen-amber)]/60 focus-visible:ring-offset-0",
              "active:scale-[0.96]",
              active
                ? isImageActive
                  ? "text-black"
                  : "text-white"
                : "text-neutral-400 hover:text-white",
            )}
          >
            {active && (
              <motion.span
                layoutId="mode-switcher-pill"
                transition={{ type: "spring", damping: 28, stiffness: 380 }}
                className={cn(
                  "absolute inset-0 rounded-full -z-10",
                  isImageActive
                    ? "bg-[var(--color-lumen-amber)] shadow-[0_0_14px_rgba(242,169,58,0.35)]"
                    : "bg-white/15",
                )}
              />
            )}
            <Icon className="w-3.5 h-3.5" aria-hidden />
            <span className="hidden sm:inline">{m.label}</span>
          </button>
        );
      })}
    </div>
  );
}
