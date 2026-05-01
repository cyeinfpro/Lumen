"use client";

// 用户消息气泡：靠右 amber 淡底；hover 显示「复制 prompt」浮动按钮。
// 附件缩略图横排，第一张是 primary。

import { motion } from "framer-motion";
import { useState } from "react";
import { Copy, Check } from "lucide-react";
import { cn } from "@/lib/utils";
import { toast } from "@/components/ui/primitives";
import type { UserMessage } from "@/lib/types";

interface UserBubbleProps {
  msg: UserMessage;
}

export function UserBubble({ msg }: UserBubbleProps) {
  const [copied, setCopied] = useState(false);

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
      className="flex justify-end group"
    >
      <div className="max-w-[92%] md:max-w-[88%] min-w-0 flex flex-col items-end gap-2">
        {msg.attachments.length > 0 && (
          <div className="flex flex-wrap gap-2 justify-end">
            {msg.attachments.map((att, idx) => (
              <motion.div
                key={att.id}
                layout
                initial={{ opacity: 0, scale: 0.9 }}
                animate={{ opacity: 1, scale: 1 }}
                transition={{
                  type: "spring",
                  damping: 26,
                  stiffness: 320,
                  delay: idx * 0.03,
                }}
                className={cn(
                  "relative w-16 h-16 rounded-xl overflow-hidden",
                  "border border-white/10 bg-white/5",
                  idx === 0 && "ring-1 ring-[var(--color-lumen-amber)]/60",
                )}
                title={idx === 0 ? "Primary reference" : "Reference"}
              >
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img
                  src={att.data_url}
                  alt=""
                  className="w-full h-full object-cover"
                  loading="lazy"
                />
                {idx === 0 && (
                  <span className="absolute left-1 top-1 px-1 py-0.5 rounded text-[8px] font-bold tracking-wider uppercase bg-[var(--color-lumen-amber)] text-black">
                    P
                  </span>
                )}
              </motion.div>
            ))}
          </div>
        )}

        {msg.text && (
          <div
            className={cn(
              "relative px-4 py-3 md:px-5 md:py-3.5 rounded-2xl rounded-br-md text-[0.9rem] md:text-[0.95rem] whitespace-pre-wrap leading-relaxed",
              "bg-[var(--color-lumen-amber)]/15 border border-[var(--color-lumen-amber)]/25",
              "text-neutral-100 shadow-sm break-words [overflow-wrap:anywhere]",
            )}
          >
            {msg.text}
            <button
              type="button"
              onClick={() => void handleCopy()}
              aria-label={copied ? "已复制" : "复制"}
              title={copied ? "已复制" : "复制"}
              className={cn(
                "absolute right-2 bottom-2 p-1 rounded-md",
                "text-neutral-400 hover:text-neutral-100 hover:bg-white/10",
                "transition-all duration-150 active:scale-[0.92]",
              )}
            >
              {copied ? (
                <Check className="w-3.5 h-3.5 text-[var(--ok,#30A46C)]" />
              ) : (
                <Copy className="w-3.5 h-3.5" />
              )}
            </button>
          </div>
        )}
      </div>
    </motion.div>
  );
}

async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    try {
      const el = document.createElement("textarea");
      el.value = text;
      el.setAttribute("readonly", "");
      el.style.position = "fixed";
      el.style.left = "-9999px";
      document.body.appendChild(el);
      el.select();
      const ok = document.execCommand("copy");
      el.remove();
      return ok;
    } catch {
      return false;
    }
  }
}
