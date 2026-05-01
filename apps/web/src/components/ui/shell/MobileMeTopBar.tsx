"use client";

import { Search, Settings, X } from "lucide-react";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { MobileTopBar } from "./MobileTopBar";
import { Pressable } from "@/components/ui/primitives/mobile/Pressable";

export function MobileMeTopBar({
  query,
  onQueryChange,
  userLabel,
  onAvatarTap,
}: {
  query: string;
  onQueryChange: (v: string) => void;
  userLabel?: string;
  onAvatarTap?: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (expanded) inputRef.current?.focus();
  }, [expanded]);

  return (
    <MobileTopBar
      left={
        expanded ? (
          <div className="flex-1 flex items-center gap-1.5 h-9 px-3 rounded-full bg-[var(--bg-2)] border border-[var(--border-subtle)]">
            <Search className="w-4 h-4 text-[var(--fg-2)]" />
            <input
              ref={inputRef}
              value={query}
              onChange={(e) => onQueryChange(e.target.value)}
              placeholder="搜索会话"
              className="flex-1 bg-transparent text-[14px] text-[var(--fg-0)] placeholder:text-[var(--fg-2)] outline-none"
            />
            <button
              type="button"
              aria-label="关闭搜索"
              onClick={() => {
                onQueryChange("");
                setExpanded(false);
              }}
              className="inline-flex items-center justify-center w-6 h-6 rounded-full text-[var(--fg-2)]"
            >
              <X className="w-3.5 h-3.5" />
            </button>
          </div>
        ) : (
          <button
            type="button"
            onClick={() => setExpanded(true)}
            aria-label="搜索会话"
            className="inline-flex items-center gap-2 h-9 px-3 rounded-full bg-[var(--bg-2)] border border-[var(--border-subtle)] text-[13px] text-[var(--fg-2)]"
          >
            <Search className="w-4 h-4" />
            <span>搜索会话</span>
          </button>
        )
      }
      right={
        <>
          <Pressable
            as="button"
            aria-label="账户"
            onPress={onAvatarTap}
            pressScale="tight"
            haptic="light"
            minHit
            className="inline-flex items-center justify-center w-11 h-11 rounded-full bg-[var(--bg-2)] border border-[var(--border-subtle)] text-[11px] text-[var(--fg-1)]"
          >
            {userLabel ? userLabel.slice(0, 1).toUpperCase() : "U"}
          </Pressable>
          <Link
            href="/settings/privacy"
            aria-label="设置"
            className="inline-flex items-center justify-center w-9 h-9 rounded-full text-[var(--fg-1)] hover:text-[var(--fg-0)]"
          >
            <Settings className="w-4 h-4" />
          </Link>
        </>
      }
    />
  );
}
