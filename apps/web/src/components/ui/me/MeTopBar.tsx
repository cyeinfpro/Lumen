"use client";

import { Plus, Search, Settings, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { MobileTopBar } from "@/components/ui/shell/MobileTopBar";
import { IconButton } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

export interface MeTopBarProps {
  query: string;
  onQueryChange: (v: string) => void;
  userLabel?: string;
  /** 顶栏右上角齿轮按钮回调 —— 由父组件控制 AccountSheet 开关 */
  onSettingsTap?: () => void;
  /** 顶栏 "+" 按钮回调 —— 新建会话 */
  onCreateConversation?: () => void;
  /** "+" 按钮 disabled 状态（mutation pending） */
  createPending?: boolean;
  /** @deprecated 历史 prop，保留兼容 */
  onAvatarTap?: () => void;
}

export function MeTopBar({
  query,
  onQueryChange,
  onSettingsTap,
  onCreateConversation,
  createPending,
}: MeTopBarProps) {
  const [expanded, setExpanded] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (expanded) inputRef.current?.focus();
  }, [expanded]);

  return (
    <MobileTopBar
      left={
        expanded ? (
          <div
            className={cn(
              "flex-1 flex items-center gap-2 h-9 px-3 rounded-full",
              "bg-[var(--bg-2)] border border-[var(--border-subtle)]",
              "focus-within:border-[var(--amber-400)]/40",
              "transition-colors",
            )}
          >
            <Search className="w-4 h-4 text-[var(--fg-2)] shrink-0" />
            <input
              ref={inputRef}
              value={query}
              onChange={(e) => onQueryChange(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Escape") {
                  onQueryChange("");
                  setExpanded(false);
                }
              }}
              placeholder="搜索会话"
              aria-label="搜索会话"
              className={cn(
                "flex-1 bg-transparent text-[14px] text-[var(--fg-0)]",
                "placeholder:text-[var(--fg-2)] outline-none",
              )}
            />
            <IconButton
              size="sm"
              variant="ghost"
              aria-label="关闭搜索"
              onClick={() => {
                onQueryChange("");
                setExpanded(false);
              }}
              className="w-7 h-7 max-sm:min-h-7 max-sm:min-w-7 -mr-1 rounded-full"
            >
              <X className="w-3.5 h-3.5" />
            </IconButton>
          </div>
        ) : (
          <span className="type-section-title pl-0.5 tracking-tight">
            我的
          </span>
        )
      }
      right={
        !expanded ? (
          <>
            <IconButton
              size="md"
              variant="ghost"
              onClick={() => setExpanded(true)}
              aria-label="搜索会话"
              className="rounded-full"
            >
              <Search className="w-[18px] h-[18px]" />
            </IconButton>
            {onCreateConversation && (
              <IconButton
                size="md"
                variant="ghost"
                onClick={onCreateConversation}
                disabled={createPending}
                aria-label="新建会话"
                className="rounded-full text-[var(--amber-400)] hover:text-[var(--amber-400)]"
                style={{ filter: "drop-shadow(0 0 6px var(--amber-glow))" }}
              >
                <Plus className="w-[18px] h-[18px]" strokeWidth={2.4} />
              </IconButton>
            )}
            {onSettingsTap && (
              <IconButton
                size="md"
                variant="ghost"
                onClick={onSettingsTap}
                aria-label="账户与设置"
                className="rounded-full"
              >
                <Settings className="w-[18px] h-[18px]" />
              </IconButton>
            )}
          </>
        ) : undefined
      }
    />
  );
}
