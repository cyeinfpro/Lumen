"use client";

import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import {
  Command,
  Keyboard,
  LayoutGrid,
  MousePointer2,
  Move,
  X,
  type LucideIcon,
} from "lucide-react";
import {
  useCallback,
  useId,
  useRef,
  useSyncExternalStore,
} from "react";

import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import { DURATION, EASE } from "@/lib/motion";
import { cn } from "@/lib/utils";
import { IconButton, Kbd } from "@/components/ui/primitives";
import { useModalLayer } from "@/components/ui/primitives/mobile/useModalLayer";

export interface CanvasShortcut {
  id: string;
  label: string;
  keys: readonly string[];
  description?: string;
}

export interface CanvasShortcutGroup {
  id: string;
  title: string;
  icon: LucideIcon;
  shortcuts: readonly CanvasShortcut[];
}

export interface CanvasShortcutsDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  groups?: readonly CanvasShortcutGroup[];
  title?: string;
  description?: string;
  className?: string;
}

export const DEFAULT_CANVAS_SHORTCUT_GROUPS: readonly CanvasShortcutGroup[] = [
  {
    id: "general",
    title: "通用",
    icon: Command,
    shortcuts: [
      {
        id: "command-menu",
        label: "打开画布命令菜单",
        keys: ["Mod", "Shift", "K"],
      },
      { id: "undo", label: "撤销", keys: ["Mod", "Z"] },
      { id: "redo", label: "重做", keys: ["Mod", "Shift", "Z"] },
      { id: "close", label: "关闭或取消", keys: ["Esc"] },
    ],
  },
  {
    id: "viewport",
    title: "视图",
    icon: Move,
    shortcuts: [
      { id: "zoom-in", label: "放大", keys: ["+"] },
      { id: "zoom-out", label: "缩小", keys: ["-"] },
      { id: "zoom-reset", label: "重置为 100%", keys: ["0"] },
      { id: "fit-view", label: "适应画布", keys: ["Mod", "0"] },
      { id: "toggle-grid", label: "切换网格", keys: ["G"] },
      { id: "toggle-minimap", label: "切换小地图", keys: ["M"] },
    ],
  },
  {
    id: "selection",
    title: "选择与编辑",
    icon: MousePointer2,
    shortcuts: [
      { id: "multi-select", label: "追加选择", keys: ["Shift", "点击"] },
      { id: "select-all", label: "选择全部节点", keys: ["Mod", "A"] },
      { id: "copy", label: "复制选区", keys: ["Mod", "C"] },
      { id: "paste", label: "粘贴节点", keys: ["Mod", "V"] },
      { id: "duplicate", label: "重复选区", keys: ["Mod", "D"] },
      { id: "fit-selection", label: "适应选区", keys: ["Shift", "2"] },
      { id: "delete", label: "删除选区", keys: ["Delete"] },
    ],
  },
  {
    id: "layout",
    title: "节点与布局",
    icon: LayoutGrid,
    shortcuts: [
      { id: "add-node", label: "添加节点", keys: ["/"] },
      { id: "auto-layout", label: "自动布局", keys: ["Shift", "A"] },
      { id: "run-node", label: "运行节点", keys: ["Mod", "Enter"] },
      { id: "pan", label: "临时平移", keys: ["Space", "拖动"] },
    ],
  },
];

const MODIFIER_SUBSCRIBE_NOOP = (): (() => void) => () => {};
const MODIFIER_SERVER_SNAPSHOT = (): "Ctrl" => "Ctrl";

function detectModifierLabel(): "⌘" | "Ctrl" {
  if (typeof navigator === "undefined") return "Ctrl";
  return /Mac|iPhone|iPad|iPod/i.test(navigator.platform || "") ? "⌘" : "Ctrl";
}

export function CanvasShortcutsDialog({
  open,
  onOpenChange,
  groups = DEFAULT_CANVAS_SHORTCUT_GROUPS,
  title = "画布快捷键",
  description = "查看画布导航、选择与布局操作。",
  className,
}: CanvasShortcutsDialogProps) {
  const reduceMotion = useReducedMotion();
  const headingId = useId();
  const descriptionId = useId();
  const dialogRef = useRef<HTMLElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const modifierLabel = useSyncExternalStore<string>(
    MODIFIER_SUBSCRIBE_NOOP,
    detectModifierLabel,
    MODIFIER_SERVER_SNAPSHOT,
  );
  useBodyScrollLock(open);

  const closeDialog = useCallback(() => {
    onOpenChange(false);
  }, [onOpenChange]);
  const onDialogKeyDown = useModalLayer({
    open,
    rootRef: dialogRef,
    onClose: closeDialog,
    initialFocusRef: closeButtonRef,
  });

  return (
    <AnimatePresence>
      {open ? (
        <motion.div
          key="canvas-shortcuts-dialog"
          className="mobile-dialog-shell fixed inset-0 z-[var(--z-dialog)] flex items-end justify-center p-0 sm:items-center sm:px-4"
          initial={reduceMotion ? false : { opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: reduceMotion ? 0 : DURATION.quick }}
        >
          <button
            type="button"
            aria-label="关闭快捷键"
            tabIndex={-1}
            className="absolute inset-0 cursor-default bg-[var(--surface-scrim)]"
            onClick={closeDialog}
          />
          <motion.section
            ref={dialogRef}
            role="dialog"
            aria-modal="true"
            aria-labelledby={headingId}
            aria-describedby={descriptionId}
            tabIndex={-1}
            onKeyDown={onDialogKeyDown}
            initial={
              reduceMotion ? false : { opacity: 0, scale: 0.98, y: 12 }
            }
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={
              reduceMotion
                ? { opacity: 0 }
                : { opacity: 0, scale: 0.98, y: 12 }
            }
            transition={{
              duration: reduceMotion ? 0 : DURATION.normal,
              ease: EASE.develop,
            }}
            className={cn(
              "mobile-dialog-panel surface-dialog relative flex max-h-[86dvh] w-full max-w-3xl flex-col overflow-hidden",
              "max-sm:rounded-t-[var(--radius-sheet)] max-sm:rounded-b-none max-sm:border-b-0",
              className,
            )}
          >
            <header className="flex shrink-0 items-start gap-3 border-b border-[var(--border)] px-5 py-4">
              <span
                className="grid h-9 w-9 shrink-0 place-items-center rounded-[var(--radius-control)] bg-[var(--accent-soft)] text-[var(--accent)]"
                aria-hidden
              >
                <Keyboard className="h-[18px] w-[18px]" />
              </span>
              <div className="min-w-0 flex-1">
                <h2 id={headingId} className="type-card-title">
                  {title}
                </h2>
                <p
                  id={descriptionId}
                  className="mt-1 type-body-sm text-[var(--fg-2)]"
                >
                  {description}
                </p>
              </div>
              <IconButton
                ref={closeButtonRef}
                aria-label="关闭快捷键"
                size="lg"
                onClick={closeDialog}
              >
                <X className="h-4 w-4" aria-hidden />
              </IconButton>
            </header>

            <div className="mobile-dialog-scroll min-h-0 flex-1 overflow-y-auto p-5">
              {groups.length > 0 ? (
                <div className="grid gap-x-8 gap-y-6 sm:grid-cols-2">
                  {groups.map((group) => (
                    <ShortcutGroup
                      key={group.id}
                      group={group}
                      modifierLabel={modifierLabel}
                    />
                  ))}
                </div>
              ) : (
                <div
                  role="status"
                  className="grid min-h-48 place-items-center text-center type-body-sm text-[var(--fg-2)]"
                >
                  暂无快捷键
                </div>
              )}
            </div>
          </motion.section>
        </motion.div>
      ) : null}
    </AnimatePresence>
  );
}

function ShortcutGroup({
  group,
  modifierLabel,
}: {
  group: CanvasShortcutGroup;
  modifierLabel: string;
}) {
  const GroupIcon = group.icon;
  const headingId = useId();
  return (
    <section aria-labelledby={headingId} className="min-w-0">
      <div className="flex items-center gap-2 border-b border-[var(--border)] pb-2">
        <GroupIcon className="h-4 w-4 text-[var(--accent)]" aria-hidden />
        <h3 id={headingId} className="type-body-sm font-medium text-[var(--fg-0)]">
          {group.title}
        </h3>
      </div>
      <div className="divide-y divide-[var(--border-subtle)]">
        {group.shortcuts.map((shortcut) => (
          <div
            key={shortcut.id}
            className="flex min-h-11 items-center justify-between gap-4 py-2"
          >
            <div className="min-w-0">
              <p className="type-body-sm text-[var(--fg-1)]">
                {shortcut.label}
              </p>
              {shortcut.description ? (
                <p className="mt-0.5 type-caption text-[var(--fg-2)]">
                  {shortcut.description}
                </p>
              ) : null}
            </div>
            <span className="flex shrink-0 flex-wrap justify-end gap-1">
              {shortcut.keys.map((key, index) => (
                <Kbd key={`${key}-${index}`}>
                  {key === "Mod" ? modifierLabel : key}
                </Kbd>
              ))}
            </span>
          </div>
        ))}
      </div>
    </section>
  );
}
