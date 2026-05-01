"use client";

// 助手消息气泡：靠左玻璃卡；包含文本 + 可选的生成卡 + 底部工具条（重试/复制/意图切换）。
// IntentBadge 放在文本气泡内部（右上）或生成卡右上；工具条在气泡外底部，hover/focus 显现。

import { motion, AnimatePresence } from "framer-motion";
import dynamic from "next/dynamic";
import { useState } from "react";
import { Copy, Check, RotateCw, ChevronDown } from "lucide-react";
import { cn } from "@/lib/utils";
import { Markdown } from "../Markdown";
import { toast } from "@/components/ui/primitives";
import { IntentBadge } from "./IntentBadge";
import type { AssistantMessage, Generation, Intent } from "@/lib/types";

export interface AssistantBubbleProps {
  msg: AssistantMessage;
  generations: Generation[];
  onEditImage: (imageId: string) => void;
  onRetry: (gen: Generation) => void;
  onRetryText: () => void;
  onRegenerate: (newIntent: Exclude<Intent, "auto">) => Promise<void>;
}

const GenerationView = dynamic(() => import("./GenerationView"), {
  ssr: false,
  loading: () => <GenerationViewFallback />,
});

export function AssistantBubble({
  msg,
  generations,
  onEditImage,
  onRetry,
  onRetryText,
  onRegenerate,
}: AssistantBubbleProps) {
  const [copied, setCopied] = useState(false);
  const [thinkingOpen, setThinkingOpen] = useState(false);
  const isStreamingText = msg.status === "streaming";
  const isThinking = isStreamingText && !!msg.thinking && !msg.text;
  const isChatLike =
    msg.intent_resolved === "chat" || msg.intent_resolved === "vision_qa";
  // pending / streaming / canceled 期间不允许切换 intent
  const canSwitchIntent =
    msg.status === "succeeded" || msg.status === "failed";
  const isFailedText = msg.status === "failed" && isChatLike;
  const canCopy = Boolean(msg.text && msg.status !== "pending");
  const hasGenerations = generations.length > 0;

  const handleCopy = async () => {
    if (!msg.text) return;
    if (await copyText(msg.text)) {
      setCopied(true);
      setTimeout(() => setCopied(false), 1400);
    } else {
      toast.error("复制失败", { description: "浏览器拒绝了剪贴板写入" });
    }
  };

  return (
    <motion.div
      layout="position"
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -6 }}
      transition={{ type: "spring", damping: 28, stiffness: 320 }}
      className="group flex justify-start"
    >
      <div className="max-w-[96%] md:max-w-[96%] w-full min-w-0 flex flex-col gap-2">
        {/* Thinking 折叠区：默认收起；点击后展开 */}
        {msg.thinking && (
          <div className="rounded-xl border border-white/[0.06] bg-white/[0.03] overflow-hidden">
            <button
              type="button"
              onClick={() => setThinkingOpen((v) => !v)}
              className={cn(
                "flex w-full items-center gap-2 px-4 py-2 text-xs text-neutral-400",
                "hover:text-neutral-300 transition-colors",
              )}
            >
              <span className={cn(
                "inline-block w-1.5 h-1.5 rounded-full",
                isThinking
                  ? "bg-[var(--color-lumen-amber)] animate-pulse"
                  : "bg-neutral-500",
              )} />
              <span>{isThinking ? "思考中…" : "思考过程"}</span>
              <ChevronDown
                className={cn(
                  "w-3 h-3 ml-auto transition-transform duration-200",
                  thinkingOpen && "rotate-180",
                )}
              />
            </button>
            <AnimatePresence initial={false}>
              {thinkingOpen && (
                <motion.div
                  initial={{ height: 0, opacity: 0 }}
                  animate={{ height: "auto", opacity: 1 }}
                  exit={{ height: 0, opacity: 0 }}
                  transition={{ duration: 0.2 }}
                  className="overflow-hidden"
                >
                  <div className="px-4 pb-3 text-xs leading-relaxed text-neutral-500 max-h-60 overflow-y-auto">
                    <Markdown>{msg.thinking}</Markdown>
                    {isThinking && (
                      <span
                        aria-hidden
                        className="inline-block w-[0.4ch] ml-0.5 animate-pulse text-neutral-500"
                      >
                        ▍
                      </span>
                    )}
                  </div>
                </motion.div>
              )}
            </AnimatePresence>
          </div>
        )}

        {/* 文本气泡 */}
        {(msg.text || (isChatLike && !hasGenerations)) && (
          <div
            className={cn(
              "relative px-4 py-3 md:px-5 md:py-3.5 rounded-2xl rounded-bl-md text-[0.9rem] md:text-[0.95rem] leading-relaxed",
              "bg-[var(--bg-1)]/70 border border-white/10 text-neutral-200",
              "backdrop-blur-sm shadow-sm min-w-0 break-words [overflow-wrap:anywhere]",
              "[&_pre]:max-w-full [&_pre]:overflow-x-auto [&_img]:max-w-full [&_img]:h-auto",
              isFailedText && "border-red-400/30 bg-red-500/5",
            )}
          >
            {msg.text ? (
              <Markdown>{msg.text}</Markdown>
            ) : (
              <span className="text-neutral-400">
                {isStreamingText ? "" : "…"}
              </span>
            )}
            {isStreamingText && (
              <span
                aria-hidden
                className="inline-block w-[0.5ch] ml-0.5 animate-pulse text-[var(--color-lumen-amber)]"
              >
                ▍
              </span>
            )}
            <IntentBadge
              currentIntent={msg.intent_resolved}
              disabled={!canSwitchIntent}
              onSwitch={onRegenerate}
              className="absolute -top-2.5 right-2"
            />
            {canCopy && (
              <button
                type="button"
                onClick={() => void handleCopy()}
                aria-label={copied ? "已复制" : "复制"}
                title={copied ? "已复制" : "复制"}
                className={cn(
                  "absolute right-2 bottom-2 p-1 rounded-md",
                  "text-neutral-500 hover:text-neutral-200 hover:bg-white/10",
                  "transition-all duration-150 active:scale-[0.92]",
                )}
              >
                {copied ? (
                  <Check className="w-3.5 h-3.5 text-[var(--ok,#30A46C)]" />
                ) : (
                  <Copy className="w-3.5 h-3.5" />
                )}
              </button>
            )}
          </div>
        )}

        {/* 生成卡 */}
        {hasGenerations && (
          <div
            className={cn(
              generations.length === 1
                ? "flex flex-col gap-3"
                : "grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3",
            )}
          >
            {generations.map((gen, index) => (
              <GenerationView
                key={gen.id}
                gen={gen}
                currentIntent={msg.intent_resolved}
                canSwitchIntent={canSwitchIntent}
                onEditImage={onEditImage}
                onRetry={onRetry}
                onRegenerate={onRegenerate}
                compact={generations.length > 1}
                ordinal={generations.length > 1 ? index + 1 : undefined}
              />
            ))}
          </div>
        )}

        {/* 底部重试按钮 */}
        {msg.status === "failed" && isChatLike && (
          <div
            className={cn(
              "flex items-center gap-1 pl-2 -mt-1",
              "opacity-100 sm:opacity-0 sm:group-hover:opacity-100 focus-within:opacity-100",
              "transition-opacity duration-200",
            )}
          >
            <ToolbarButton onClick={onRetryText} label="重试">
              <RotateCw className="w-3.5 h-3.5" />
            </ToolbarButton>
          </div>
        )}
      </div>
    </motion.div>
  );
}

export default AssistantBubble;

function GenerationViewFallback() {
  return (
    <div className="flex flex-col gap-2.5">
      <div className="aspect-[4/3] w-full rounded-2xl border border-white/10 bg-white/[0.03]" />
      <div className="h-4 w-2/3 rounded bg-white/[0.04]" />
    </div>
  );
}

// BUG-036: 移除已弃用的 execCommand("copy") 回退。navigator.clipboard API 在所有现代浏览器中可用。
async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

// 通用小工具按钮：36px 触控目标在子元素 padding 内得到
function ToolbarButton({
  onClick,
  label,
  children,
}: {
  onClick: () => void;
  label: string;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      title={label}
      className={cn(
        "inline-flex items-center justify-center w-9 h-9 rounded-md",
        "text-neutral-400 hover:text-white hover:bg-white/10",
        "active:scale-[0.92] transition-all duration-150",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-lumen-amber)]/60",
      )}
    >
      {children}
    </button>
  );
}
