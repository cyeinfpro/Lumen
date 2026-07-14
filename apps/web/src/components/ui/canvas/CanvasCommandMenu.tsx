"use client";

import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import {
  Box,
  Command,
  CornerDownLeft,
  Search,
  X,
  type LucideIcon,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";

import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import { DURATION, EASE } from "@/lib/motion";
import { cn } from "@/lib/utils";
import { IconButton, Kbd } from "@/components/ui/primitives";
import { useModalLayer } from "@/components/ui/primitives/mobile/useModalLayer";

export type CanvasCommandMenuItemKind = "node" | "command";

export interface CanvasCommandMenuItem {
  id: string;
  kind: CanvasCommandMenuItemKind;
  label: string;
  description?: string;
  keywords?: readonly string[];
  icon: LucideIcon;
  shortcut?: readonly string[];
  disabled?: boolean;
}

export interface CanvasCommandMenuProps {
  open: boolean;
  items: readonly CanvasCommandMenuItem[];
  onOpenChange: (open: boolean) => void;
  onSelect: (item: CanvasCommandMenuItem) => void;
  title?: string;
  placeholder?: string;
  emptyLabel?: string;
  className?: string;
}

interface IndexedCommandItem {
  item: CanvasCommandMenuItem;
  index: number;
}

const GROUPS: ReadonlyArray<{
  kind: CanvasCommandMenuItemKind;
  label: string;
  icon: LucideIcon;
}> = [
  { kind: "node", label: "节点", icon: Box },
  { kind: "command", label: "命令", icon: Command },
];

function normalizeSearchValue(value: string) {
  return value.toLocaleLowerCase("zh-CN").replace(/\s+/g, " ").trim();
}

function searchableText(item: CanvasCommandMenuItem) {
  return normalizeSearchValue(
    [
      item.label,
      item.description ?? "",
      item.kind === "node" ? "节点 node" : "命令 command",
      ...(item.keywords ?? []),
    ].join(" "),
  );
}

function filterCommandItems(
  items: readonly CanvasCommandMenuItem[],
  query: string,
) {
  const tokens = normalizeSearchValue(query).split(" ").filter(Boolean);
  if (tokens.length === 0) return [...items];
  return items.filter((item) => {
    const text = searchableText(item);
    return tokens.every((token) => text.includes(token));
  });
}

function firstEnabledIndex(items: readonly CanvasCommandMenuItem[]) {
  return items.findIndex((item) => !item.disabled);
}

function lastEnabledIndex(items: readonly CanvasCommandMenuItem[]) {
  for (let index = items.length - 1; index >= 0; index -= 1) {
    if (!items[index]?.disabled) return index;
  }
  return -1;
}

function nextEnabledIndex(
  items: readonly CanvasCommandMenuItem[],
  current: number,
  direction: 1 | -1,
) {
  if (items.length === 0) return -1;
  for (let offset = 1; offset <= items.length; offset += 1) {
    const index = (current + direction * offset + items.length) % items.length;
    if (!items[index]?.disabled) return index;
  }
  return -1;
}

function resolvedSelectedIndex(
  items: readonly CanvasCommandMenuItem[],
  selectedIndex: number,
) {
  if (selectedIndex >= 0 && !items[selectedIndex]?.disabled) {
    return selectedIndex;
  }
  return firstEnabledIndex(items);
}

export function CanvasCommandMenu({
  open,
  items,
  onOpenChange,
  onSelect,
  title = "画布命令",
  placeholder = "搜索节点或命令",
  emptyLabel = "无结果",
  className,
}: CanvasCommandMenuProps) {
  const reduceMotion = useReducedMotion();
  const headingId = useId();
  const descriptionId = useId();
  const listboxId = useId();
  const dialogRef = useRef<HTMLElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const optionRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const [query, setQuery] = useState("");
  const [selectedIndex, setSelectedIndex] = useState(0);
  useBodyScrollLock(open);

  const filteredItems = useMemo(
    () => filterCommandItems(items, query),
    [items, query],
  );
  const effectiveSelectedIndex = resolvedSelectedIndex(
    filteredItems,
    selectedIndex,
  );
  const selectedItem = filteredItems[effectiveSelectedIndex];
  const indexedItems = useMemo(
    () => filteredItems.map((item, index) => ({ item, index })),
    [filteredItems],
  );

  const closeMenu = useCallback(() => {
    setQuery("");
    setSelectedIndex(0);
    onOpenChange(false);
  }, [onOpenChange]);
  const onDialogKeyDown = useModalLayer({
    open,
    rootRef: dialogRef,
    onClose: closeMenu,
    initialFocusRef: inputRef,
  });

  const selectItem = useCallback(
    (item: CanvasCommandMenuItem) => {
      if (item.disabled) return;
      closeMenu();
      onSelect(item);
    },
    [closeMenu, onSelect],
  );
  const setOptionRef = useCallback(
    (index: number, node: HTMLButtonElement | null) => {
      optionRefs.current[index] = node;
    },
    [],
  );

  useEffect(() => {
    if (!open || effectiveSelectedIndex < 0) return;
    optionRefs.current[effectiveSelectedIndex]?.scrollIntoView({
      block: "nearest",
    });
  }, [effectiveSelectedIndex, open]);

  const handleKeyDown = (event: ReactKeyboardEvent<HTMLElement>) => {
    onDialogKeyDown(event);
    if (event.nativeEvent.isComposing) return;
    if (event.key === "Enter" && event.target instanceof HTMLButtonElement) {
      return;
    }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const direction = event.key === "ArrowDown" ? 1 : -1;
      setSelectedIndex(
        nextEnabledIndex(filteredItems, effectiveSelectedIndex, direction),
      );
      return;
    }
    if (event.key === "Home" || event.key === "End") {
      event.preventDefault();
      setSelectedIndex(
        event.key === "Home"
          ? firstEnabledIndex(filteredItems)
          : lastEnabledIndex(filteredItems),
      );
      return;
    }
    if (event.key === "Enter" && selectedItem) {
      event.preventDefault();
      selectItem(selectedItem);
    }
  };

  const optionId = (index: number) => `${listboxId}-option-${index}`;

  return (
    <AnimatePresence>
      {open ? (
        <motion.div
          key="canvas-command-menu"
          className="mobile-dialog-shell fixed inset-0 z-[var(--z-dialog)] flex items-end justify-center p-0 sm:items-start sm:px-4 sm:pt-[14vh]"
          initial={reduceMotion ? false : { opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: reduceMotion ? 0 : DURATION.quick }}
        >
          <button
            type="button"
            aria-label="关闭画布命令"
            tabIndex={-1}
            className="absolute inset-0 cursor-default bg-[var(--surface-scrim)]"
            onClick={closeMenu}
          />
          <motion.section
            ref={dialogRef}
            role="dialog"
            aria-modal="true"
            aria-labelledby={headingId}
            aria-describedby={descriptionId}
            onKeyDown={handleKeyDown}
            initial={
              reduceMotion ? false : { opacity: 0, scale: 0.98, y: 10 }
            }
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={
              reduceMotion
                ? { opacity: 0 }
                : { opacity: 0, scale: 0.98, y: 10 }
            }
            transition={{
              duration: reduceMotion ? 0 : DURATION.normal,
              ease: EASE.develop,
            }}
            className={cn(
              "mobile-dialog-panel surface-dialog relative flex max-h-[82dvh] w-full max-w-2xl flex-col overflow-hidden",
              "max-sm:rounded-t-[var(--radius-sheet)] max-sm:rounded-b-none max-sm:border-b-0",
              className,
            )}
          >
            <h2 id={headingId} className="sr-only">
              {title}
            </h2>
            <p id={descriptionId} className="sr-only">
              搜索节点或命令，使用上下方向键选择，按 Enter 执行。
            </p>

            <div className="flex min-h-14 shrink-0 items-center gap-3 border-b border-[var(--border)] px-4">
              <Search
                className="h-5 w-5 shrink-0 text-[var(--fg-2)]"
                aria-hidden
              />
              <input
                ref={inputRef}
                role="combobox"
                aria-expanded="true"
                aria-controls={listboxId}
                aria-activedescendant={
                  selectedItem ? optionId(effectiveSelectedIndex) : undefined
                }
                aria-autocomplete="list"
                value={query}
                onChange={(event) => {
                  setQuery(event.currentTarget.value);
                  setSelectedIndex(0);
                }}
                placeholder={placeholder}
                autoComplete="off"
                spellCheck={false}
                className="h-14 min-w-0 flex-1 bg-transparent type-body text-[var(--fg-0)] outline-none placeholder:text-[var(--fg-2)]"
              />
              <IconButton
                aria-label="关闭画布命令"
                size="lg"
                onClick={closeMenu}
              >
                <X className="h-4 w-4" aria-hidden />
              </IconButton>
            </div>

            <div
              id={listboxId}
              role="listbox"
              aria-label="画布节点与命令"
              className="mobile-dialog-scroll min-h-0 flex-1 overflow-y-auto p-2"
            >
              {indexedItems.length > 0 ? (
                GROUPS.map((group) => {
                  const groupItems = indexedItems.filter(
                    ({ item }) => item.kind === group.kind,
                  );
                  return groupItems.length > 0 ? (
                    <CommandGroup
                      key={group.kind}
                      group={group}
                      items={groupItems}
                      selectedIndex={effectiveSelectedIndex}
                      optionId={optionId}
                      setOptionRef={setOptionRef}
                      onHighlight={setSelectedIndex}
                      onSelect={selectItem}
                    />
                  ) : null;
                })
              ) : (
                <div
                  role="status"
                  className="grid min-h-40 place-items-center px-4 text-center type-body-sm text-[var(--fg-2)]"
                >
                  {emptyLabel}
                </div>
              )}
            </div>

            <div
              aria-label="命令菜单快捷键"
              className="flex shrink-0 flex-wrap items-center gap-x-4 gap-y-2 border-t border-[var(--border)] px-4 py-3 type-caption text-[var(--fg-2)]"
            >
              <span className="inline-flex items-center gap-1.5">
                <Kbd>↑</Kbd>
                <Kbd>↓</Kbd>
                选择
              </span>
              <span className="inline-flex items-center gap-1.5">
                <Kbd>Enter</Kbd>
                执行
              </span>
              <span className="inline-flex items-center gap-1.5">
                <Kbd>Esc</Kbd>
                关闭
              </span>
            </div>
          </motion.section>
        </motion.div>
      ) : null}
    </AnimatePresence>
  );
}

function CommandGroup({
  group,
  items,
  selectedIndex,
  optionId,
  setOptionRef,
  onHighlight,
  onSelect,
}: {
  group: (typeof GROUPS)[number];
  items: readonly IndexedCommandItem[];
  selectedIndex: number;
  optionId: (index: number) => string;
  setOptionRef: (index: number, node: HTMLButtonElement | null) => void;
  onHighlight: (index: number) => void;
  onSelect: (item: CanvasCommandMenuItem) => void;
}) {
  const GroupIcon = group.icon;
  const groupId = useId();
  return (
    <section role="group" aria-labelledby={groupId} className="py-1">
      <div
        id={groupId}
        className="flex items-center gap-2 px-3 pb-1 pt-2 type-overline text-[var(--fg-2)]"
      >
        <GroupIcon className="h-3.5 w-3.5" aria-hidden />
        {group.label}
      </div>
      <div className="grid gap-0.5">
        {items.map(({ item, index }) => (
          <CommandOption
            key={item.id}
            item={item}
            index={index}
            selected={selectedIndex === index}
            optionId={optionId(index)}
            setOptionRef={setOptionRef}
            onHighlight={onHighlight}
            onSelect={onSelect}
          />
        ))}
      </div>
    </section>
  );
}

function CommandOption({
  item,
  index,
  selected,
  optionId,
  setOptionRef,
  onHighlight,
  onSelect,
}: {
  item: CanvasCommandMenuItem;
  index: number;
  selected: boolean;
  optionId: string;
  setOptionRef: (index: number, node: HTMLButtonElement | null) => void;
  onHighlight: (index: number) => void;
  onSelect: (item: CanvasCommandMenuItem) => void;
}) {
  const ItemIcon = item.icon;
  return (
    <button
      ref={(node) => setOptionRef(index, node)}
      id={optionId}
      type="button"
      role="option"
      aria-selected={selected}
      aria-disabled={item.disabled || undefined}
      disabled={item.disabled}
      tabIndex={-1}
      onMouseEnter={() => onHighlight(index)}
      onFocus={() => onHighlight(index)}
      onClick={() => onSelect(item)}
      className={cn(
        "flex min-h-[58px] w-full items-center gap-3 rounded-[var(--radius-control)] px-3 py-2 text-left",
        "transition-colors duration-[var(--dur-fast)] ease-[var(--ease-develop)] motion-reduce:transition-none",
        "disabled:pointer-events-none disabled:opacity-45",
        selected
          ? "bg-[var(--bg-2)] text-[var(--fg-0)]"
          : "text-[var(--fg-1)] hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)]",
      )}
    >
      <span
        className={cn(
          "grid h-9 w-9 shrink-0 place-items-center rounded-[var(--radius-control)]",
          selected ? "bg-[var(--accent-soft)]" : "bg-[var(--bg-2)]",
        )}
        aria-hidden
      >
        <ItemIcon
          className={cn(
            "h-[18px] w-[18px]",
            selected ? "text-[var(--accent)]" : "text-[var(--fg-2)]",
          )}
        />
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate type-body-sm font-medium text-[var(--fg-0)]">
          {item.label}
        </span>
        {item.description ? (
          <span className="mt-0.5 block truncate type-caption text-[var(--fg-2)]">
            {item.description}
          </span>
        ) : null}
      </span>
      {item.shortcut?.length ? (
        <span className="hidden shrink-0 items-center gap-1 sm:flex" aria-hidden>
          {item.shortcut.map((key) => (
            <Kbd key={key}>{key}</Kbd>
          ))}
        </span>
      ) : (
        <CornerDownLeft
          className={cn(
            "h-4 w-4 shrink-0",
            selected ? "text-[var(--fg-1)]" : "text-[var(--fg-3)]",
          )}
          aria-hidden
        />
      )}
    </button>
  );
}
