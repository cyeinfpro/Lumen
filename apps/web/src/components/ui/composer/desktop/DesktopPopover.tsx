"use client";

// Darkroom 桌面端轻量 Popover 原语。
// 以 viewport fixed + portal 渲染，避免被 scroll / overflow 容器裁剪。
// 默认挂在 trigger 上方 8px，支持 ESC / 点外关闭。

import { AnimatePresence, motion } from "framer-motion";
import {
  type RefObject,
  type ReactNode,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import { cn } from "@/lib/utils";
import { DURATION, EASE } from "@/lib/motion";

export interface DesktopPopoverProps {
  open: boolean;
  onClose: () => void;
  /** 触发器容器，用于 viewport fixed 定位 */
  anchorRef: RefObject<HTMLElement | null>;
  /** Popover 内容（通常是列表/表单） */
  children: ReactNode;
  /** 可访问名 */
  ariaLabel?: string;
  /** 对齐方式：相对 trigger 的 left / center / right */
  align?: "left" | "center" | "right";
  className?: string;
}

/**
 * 使用方式：
 *   <div className="relative">
 *     <button ...>Trigger</button>
 *     <DesktopPopover open={open} onClose={...}>...</DesktopPopover>
 *   </div>
 *
 * 自身位于 trigger 上方 8px，max-h 360 overflow-auto，min-w 220。
 */
export function DesktopPopover({
  open,
  onClose,
  anchorRef,
  children,
  ariaLabel,
  align = "left",
  className,
}: DesktopPopoverProps) {
  const panelRef = useRef<HTMLDivElement | null>(null);
  const onCloseRef = useRef(onClose);
  const [position, setPosition] = useState<{
    left: number;
    top: number;
    translateX: string;
  } | null>(null);

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  const updatePosition = useCallback(() => {
    const anchor = anchorRef.current;
    const panel = panelRef.current;
    if (!anchor || !panel) return;

    const anchorRect = anchor.getBoundingClientRect();
    const panelRect = panel.getBoundingClientRect();
    const viewportW = window.innerWidth;
    const viewportH = window.innerHeight;
    const gutter = 12;
    let left = anchorRect.left;
    let translateX = "0";

    if (align === "center") {
      left = anchorRect.left + anchorRect.width / 2;
      translateX = "-50%";
    } else if (align === "right") {
      left = anchorRect.right;
      translateX = "-100%";
    }

    if (align === "left") {
      left = Math.min(
        Math.max(left, gutter),
        Math.max(gutter, viewportW - panelRect.width - gutter),
      );
    } else if (align === "center") {
      left = Math.min(
        Math.max(left, gutter + panelRect.width / 2),
        Math.max(gutter + panelRect.width / 2, viewportW - gutter - panelRect.width / 2),
      );
    } else {
      left = Math.min(
        Math.max(left, gutter + panelRect.width),
        Math.max(gutter + panelRect.width, viewportW - gutter),
      );
    }

    const topAbove = anchorRect.top - panelRect.height - 8;
    const topBelow = anchorRect.bottom + 8;
    const top =
      topAbove >= gutter
        ? topAbove
        : Math.min(topBelow, Math.max(gutter, viewportH - panelRect.height - gutter));

    setPosition({ left, top, translateX });
  }, [align, anchorRef]);

  // ESC 关闭
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onCloseRef.current();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  // 点外关闭（mousedown 监听：避免与按钮 click 打架）
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      const panel = panelRef.current;
      const anchor = anchorRef.current;
      if (!panel) return;
      const target = e.target as Node | null;
      if (!target) return;
      if (panel.contains(target)) return;
      if (anchor?.contains(target)) return;
      // trigger 也算外部——关闭；若外层希望 trigger 切换 open，调用方已在 onClick 处 setOpen
      onCloseRef.current();
    };
    // 用 mousedown 而不是 click：避免 trigger 的 click toggle 后立刻关闭
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [anchorRef, open]);

  useEffect(() => {
    if (!open) return;
    const frame = window.requestAnimationFrame(() => updatePosition());
    const onViewportChange = () => updatePosition();
    window.addEventListener("resize", onViewportChange);
    window.addEventListener("scroll", onViewportChange, true);
    return () => {
      window.cancelAnimationFrame(frame);
      window.removeEventListener("resize", onViewportChange);
      window.removeEventListener("scroll", onViewportChange, true);
    };
  }, [open, updatePosition]);

  const originClass = useCallback(() => {
    if (align === "right") return "origin-bottom-right";
    if (align === "center") return "origin-bottom";
    return "origin-bottom-left";
  }, [align]);

  if (typeof document === "undefined") return null;

  return createPortal(
    <AnimatePresence>
      {open && (
        <motion.div
          ref={panelRef}
          data-lumen-composer-floating
          role="dialog"
          aria-label={ariaLabel}
          initial={{ opacity: 0, scale: 0.96, y: 4 }}
          animate={{ opacity: 1, scale: 1, y: 0 }}
          exit={{ opacity: 0, scale: 0.96, y: 4 }}
          transition={{ duration: DURATION.quick, ease: EASE.develop }}
          className={cn(
            "fixed z-[var(--z-tray,50)]",
            originClass(),
            "min-w-[220px] max-h-[360px] overflow-auto",
            "rounded-xl bg-[var(--bg-1)] border border-[var(--border-subtle)]",
            "shadow-[var(--shadow-2)] backdrop-blur-xl",
            "p-1",
            className,
          )}
          style={{
            left: position?.left ?? -9999,
            top: position?.top ?? -9999,
            transform: `translate(${position?.translateX ?? "0"}, 0)`,
          }}
        >
          {children}
        </motion.div>
      )}
    </AnimatePresence>,
    document.body,
  );
}

// ———————————————————————————————————————————————————
// 配套列表组件：跟 BottomSheet 的 SheetList 相近，但卡片化、更紧凑
// ———————————————————————————————————————————————————

export interface PopoverListItem {
  key: string;
  label: string;
  hint?: string;
  selected: boolean;
  onSelect: () => void;
}

export function PopoverList({
  title,
  items,
}: {
  title?: string;
  items: PopoverListItem[];
}) {
  return (
    <div className="flex flex-col">
      {title && (
        <div className="px-3 pt-2 pb-1.5 text-[11px] font-medium uppercase tracking-wider text-[var(--fg-2)]">
          {title}
        </div>
      )}
      <ul className="flex flex-col">
        {items.map((it) => (
          <li key={it.key}>
            <button
              type="button"
              onClick={it.onSelect}
              className={cn(
                "w-full h-10 flex items-center gap-3 px-3 text-left rounded-lg",
                "text-[13px] transition-colors",
                "hover:bg-[var(--bg-2)]",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
                it.selected ? "text-[var(--amber-300)]" : "text-[var(--fg-0)]",
              )}
            >
              <span className="flex-1 truncate">{it.label}</span>
              {it.hint && (
                <span className="text-[11px] text-[var(--fg-2)] shrink-0">
                  {it.hint}
                </span>
              )}
              {it.selected && (
                <span
                  aria-hidden
                  className="w-1.5 h-1.5 rounded-full bg-[var(--amber-400)] shadow-[var(--shadow-amber)]"
                />
              )}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
