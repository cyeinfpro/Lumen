"use client";

// Sidebar 搜索框：客户端 filter 已加载会话的 title，
// debounce 350ms；Esc 清空；⌘/Ctrl+K 聚焦（全局，侧栏可见时才启用；input 不可见时忽略）。

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

  // ⌘/Ctrl+K 聚焦搜索框（仅在 input 可见时响应，避免移动端抽屉收起时抢焦点）
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const isK = e.key === "k" || e.key === "K";
      if (!isK) return;
      if (!(e.metaKey || e.ctrlKey)) return;
      const el = inputRef.current;
      if (!el) return;
      // offsetParent 为 null 代表元素不可见（display:none 或祖先隐藏）
      if (!el.offsetParent) return;
      e.preventDefault();
      el.focus();
      el.select();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);

  return (
    <div
      className={cn(
        "relative flex items-center h-10 md:h-9 rounded-lg bg-white/[0.04] border border-white/10 focus-within:border-[var(--accent)]/60 focus-within:bg-white/[0.06] transition-colors",
        className,
      )}
    >
      <Search className="w-3.5 h-3.5 text-neutral-500 absolute left-2.5 pointer-events-none" />
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
        className="w-full h-full bg-transparent pl-8 pr-10 text-base md:text-sm text-neutral-100 placeholder:text-neutral-500 outline-none"
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
          className="absolute right-1 w-8 h-8 inline-flex items-center justify-center rounded text-neutral-500 hover:text-neutral-200 hover:bg-white/5 transition-colors"
        >
          <X className="w-4 h-4" />
        </button>
      )}
      <kbd
        aria-hidden
        className={cn(
          "absolute right-2 px-1.5 py-0.5 rounded text-[10px] font-mono text-neutral-500 bg-white/5 border border-white/10 pointer-events-none transition-opacity",
          local ? "opacity-0" : "opacity-100",
        )}
      >
        ⌘K
      </kbd>
    </div>
  );
}
