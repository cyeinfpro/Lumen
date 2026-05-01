"use client";

// 单条会话 item：hover 出 more 菜单，点击菜单再确认（内嵌 popover，不用 window.confirm）。
// 用 ref 挂载 focus/scrollIntoView，方便父级键盘导航定位。

import { forwardRef, useEffect, useRef, useState } from "react";
import {
  Archive,
  ArchiveRestore,
  Check,
  Loader2,
  MessageSquare,
  MoreHorizontal,
  Pencil,
  Trash2,
  X,
} from "lucide-react";
import type { ConversationSummary } from "@/lib/apiClient";
import { cn } from "@/lib/utils";

export function titleOf(c: ConversationSummary): string {
  const t = c.title?.trim();
  return t || "New Canvas";
}

type MenuView = "closed" | "menu" | "rename" | "confirmDelete";

export interface ConversationItemProps {
  conv: ConversationSummary;
  active: boolean;
  deleting?: boolean;
  renaming?: boolean;
  archiving?: boolean;
  onSelect: () => void;
  onRename: (title: string) => void;
  onArchive: (nextArchived: boolean) => void;
  onDelete: () => void;
}

export const ConversationItem = forwardRef<HTMLLIElement, ConversationItemProps>(
  function ConversationItem(
    {
      conv,
      active,
      deleting,
      renaming,
      archiving,
      onSelect,
      onRename,
      onArchive,
      onDelete,
    },
    ref,
  ) {
    const [view, setView] = useState<MenuView>("closed");
    const [renameValue, setRenameValue] = useState(titleOf(conv));
    const rootRef = useRef<HTMLDivElement | null>(null);
    const renameInputRef = useRef<HTMLInputElement | null>(null);

    // iOS Safari 上 autoFocus 在 popover 动画期间常被忽略；进入 rename 视图后
    // 用一帧后的显式 focus 更稳定。
    useEffect(() => {
      if (view !== "rename") return;
      const t = window.setTimeout(() => {
        const el = renameInputRef.current;
        if (!el) return;
        el.focus();
        el.select();
      }, 30);
      return () => window.clearTimeout(t);
    }, [view]);

    // 外部点击 / Esc 关闭 popover
    useEffect(() => {
      if (view === "closed") return;
      const onDoc = (e: MouseEvent) => {
        if (!rootRef.current) return;
        if (rootRef.current.contains(e.target as Node)) return;
        setView("closed");
      };
      const onKey = (e: KeyboardEvent) => {
        if (e.key === "Escape") setView("closed");
      };
      document.addEventListener("mousedown", onDoc);
      document.addEventListener("keydown", onKey);
      return () => {
        document.removeEventListener("mousedown", onDoc);
        document.removeEventListener("keydown", onKey);
      };
    }, [view]);

    const openRename = () => {
      setRenameValue(titleOf(conv));
      setView("rename");
    };

    const busy = Boolean(deleting || renaming || archiving);

    return (
      <li ref={ref} className="group relative" data-conv-id={conv.id}>
        <div ref={rootRef} className="relative">
          <button
            type="button"
            onClick={onSelect}
            aria-current={active ? "true" : undefined}
            aria-label={titleOf(conv)}
            title={titleOf(conv)}
            className={cn(
              "w-full flex items-center gap-2.5 pl-2.5 pr-11 h-11 md:h-10 text-sm rounded-md text-left transition-colors outline-none",
              "focus-visible:ring-2 focus-visible:ring-[var(--accent)]/60",
              active
                ? "bg-[var(--accent)]/12 text-[var(--fg-0)] shadow-[inset_2px_0_0_var(--accent)]"
                : "text-neutral-300 hover:bg-white/[0.04] hover:text-white",
            )}
          >
            <MessageSquare
              className={cn(
                "w-3.5 h-3.5 shrink-0",
                active ? "text-[var(--accent)]" : "text-neutral-500",
              )}
            />
            <span className="truncate flex-1">{titleOf(conv)}</span>
            {conv.archived && (
              <Archive className="w-3 h-3 shrink-0 text-neutral-500" />
            )}
          </button>

          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              setView((v) => (v === "menu" ? "closed" : "menu"));
            }}
            disabled={busy}
            aria-label="更多操作"
            aria-haspopup="menu"
            aria-expanded={view !== "closed"}
            className={cn(
              "absolute right-1 top-1/2 -translate-y-1/2 w-9 h-9 md:w-7 md:h-7 inline-flex items-center justify-center rounded-md text-neutral-400 hover:text-white hover:bg-white/10 transition-all",
              "focus-visible:opacity-100 focus-visible:ring-2 focus-visible:ring-[var(--accent)]/60",
              // 移动端常显（<md 触控设备没有 hover），桌面端 hover 才显
              view !== "closed"
                ? "opacity-100"
                : "opacity-100 md:opacity-0 md:group-hover:opacity-100",
              busy && "pointer-events-none",
            )}
          >
            {busy ? (
              <Loader2 className="w-3.5 h-3.5 animate-spin" />
            ) : (
              <MoreHorizontal className="w-3.5 h-3.5" />
            )}
          </button>

          {/* 动作菜单 —— z-50 高于移动端抽屉(z-40) */}
          {view === "menu" && (
            <div
              role="menu"
              onClick={(e) => e.stopPropagation()}
              className="absolute right-0 top-full mt-1 z-50 w-40 rounded-lg border border-white/10 bg-[var(--bg-2)]/95 backdrop-blur-xl shadow-lumen-card py-1"
            >
              <MenuButton
                icon={<Pencil className="w-3.5 h-3.5" />}
                onClick={(e) => {
                  e.stopPropagation();
                  openRename();
                }}
              >
                重命名
              </MenuButton>
              <MenuButton
                icon={
                  conv.archived ? (
                    <ArchiveRestore className="w-3.5 h-3.5" />
                  ) : (
                    <Archive className="w-3.5 h-3.5" />
                  )
                }
                onClick={(e) => {
                  e.stopPropagation();
                  onArchive(!conv.archived);
                  setView("closed");
                }}
              >
                {conv.archived ? "取消归档" : "归档"}
              </MenuButton>
              <div className="my-1 border-t border-white/5" />
              <MenuButton
                icon={<Trash2 className="w-3.5 h-3.5" />}
                danger
                onClick={(e) => {
                  e.stopPropagation();
                  setView("confirmDelete");
                }}
              >
                删除
              </MenuButton>
            </div>
          )}

          {/* 重命名 popover */}
          {view === "rename" && (
            <form
              role="dialog"
              onClick={(e) => e.stopPropagation()}
              onSubmit={(e) => {
                e.preventDefault();
                e.stopPropagation();
                const next = renameValue.trim();
                if (next && next !== titleOf(conv)) {
                  onRename(next);
                }
                setView("closed");
              }}
              className="absolute right-0 top-full mt-1 z-50 w-64 p-2 rounded-lg border border-white/10 bg-[var(--bg-2)]/95 backdrop-blur-xl shadow-lumen-card"
            >
              <input
                ref={renameInputRef}
                type="text"
                value={renameValue}
                onChange={(e) => setRenameValue(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Escape") {
                    e.stopPropagation();
                    setView("closed");
                  }
                }}
                onClick={(e) => e.stopPropagation()}
                maxLength={120}
                className="w-full h-8 px-2 text-sm bg-white/5 border border-white/10 rounded-md outline-none focus:border-[var(--accent)]/60 text-neutral-100 placeholder:text-neutral-500"
                placeholder="会话标题"
              />
              <div className="flex gap-1.5 mt-1.5 justify-end">
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation();
                    setView("closed");
                  }}
                  className="px-2 h-7 text-xs rounded-md text-neutral-400 hover:text-white hover:bg-white/5 transition-colors"
                >
                  取消
                </button>
                <button
                  type="submit"
                  onClick={(e) => e.stopPropagation()}
                  className="inline-flex items-center gap-1 px-2.5 h-7 text-xs rounded-md bg-[var(--accent)]/20 text-[var(--accent)] hover:bg-[var(--accent)]/30 transition-colors"
                >
                  <Check className="w-3 h-3" />
                  保存
                </button>
              </div>
            </form>
          )}

          {/* 删除确认 popover */}
          {view === "confirmDelete" && (
            <div
              role="dialog"
              onClick={(e) => e.stopPropagation()}
              className="absolute right-0 top-full mt-1 z-50 w-64 p-2.5 rounded-lg border border-red-500/20 bg-[var(--bg-2)]/95 backdrop-blur-xl shadow-lumen-card"
            >
              <p className="text-xs text-neutral-300 leading-snug px-0.5 mb-2">
                确认删除会话
                <span className="text-neutral-100 font-medium mx-1">
                  「{titleOf(conv)}」
                </span>
                ？此操作不可恢复。
              </p>
              <div className="flex gap-1.5 justify-end">
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation();
                    setView("closed");
                  }}
                  className="inline-flex items-center gap-1 px-2 h-7 text-xs rounded-md text-neutral-400 hover:text-white hover:bg-white/5 transition-colors"
                >
                  <X className="w-3 h-3" />
                  取消
                </button>
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation();
                    onDelete();
                    setView("closed");
                  }}
                  className="inline-flex items-center gap-1 px-2.5 h-7 text-xs rounded-md bg-red-500/20 text-red-300 hover:bg-red-500/30 transition-colors"
                >
                  <Trash2 className="w-3 h-3" />
                  删除
                </button>
              </div>
            </div>
          )}
        </div>
      </li>
    );
  },
);

function MenuButton({
  children,
  icon,
  onClick,
  danger,
}: {
  children: React.ReactNode;
  icon: React.ReactNode;
  onClick: (e: React.MouseEvent<HTMLButtonElement>) => void;
  danger?: boolean;
}) {
  return (
    <button
      type="button"
      role="menuitem"
      onClick={onClick}
      className={cn(
        "w-full flex items-center gap-2 px-2.5 py-2.5 md:py-1.5 text-xs text-left transition-colors",
        "active:scale-[0.98]",
        danger
          ? "text-red-300 hover:bg-red-500/10 hover:text-red-200"
          : "text-neutral-300 hover:bg-white/5 hover:text-white",
      )}
    >
      <span className="shrink-0">{icon}</span>
      {children}
    </button>
  );
}
