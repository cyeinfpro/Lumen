"use client";

import { Search, X } from "lucide-react";
import { type KeyboardEvent, useCallback, useEffect, useRef } from "react";

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
    const input = inputRef.current;
    if (open && input) {
      const frame = window.requestAnimationFrame(() => input.focus());
      return () => window.cancelAnimationFrame(frame);
    }
    if (!open && document.activeElement === input) {
      input?.blur();
    }
  }, [open]);

  const closeSearch = useCallback(() => {
    inputRef.current?.blur();
    onClose();
  }, [onClose]);

  const handleKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Escape") {
      e.preventDefault();
      closeSearch();
    }
  };

  return (
    <div className={`stream-collapse ${open ? "open" : ""}`}>
      <div>
        <div className="sticky top-0 z-10 bg-[var(--bg-0)]/88 px-3 py-2 backdrop-blur-xl md:static md:bg-transparent md:backdrop-blur-none">
          <div
            className={[
              "flex min-h-11 items-center gap-2 px-3",
              "rounded-[var(--radius-panel)] bg-[var(--bg-1)] border border-[var(--border-subtle)] shadow-[var(--shadow-1)]",
              "focus-within:border-[var(--border-amber)] focus-within:ring-2 focus-within:ring-[var(--accent)]/20",
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
              placeholder="搜索已加载作品…"
              aria-label="搜索已加载作品"
              className={[
                "flex-1 min-h-11 bg-transparent border-none outline-none",
                "type-body text-[var(--fg-0)] placeholder:text-[var(--fg-2)]",
              ].join(" ")}
            />
            {value && (
              <button
                type="button"
                onClick={() => onChange("")}
                aria-label="清空"
                className="inline-flex min-h-11 min-w-11 cursor-pointer items-center justify-center rounded-full text-[var(--fg-2)] hover:text-[var(--fg-0)] md:h-7 md:w-7 md:min-h-0 md:min-w-0 focus-visible:outline-none"
              >
                <X className="w-3.5 h-3.5" />
              </button>
            )}
            <button
              type="button"
              onClick={closeSearch}
              className="ml-1 min-h-11 cursor-pointer rounded-full px-2.5 text-[13px] text-[var(--fg-1)] hover:text-[var(--fg-0)] focus-visible:outline-none md:h-8 md:min-h-0"
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
              <span>个已加载结果</span>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
