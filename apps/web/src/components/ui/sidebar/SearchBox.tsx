"use client";

// Sidebar 搜索框：客户端 filter 已加载会话的 title，
// debounce 350ms；Esc 清空。搜索入口保持显式输入框，不抢占全局命令面板快捷键。

import { useEffect, useRef, useState } from "react";
import { Search, X } from "lucide-react";
import { IconButton } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

export function SearchBox({
  value,
  onChange,
  className,
}: {
  value: string;
  onChange: (v: string) => void;
  className?: string;
}) {
  const [local, setLocal] = useState(value);
  const lastSyncedValueRef = useRef(value);
  const inputRef = useRef<HTMLInputElement | null>(null);

  // 父级 value 变化时同步到本地（外部清空 / 重置场景）。
  // 仅在父值变化时执行，避免覆盖用户输入中但还未 debounce flush 的中间态。
  useEffect(() => {
    if (lastSyncedValueRef.current !== value) {
      lastSyncedValueRef.current = value;
      setLocal(value);
    }
  }, [value]);

  // debounce 350ms 回调父级，避免每次击键都 re-filter
  useEffect(() => {
    if (local === value) return;
    const t = window.setTimeout(() => {
      onChange(local);
    }, 350);
    return () => window.clearTimeout(t);
  }, [local, onChange, value]);

  return (
    <div
      className={cn(
        "relative flex min-h-11 items-center rounded-[var(--radius-control)] border border-transparent bg-[var(--bg-0)]/68 transition-colors md:min-h-10",
        "focus-within:border-[var(--border)] focus-within:bg-[var(--bg-0)]",
        className,
      )}
    >
      <Search className="w-3.5 h-3.5 text-[var(--fg-2)] absolute left-2.5 pointer-events-none" />
      <input
        ref={inputRef}
        type="search"
        inputMode="search"
        placeholder="搜索会话"
        aria-label="搜索会话"
        value={local}
        onChange={(e) => setLocal(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Escape" && local) {
            e.preventDefault();
            e.stopPropagation();
            setLocal("");
            onChange("");
          }
        }}
        // 移动端用 16px 档防 iOS Safari 聚焦缩放；桌面回到紧凑正文档。
        className="h-full w-full bg-transparent pl-8 pr-10 type-card-title font-normal text-[var(--fg-0)] placeholder:text-[var(--fg-2)] outline-none"
      />
      {local && (
        <IconButton
          size="sm"
          variant="ghost"
          onClick={() => {
            setLocal("");
            onChange("");
            inputRef.current?.focus();
          }}
          aria-label="清除搜索"
          className="absolute right-0 text-[var(--fg-2)] md:right-1"
        >
          <X className="w-4 h-4" />
        </IconButton>
      )}
    </div>
  );
}
