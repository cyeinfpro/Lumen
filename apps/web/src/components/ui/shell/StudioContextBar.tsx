"use client";

import {
  Images,
  MessageSquareText,
  Settings2,
  Zap,
} from "lucide-react";
import {
  type ComponentProps,
  useEffect,
  useRef,
  useState,
} from "react";

import { ConversationMemoryButton } from "@/components/ui/chat/ConversationMemoryButton";
import { ContextWindowMeter } from "@/components/ui/chat/ContextWindowMeter";
import { IconButton } from "@/components/ui/primitives";
import { SegmentedControl } from "@/components/ui/primitives/mobile";
import { SystemPromptManager } from "@/components/ui/SystemPromptManager";
import { cn } from "@/lib/utils";

type ContextStats = ComponentProps<typeof ContextWindowMeter>["stats"];

export function StudioContextBar({
  title,
  view,
  onViewChange,
  fast,
  onFastChange,
  contextStats,
}: {
  title: string;
  view: "chat" | "images";
  onViewChange: (view: "chat" | "images") => void;
  fast: boolean;
  onFastChange: (next: boolean) => void;
  contextStats: ContextStats;
}) {
  const [settingsOpen, setSettingsOpen] = useState(false);
  const settingsRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!settingsOpen) return;
    const onPointerDown = (event: PointerEvent) => {
      const root = settingsRef.current;
      if (!root || !(event.target instanceof Node) || root.contains(event.target)) {
        return;
      }
      setSettingsOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setSettingsOpen(false);
    };
    window.addEventListener("pointerdown", onPointerDown, true);
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("pointerdown", onPointerDown, true);
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [settingsOpen]);

  return (
    <div className="flex h-11 shrink-0 items-center gap-3 border-b border-[var(--border-subtle)] bg-[var(--surface-chrome)]/88 px-3 md:px-4">
      <div className="flex min-w-0 flex-1 items-center gap-2.5">
        <p className="type-nav max-w-[min(42vw,360px)] truncate text-[var(--fg-0)]">
          {title}
        </p>

        <span
          aria-hidden
          className="hidden h-4 w-px bg-[var(--border-subtle)] sm:block"
        />

        <div className="hidden shrink-0 sm:block">
          <SegmentedControl<"chat" | "images">
            value={view}
            onChange={onViewChange}
            ariaLabel="会话视图"
            density="compact"
            items={[
              {
                value: "chat",
                label: (
                  <span className="inline-flex items-center gap-1">
                    <MessageSquareText className="h-3.5 w-3.5" aria-hidden />
                    <span>对话</span>
                  </span>
                ),
              },
              {
                value: "images",
                label: (
                  <span className="inline-flex items-center gap-1">
                    <Images className="h-3.5 w-3.5" aria-hidden />
                    <span>图库</span>
                  </span>
                ),
              },
            ]}
          />
        </div>
      </div>

      <div ref={settingsRef} className="relative shrink-0">
        <IconButton
          size="sm"
          variant={settingsOpen ? "secondary" : "ghost"}
          onClick={() => setSettingsOpen((open) => !open)}
          aria-haspopup="menu"
          aria-expanded={settingsOpen}
          aria-label="会话设置"
          className={cn(
            "relative",
            !settingsOpen && "text-[var(--fg-2)]",
          )}
        >
          <Settings2 className="h-4 w-4" aria-hidden />
          {fast && (
            <span
              aria-hidden
              className="absolute right-1 top-1 h-1.5 w-1.5 rounded-full bg-[var(--accent)]"
            />
          )}
        </IconButton>

        {settingsOpen && (
          <div
            role="menu"
            aria-label="会话设置"
            className="surface-panel adaptive-material absolute right-0 top-10 z-40 w-[min(320px,calc(100vw-24px))] origin-top-right p-2"
          >
            <div className="px-2 pb-2 pt-1">
              <p className="type-label text-[var(--fg-0)]">
                会话设置
              </p>
            </div>

            <button
              type="button"
              role="menuitemcheckbox"
              aria-checked={fast}
              onClick={() => onFastChange(!fast)}
              className={cn(
                "type-control flex min-h-10 w-full items-center gap-2 rounded-[var(--radius-control)] px-2.5 text-left",
                "transition-colors focus-visible:outline-none focus-visible:shadow-[var(--ring)]",
                fast
                  ? "bg-[var(--accent-soft)] text-[var(--fg-0)]"
                  : "text-[var(--fg-1)] hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)]",
              )}
            >
              <Zap
                className={cn(
                  "h-4 w-4",
                  fast ? "text-[var(--accent)]" : "text-[var(--fg-2)]",
                )}
                fill={fast ? "currentColor" : "none"}
                aria-hidden
              />
              <span className="min-w-0 flex-1">
                Fast
              </span>
              <span
                aria-hidden
                className={cn(
                  "h-2 w-2 rounded-full",
                  fast ? "bg-[var(--accent)]" : "bg-[var(--fg-3)]",
                )}
              />
            </button>

            <div className="my-2 h-px bg-[var(--border-subtle)]" />

            <div className="list-group grid px-1">
              <div className="list-row flex min-h-10 items-center justify-between gap-3 px-1">
                <span className="type-caption">上下文</span>
                <ContextWindowMeter stats={contextStats} compact />
              </div>
              <div className="list-row flex min-h-10 items-center justify-between gap-3 px-1">
                <span className="type-caption">记忆</span>
                <ConversationMemoryButton />
              </div>
              <div className="list-row flex min-h-10 items-center justify-between gap-3 px-1">
                <span className="type-caption">
                  系统提示词
                </span>
                <SystemPromptManager compact />
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
