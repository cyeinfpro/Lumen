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
import { Button } from "@/components/ui/primitives";
import { copy } from "@/lib/copy";
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
      {/* 意图徽章触发器：极小尺寸 + pill 边框，不匹配标准 Button */}
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
            ? "border-[var(--border)] bg-[var(--bg-1)]/72 text-[var(--fg-1)] cursor-not-allowed opacity-70"
            : "border-[var(--border)] bg-[var(--bg-1)]/86 text-[var(--fg-0)] hover:border-[var(--color-lumen-amber)]/60 hover:text-[var(--color-lumen-amber)] active:scale-[0.97] cursor-pointer",
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
              "rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/95 backdrop-blur-xl",
              "shadow-[var(--shadow-3)] overflow-hidden",
            )}
            style={{ transformOrigin: "top right" }}
          >
            {pendingIntent && (
              <div className="px-3 py-2.5 border-b border-[var(--border)] space-y-2">
                <p className="text-xs text-[var(--fg-0)] leading-relaxed">
                  以{" "}
                  <span className="text-[var(--color-lumen-amber)] font-medium">
                    {INTENT_META[pendingIntent].label}
                  </span>{" "}
                  重新生成？
                </p>
                <div className="flex gap-2">
                  <Button
                    type="button"
                    size="sm"
                    variant="primary"
                    onClick={() => void handleConfirm()}
                    loading={busy}
                    fullWidth
                    className="h-7 text-[11px]"
                  >
                    {busy ? "切换中" : copy.action.confirm}
                  </Button>
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    onClick={() => {
                      setPendingIntent(null);
                      setError(null);
                    }}
                    disabled={busy}
                    fullWidth
                    className="h-7 text-[11px]"
                  >
                    {copy.action.cancel}
                  </Button>
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
                  <Button
                    key={opt}
                    type="button"
                    size="sm"
                    variant="ghost"
                    role="menuitem"
                    disabled={isCurrent || busy}
                    onClick={() => handlePick(opt)}
                    leftIcon={<OptIcon className="w-3.5 h-3.5 shrink-0" aria-hidden />}
                    rightIcon={
                      isCurrent ? (
                        <Check className="w-3.5 h-3.5 text-[var(--color-lumen-amber)]" aria-hidden />
                      ) : undefined
                    }
                    className={cn(
                      "w-full justify-start h-auto px-3 py-1.5 text-xs rounded-none",
                      isCurrent
                        ? "text-[var(--color-lumen-amber)] cursor-default disabled:opacity-100"
                        : isPending
                          ? "text-[var(--fg-0)] bg-white/5"
                          : "text-[var(--fg-1)] hover:bg-white/5 hover:text-[var(--fg-0)]",
                      busy && !isCurrent && "opacity-50 cursor-wait",
                    )}
                  >
                    <span className="flex-1 text-left">{M.label}</span>
                  </Button>
                );
              })}
            </div>
            {error && (
              <p className="px-3 pt-1.5 pb-2 text-[10px] text-danger border-t border-white/10">
                {error}
              </p>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
