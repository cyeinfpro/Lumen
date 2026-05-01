"use client";

// 意图徽章（DESIGN §22.1）：显示当前 assistant 消息的 intent_resolved；
// 点击 → 弹菜单切换 intent，并通过 regenerateAssistant 让后端用新 intent 重跑。
// 非 icon-only：关键操作 + 需要当前状态可视。

import { AnimatePresence, motion } from "framer-motion";
import { useEffect, useRef, useState } from "react";
import {
  MessageSquare,
  Eye,
  Image as ImageIcon,
  Sparkles,
  Check,
  Loader2,
} from "lucide-react";
import { cn } from "@/lib/utils";
import type { Intent } from "@/lib/types";

type Resolved = Exclude<Intent, "auto">;

const INTENT_META: Record<
  Resolved,
  { label: string; short: string; icon: typeof MessageSquare }
> = {
  chat: { label: "Chat", short: "Chat", icon: MessageSquare },
  vision_qa: { label: "Vision QA", short: "Vision", icon: Eye },
  text_to_image: { label: "Text → Image", short: "Image", icon: Sparkles },
  image_to_image: { label: "Image → Image", short: "Edit", icon: ImageIcon },
};

const INTENT_OPTIONS: ReadonlyArray<Resolved> = [
  "chat",
  "vision_qa",
  "text_to_image",
  "image_to_image",
];

interface IntentBadgeProps {
  currentIntent: Resolved;
  disabled: boolean;
  onSwitch: (newIntent: Resolved) => Promise<void>;
  className?: string;
}

export function IntentBadge({
  currentIntent,
  disabled,
  onSwitch,
  className,
}: IntentBadgeProps) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pendingIntent, setPendingIntent] = useState<Resolved | null>(null);
  const wrapRef = useRef<HTMLDivElement | null>(null);

  // HIGH #3：pointerdown 外部点击 + Esc 关闭
  useEffect(() => {
    if (!open) return;
    const pointerHandler = (e: PointerEvent) => {
      const node = wrapRef.current;
      if (!node) return;
      if (e.target instanceof Node && !node.contains(e.target)) {
        setOpen(false);
        setError(null);
        setPendingIntent(null);
      }
    };
    const keyHandler = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setOpen(false);
        setPendingIntent(null);
        setError(null);
      }
    };
    document.addEventListener("pointerdown", pointerHandler);
    document.addEventListener("keydown", keyHandler);
    return () => {
      document.removeEventListener("pointerdown", pointerHandler);
      document.removeEventListener("keydown", keyHandler);
    };
  }, [open]);

  const meta = INTENT_META[currentIntent];
  const Icon = meta.icon;
  const disabledTitle = disabled ? "生成中无法切换意图" : "切换意图";

  const handlePick = (next: Resolved) => {
    if (next === currentIntent || busy) return;
    setPendingIntent(next);
    setError(null);
  };

  const handleConfirm = async () => {
    if (!pendingIntent || busy) return;
    setBusy(true);
    setError(null);
    try {
      await onSwitch(pendingIntent);
      setOpen(false);
      setPendingIntent(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "切换失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div ref={wrapRef} className={cn("relative", className)}>
      <button
        type="button"
        onClick={() => {
          if (disabled || busy) return;
          setOpen((v) => !v);
          setError(null);
        }}
        disabled={disabled || busy}
        title={disabledTitle}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={`当前意图 ${meta.label}，点击切换`}
        className={cn(
          "inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium",
          "border backdrop-blur-md transition-all duration-150",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-lumen-amber)]/60",
          disabled || busy
            ? "border-white/10 bg-black/40 text-neutral-400 cursor-not-allowed opacity-70"
            : "border-white/15 bg-black/60 text-neutral-200 hover:border-[var(--color-lumen-amber)]/60 hover:text-white active:scale-[0.97] cursor-pointer",
        )}
      >
        {busy ? (
          <Loader2 className="w-3 h-3 animate-spin" aria-hidden />
        ) : (
          <Icon className="w-3 h-3" aria-hidden />
        )}
        <span>{meta.short}</span>
      </button>

      <AnimatePresence>
        {open && !disabled && (
          <motion.div
            initial={{ opacity: 0, scale: 0.95, y: -4 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.95, y: -4 }}
            transition={{ duration: 0.12, ease: [0.16, 1, 0.3, 1] }}
            role="menu"
            aria-label="切换意图"
            className={cn(
              "absolute right-0 top-[calc(100%+6px)] z-50 min-w-[220px]",
              "rounded-xl border border-white/12 bg-neutral-900/95 backdrop-blur-xl",
              "shadow-2xl shadow-black/60 overflow-hidden",
            )}
            style={{ transformOrigin: "top right" }}
          >
            {pendingIntent && (
              <div className="px-3 py-2.5 border-b border-white/10 space-y-2">
                <p className="text-xs text-neutral-200 leading-relaxed">
                  以{" "}
                  <span className="text-[var(--color-lumen-amber)] font-medium">
                    {INTENT_META[pendingIntent].label}
                  </span>{" "}
                  重新生成？
                </p>
                <div className="flex gap-2">
                  <button
                    type="button"
                    onClick={() => void handleConfirm()}
                    disabled={busy}
                    aria-disabled={busy}
                    aria-busy={busy}
                    className={cn(
                      "flex-1 inline-flex items-center justify-center gap-1 px-2.5 py-1 rounded-md text-xs font-medium",
                      "bg-[var(--color-lumen-amber)] text-black",
                      "hover:brightness-110 active:scale-[0.97]",
                      "shadow-[0_0_12px_rgba(242,169,58,0.35)]",
                      "disabled:opacity-60 disabled:cursor-wait transition-all duration-150",
                      "aria-disabled:pointer-events-none",
                      "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-lumen-amber)]/70",
                    )}
                  >
                    {busy && (
                      <Loader2
                        className="w-3 h-3 animate-spin"
                        aria-hidden
                      />
                    )}
                    <span>{busy ? "切换中…" : "确认"}</span>
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      setPendingIntent(null);
                      setError(null);
                    }}
                    disabled={busy}
                    className={cn(
                      "flex-1 px-2.5 py-1 rounded-md text-xs",
                      "bg-white/5 hover:bg-white/10 border border-white/10 text-neutral-200",
                      "active:scale-[0.97] transition-all duration-150 disabled:opacity-50",
                      "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/30",
                    )}
                  >
                    取消
                  </button>
                </div>
              </div>
            )}
            <div className="py-1">
              {INTENT_OPTIONS.map((opt) => {
                const isCurrent = opt === currentIntent;
                const isPending = opt === pendingIntent;
                const M = INTENT_META[opt];
                const OptIcon = M.icon;
                return (
                  <button
                    key={opt}
                    type="button"
                    role="menuitem"
                    disabled={isCurrent || busy}
                    onClick={() => handlePick(opt)}
                    className={cn(
                      "w-full flex items-center gap-2 px-3 py-1.5 text-left text-xs transition-colors",
                      "focus-visible:outline-none focus-visible:bg-white/10",
                      isCurrent
                        ? "text-[var(--color-lumen-amber)] cursor-default"
                        : isPending
                          ? "text-neutral-100 bg-white/5"
                          : "text-neutral-200 hover:bg-white/5 cursor-pointer",
                      busy && !isCurrent && "opacity-50 cursor-wait",
                    )}
                  >
                    <OptIcon className="w-3.5 h-3.5 shrink-0" aria-hidden />
                    <span className="flex-1">{M.label}</span>
                    {isCurrent && (
                      <Check
                        className="w-3.5 h-3.5 text-[var(--color-lumen-amber)]"
                        aria-hidden
                      />
                    )}
                  </button>
                );
              })}
            </div>
            {error && (
              <p className="px-3 pt-1.5 pb-2 text-[10px] text-red-300 border-t border-white/10">
                {error}
              </p>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
