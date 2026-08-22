"use client";

import { Archive, MoreHorizontal, Pencil, Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Button, IconButton, Input } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";
import type { AgentSession } from "../model/contracts";

export function AgentSessionItem({
  session,
  active,
  busy,
  onSelect,
  onRename,
  onArchive,
  onDelete,
}: {
  session: AgentSession;
  active: boolean;
  busy: boolean;
  onSelect: () => void;
  onRename: (title: string) => void;
  onArchive: () => void;
  onDelete: () => void;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [title, setTitle] = useState(session.title || "新会话");
  const rootRef = useRef<HTMLLIElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const menuButtonRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    if (!menuOpen) return;
    const onPointerDown = (event: PointerEvent) => {
      if (
        event.target instanceof Node &&
        !rootRef.current?.contains(event.target)
      ) {
        setMenuOpen(false);
      }
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setMenuOpen(false);
        window.requestAnimationFrame(() => menuButtonRef.current?.focus());
      }
    };
    window.addEventListener("pointerdown", onPointerDown, true);
    window.addEventListener("keydown", onKeyDown);
    const focusFrame = window.requestAnimationFrame(() => {
      menuRef.current?.querySelector<HTMLElement>('[role="menuitem"]')?.focus();
    });
    return () => {
      window.cancelAnimationFrame(focusFrame);
      window.removeEventListener("pointerdown", onPointerDown, true);
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [menuOpen]);

  const commitRename = () => {
    const next = title.trim() || "新会话";
    setTitle(next);
    setRenaming(false);
    if (next !== session.title) onRename(next);
  };

  return (
    <li ref={rootRef} data-agent-session-id={session.id} className="relative">
      {renaming ? (
        <form
          className="flex min-h-11 items-center gap-1 px-1"
          onSubmit={(event) => {
            event.preventDefault();
            commitRename();
          }}
        >
          <Input
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            onBlur={commitRename}
            aria-label="会话名称"
            autoFocus
            maxLength={255}
            className="h-9"
          />
        </form>
      ) : (
        <div
          className={cn(
            "group flex min-h-11 items-center rounded-[var(--radius-control)]",
            active ? "bg-accent-soft" : "hover:bg-[var(--bg-2)]",
          )}
        >
          <button
            type="button"
            onClick={onSelect}
            aria-current={active ? "page" : undefined}
            className="min-h-11 min-w-0 flex-1 truncate px-3 text-left type-body-sm text-[var(--fg-0)]"
          >
            <span className="block truncate">{session.title || "新会话"}</span>
            <span className="mt-0.5 block type-caption text-[var(--fg-2)]">
              {new Date(session.last_activity_at).toLocaleDateString()}
            </span>
          </button>
          <IconButton
            ref={menuButtonRef}
            size="sm"
            variant="ghost"
            aria-label="会话操作"
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            disabled={busy}
            onClick={() => setMenuOpen((open) => !open)}
            className="mr-1 shrink-0 opacity-70 group-hover:opacity-100"
          >
            <MoreHorizontal className="h-4 w-4" aria-hidden />
          </IconButton>
        </div>
      )}

      {menuOpen ? (
        <div
          ref={menuRef}
          role="menu"
          aria-label="会话操作"
          onKeyDown={(event) => {
            const items = Array.from(
              menuRef.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? [],
            );
            const current = items.indexOf(document.activeElement as HTMLElement);
            let target = current;
            if (event.key === "ArrowDown") target = (current + 1) % items.length;
            else if (event.key === "ArrowUp") target = (current - 1 + items.length) % items.length;
            else if (event.key === "Home") target = 0;
            else if (event.key === "End") target = items.length - 1;
            else return;
            event.preventDefault();
            items[target]?.focus();
          }}
          className="surface-panel absolute right-1 top-10 z-[var(--z-tray)] min-w-36 p-1"
        >
          <MenuAction
            icon={<Pencil className="h-3.5 w-3.5" />}
            label="重命名"
            onClick={() => {
              setMenuOpen(false);
              setRenaming(true);
            }}
          />
          <MenuAction
            icon={<Archive className="h-3.5 w-3.5" />}
            label={session.archived ? "取消归档" : "归档"}
            onClick={() => {
              setMenuOpen(false);
              onArchive();
            }}
          />
          <MenuAction
            danger
            icon={<Trash2 className="h-3.5 w-3.5" />}
            label="删除"
            onClick={() => {
              setMenuOpen(false);
              onDelete();
            }}
          />
        </div>
      ) : null}
    </li>
  );
}

function MenuAction({
  icon,
  label,
  danger = false,
  onClick,
}: {
  icon: React.ReactNode;
  label: string;
  danger?: boolean;
  onClick: () => void;
}) {
  return (
    <Button
      role="menuitem"
      variant="ghost"
      size="sm"
      onClick={onClick}
      leftIcon={icon}
      className={cn(
        "min-h-10 w-full justify-start px-2",
        danger && "text-[var(--danger-fg)] hover:bg-danger-soft",
      )}
    >
      {label}
    </Button>
  );
}
