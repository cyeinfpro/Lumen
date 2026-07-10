"use client";

// Sidebar 搜索框：客户端 filter 已加载会话的 title，
// debounce 350ms；Esc 清空。搜索入口保持显式输入框，不抢占全局命令面板快捷键。

import { useEffect, useRef, useState } from "react";
import { Search, X } from "lucide-react";
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
  // 仅在父值变化时执行，避免覆盖用户正在输入但尚未 debounce flush 的中间态。
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
        "relative flex h-10 items-center rounded-[var(--radius-control)] border border-transparent bg-[var(--bg-0)]/68 transition-colors md:h-9",
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
        // 移动端 font-size 16px 防 iOS Safari 聚焦缩放；桌面端回到 14px
        className="w-full h-full bg-transparent pl-8 pr-10 text-base md:text-sm text-[var(--fg-0)] placeholder:text-[var(--fg-2)] outline-none"
      />
      {local && (
        <button
          type="button"
          onClick={() => {
            setLocal("");
            onChange("");
            inputRef.current?.focus();
          }}
          aria-label="清除搜索"
          className="absolute right-1 w-8 h-8 inline-flex items-center justify-center rounded text-[var(--fg-2)] hover:text-[var(--fg-0)] hover:bg-white/5 transition-colors"
        >
          <X className="w-4 h-4" />
        </button>
      )}
    </div>
  );
}
