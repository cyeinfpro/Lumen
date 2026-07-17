"use client";

import {
  AlignCenter,
  AlignCenterVertical,
  AlignEndVertical,
  AlignLeft,
  AlignRight,
  AlignStartVertical,
  ChevronDown,
  Columns3,
  Copy,
  Rows3,
  Scan,
  Trash2,
  Workflow,
  type LucideIcon,
} from "lucide-react";
import {
  useEffect,
  useId,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";

import { Button, IconButton } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

export type CanvasSelectionAlignment =
  | "left"
  | "horizontal-center"
  | "right"
  | "top"
  | "vertical-center"
  | "bottom";

export type CanvasSelectionDistribution = "horizontal" | "vertical";

export interface CanvasSelectionToolbarProps {
  selectedCount: number;
  onCopy: () => void;
  onAlign: (alignment: CanvasSelectionAlignment) => void;
  onDistribute: (distribution: CanvasSelectionDistribution) => void;
  onAutoLayout: () => void;
  onFitSelection: () => void;
  onDelete: () => void;
  canCopy?: boolean;
  canAlign?: boolean;
  canDistribute?: boolean;
  canAutoLayout?: boolean;
  disabled?: boolean;
  className?: string;
}

interface ToolbarMenuItem {
  id: string;
  label: string;
  icon: LucideIcon;
  onSelect: () => void;
}

const ALIGNMENT_ITEMS: ReadonlyArray<{
  id: CanvasSelectionAlignment;
  label: string;
  icon: LucideIcon;
}> = [
  { id: "left", label: "左对齐", icon: AlignLeft },
  { id: "horizontal-center", label: "水平居中", icon: AlignCenter },
  { id: "right", label: "右对齐", icon: AlignRight },
  { id: "top", label: "顶部对齐", icon: AlignStartVertical },
  { id: "vertical-center", label: "垂直居中", icon: AlignCenterVertical },
  { id: "bottom", label: "底部对齐", icon: AlignEndVertical },
];

const DISTRIBUTION_ITEMS: ReadonlyArray<{
  id: CanvasSelectionDistribution;
  label: string;
  icon: LucideIcon;
}> = [
  { id: "horizontal", label: "水平分布", icon: Columns3 },
  { id: "vertical", label: "垂直分布", icon: Rows3 },
];

export function CanvasSelectionToolbar({
  selectedCount,
  onCopy,
  onAlign,
  onDistribute,
  onAutoLayout,
  onFitSelection,
  onDelete,
  canCopy = true,
  canAlign = selectedCount > 1,
  canDistribute = selectedCount > 2,
  canAutoLayout = selectedCount > 1,
  disabled = false,
  className,
}: CanvasSelectionToolbarProps) {
  if (selectedCount < 1) return null;

  const alignmentItems = ALIGNMENT_ITEMS.map((item) => ({
    ...item,
    onSelect: () => onAlign(item.id),
  }));
  const distributionItems = DISTRIBUTION_ITEMS.map((item) => ({
    ...item,
    onSelect: () => onDistribute(item.id),
  }));

  return (
    <div
      role="toolbar"
      aria-label={`${selectedCount} 个选中节点`}
      className={cn(
        "surface-panel inline-flex min-h-11 max-w-full items-center gap-0.5 overflow-visible p-1",
        className,
      )}
    >
      <span
        aria-live="polite"
        className="inline-flex h-8 min-w-12 shrink-0 items-center justify-center border-r border-[var(--border)] px-2 type-caption font-medium tabular-nums text-[var(--fg-1)]"
      >
        {selectedCount} 个
      </span>

      <IconButton
        aria-label="复制选中节点"
        tooltip="复制"
        size="sm"
        disabled={disabled || !canCopy}
        onClick={onCopy}
      >
        <Copy className="h-4 w-4" aria-hidden />
      </IconButton>

      {canAlign ? (
        <ToolbarMenu
          label="对齐"
          icon={AlignCenter}
          items={alignmentItems}
          disabled={disabled}
        />
      ) : null}
      {canDistribute ? (
        <ToolbarMenu
          label="分布"
          icon={Columns3}
          items={distributionItems}
          disabled={disabled}
          align="end"
        />
      ) : null}

      <span
        role="separator"
        aria-orientation="vertical"
        className="mx-1 h-5 w-px shrink-0 bg-[var(--border)]"
      />

      <IconButton
        aria-label="自动布局选中节点"
        tooltip="自动布局"
        size="sm"
        disabled={disabled || !canAutoLayout}
        onClick={onAutoLayout}
      >
        <Workflow className="h-4 w-4" aria-hidden />
      </IconButton>
      <IconButton
        aria-label="适应选区"
        tooltip="适应选区"
        size="sm"
        disabled={disabled}
        onClick={onFitSelection}
      >
        <Scan className="h-4 w-4" aria-hidden />
      </IconButton>

      <span
        role="separator"
        aria-orientation="vertical"
        className="mx-1 h-5 w-px shrink-0 bg-[var(--border)]"
      />

      <IconButton
        aria-label={`删除 ${selectedCount} 个选中节点`}
        tooltip="删除"
        variant="danger"
        size="sm"
        disabled={disabled}
        onClick={onDelete}
      >
        <Trash2 className="h-4 w-4" aria-hidden />
      </IconButton>
    </div>
  );
}

function ToolbarMenu({
  label,
  icon: TriggerIcon,
  items,
  disabled,
  align = "start",
}: {
  label: string;
  icon: LucideIcon;
  items: readonly ToolbarMenuItem[];
  disabled?: boolean;
  align?: "start" | "end";
}) {
  const menuId = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const focusLastRef = useRef(false);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!open) return;
    const timer = window.setTimeout(() => {
      const buttons = menuButtons(menuRef.current);
      const target = focusLastRef.current ? buttons.at(-1) : buttons[0];
      focusLastRef.current = false;
      target?.focus({ preventScroll: true });
    }, 0);
    const handlePointerDown = (event: PointerEvent) => {
      if (
        event.target instanceof Node &&
        !rootRef.current?.contains(event.target)
      ) {
        setOpen(false);
      }
    };
    const handleFocusIn = (event: FocusEvent) => {
      if (
        event.target instanceof Node &&
        !rootRef.current?.contains(event.target)
      ) {
        setOpen(false);
      }
    };
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.isComposing) return;
      event.preventDefault();
      setOpen(false);
      triggerRef.current?.focus({ preventScroll: true });
    };
    document.addEventListener("pointerdown", handlePointerDown, true);
    document.addEventListener("focusin", handleFocusIn, true);
    document.addEventListener("keydown", handleEscape, true);
    return () => {
      window.clearTimeout(timer);
      document.removeEventListener("pointerdown", handlePointerDown, true);
      document.removeEventListener("focusin", handleFocusIn, true);
      document.removeEventListener("keydown", handleEscape, true);
    };
  }, [open]);

  const openMenu = (focusLast = false) => {
    focusLastRef.current = focusLast;
    setOpen(true);
  };

  return (
    <div ref={rootRef} className="relative shrink-0">
      <Button
        ref={triggerRef}
        variant="ghost"
        size="sm"
        leftIcon={<TriggerIcon className="h-4 w-4" aria-hidden />}
        rightIcon={
          <ChevronDown
            className={cn(
              "h-3.5 w-3.5 transition-transform duration-[var(--dur-fast)] ease-[var(--ease-develop)] motion-reduce:transition-none",
              open && "rotate-180",
            )}
            aria-hidden
          />
        }
        aria-haspopup="menu"
        aria-controls={open ? menuId : undefined}
        aria-expanded={open}
        disabled={disabled}
        onClick={() => setOpen((current) => !current)}
        onKeyDown={(event) => {
          if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            event.preventDefault();
            openMenu(event.key === "ArrowUp");
          }
        }}
        className="shrink-0 px-2.5 motion-reduce:transition-none"
      >
        {label}
      </Button>

      {open ? (
        <div
          ref={menuRef}
          id={menuId}
          role="menu"
          aria-label={label}
          onKeyDown={(event) => handleMenuKeyDown(event, menuRef.current)}
          className={cn(
            "surface-panel animate-fade-in absolute top-[calc(100%+6px)] z-[var(--z-popover)] min-w-40 p-1",
            align === "start" ? "left-0" : "right-0",
          )}
        >
          {items.map((item) => {
            const ItemIcon = item.icon;
            return (
              <button
                key={item.id}
                type="button"
                role="menuitem"
                onClick={() => {
                  setOpen(false);
                  item.onSelect();
                  window.setTimeout(() => {
                    triggerRef.current?.focus({ preventScroll: true });
                  }, 0);
                }}
                className="flex min-h-9 w-full items-center gap-2.5 rounded-[var(--radius-control)] px-2.5 type-body-sm text-[var(--fg-1)] transition-colors hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)] focus-visible:bg-[var(--bg-2)] motion-reduce:transition-none max-sm:min-h-11"
              >
                <ItemIcon className="h-4 w-4 shrink-0" aria-hidden />
                {item.label}
              </button>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}

function menuButtons(menu: HTMLDivElement | null) {
  return menu
    ? Array.from(menu.querySelectorAll<HTMLButtonElement>('[role="menuitem"]'))
    : [];
}

function handleMenuKeyDown(
  event: ReactKeyboardEvent<HTMLDivElement>,
  menu: HTMLDivElement | null,
) {
  if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
  const buttons = menuButtons(menu);
  if (buttons.length === 0) return;
  event.preventDefault();
  const current = buttons.indexOf(document.activeElement as HTMLButtonElement);
  if (event.key === "Home") {
    buttons[0]?.focus({ preventScroll: true });
    return;
  }
  if (event.key === "End") {
    buttons.at(-1)?.focus({ preventScroll: true });
    return;
  }
  const direction = event.key === "ArrowDown" ? 1 : -1;
  const next = (current + direction + buttons.length) % buttons.length;
  buttons[next]?.focus({ preventScroll: true });
}
