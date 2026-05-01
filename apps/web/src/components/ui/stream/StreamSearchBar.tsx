"use client";

import { Search, X } from "lucide-react";
import { type KeyboardEvent, useEffect, useRef } from "react";

export interface StreamSearchBarProps {
  open: boolean;
  value: string;
  onChange: (v: string) => void;
  onClose: () => void;
  resultCount?: number;
  loadedCount?: number;
}

export function StreamSearchBar({
  open,
  value,
  onChange,
  onClose,
  resultCount,
  loadedCount,
}: StreamSearchBarProps) {
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (open && inputRef.current) {
      inputRef.current.focus();
    }
  }, [open]);

  const handleKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Escape") {
      e.preventDefault();
      onClose();
    }
  };

  return (
    <div className={`stream-collapse ${open ? "open" : ""}`}>
      <div>
        <div className="px-3 py-2">
          <div
            className={[
              "flex min-h-11 items-center gap-2 px-3",
              "rounded-xl bg-[var(--bg-1)] border border-[var(--border-subtle)]",
              "focus-within:border-[var(--border-amber)] focus-within:shadow-[0_0_0_3px_rgba(242,169,58,0.1)]",
              "transition-[border-color,box-shadow] duration-200",
            ].join(" ")}
          >
            <Search
              aria-hidden
              className="w-4 h-4 text-[var(--fg-2)] shrink-0"
            />
            <input
              ref={inputRef}
              type="text"
              value={value}
              onChange={(e) => onChange(e.target.value)}
              onKeyDown={handleKey}
              placeholder="搜索 prompt…"
              aria-label="搜索已加载作品"
              className={[
                "flex-1 bg-transparent border-none outline-none",
                "text-[15px] text-[var(--fg-0)] placeholder:text-[var(--fg-2)]",
              ].join(" ")}
            />
            {value && (
              <button
                type="button"
                onClick={() => onChange("")}
                aria-label="清空"
                className="inline-flex h-7 w-7 cursor-pointer items-center justify-center rounded-full text-[var(--fg-2)] hover:text-[var(--fg-0)] focus-visible:outline-none"
              >
                <X className="w-3.5 h-3.5" />
              </button>
            )}
            <button
              type="button"
              onClick={onClose}
              className="ml-1 h-8 cursor-pointer rounded-full px-2.5 text-[13px] text-[var(--fg-1)] hover:text-[var(--fg-0)] focus-visible:outline-none"
              aria-label="关闭搜索"
            >
              取消
            </button>
          </div>
          {typeof resultCount === "number" && value.trim() && (
            <div className="mt-1.5 flex items-center gap-2 px-1 text-[11px] text-[var(--fg-2)]">
              <span className="inline-flex h-5 items-center rounded-full bg-[var(--bg-2)] px-2 tabular-nums">
                {resultCount} / {loadedCount ?? resultCount}
              </span>
              <span>条匹配</span>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
