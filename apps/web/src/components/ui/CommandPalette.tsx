"use client";

import {
  ArrowRight,
  BarChart3,
  FileText,
  Home,
  Image as ImageIcon,
  Search,
  Shield,
  User,
  Wrench,
  X,
} from "lucide-react";
import { usePathname, useRouter } from "next/navigation";
import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type ComponentType,
  type KeyboardEvent as ReactKeyboardEvent,
  type SVGProps,
} from "react";

import { Kbd } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

type CommandIcon = ComponentType<SVGProps<SVGSVGElement>>;

interface Command {
  id: string;
  label: string;
  detail: string;
  href: string;
  group: "导航" | "设置" | "管理";
  keywords: string[];
  icon: CommandIcon;
  searchText: string;
}

function normalizeSearchValue(value: string) {
  return value.toLocaleLowerCase("zh-CN").replace(/\s+/g, " ").trim();
}

function command(definition: Omit<Command, "searchText">): Command {
  return {
    ...definition,
    searchText: normalizeSearchValue(
      [
        definition.label,
        definition.detail,
        definition.href,
        definition.group,
        ...definition.keywords,
      ].join(" "),
    ),
  };
}

const COMMANDS: Command[] = [
  command({
    id: "studio",
    label: "新建 / Studio",
    detail: "打开创作工作台",
    href: "/",
    group: "导航",
    keywords: ["new", "studio", "home", "创作", "首页", "工作台"],
    icon: Home,
  }),
  command({
    id: "stream",
    label: "Stream",
    detail: "浏览灵感流",
    href: "/stream",
    group: "导航",
    keywords: ["stream", "feed", "灵感流", "图片流"],
    icon: ImageIcon,
  }),
  command({
    id: "me",
    label: "个人中心",
    detail: "查看账号与历史",
    href: "/me",
    group: "导航",
    keywords: ["me", "profile", "account", "我的", "账号"],
    icon: User,
  }),
  command({
    id: "settings-usage",
    label: "用量设置",
    detail: "查看使用量与配额",
    href: "/settings/usage",
    group: "设置",
    keywords: ["settings", "usage", "quota", "用量", "配额"],
    icon: BarChart3,
  }),
  command({
    id: "settings-prompts",
    label: "提示词设置",
    detail: "管理系统提示词",
    href: "/settings/prompts",
    group: "设置",
    keywords: ["settings", "prompts", "system prompt", "提示词", "系统提示词"],
    icon: FileText,
  }),
  command({
    id: "settings-privacy",
    label: "隐私设置",
    detail: "导出或删除账号数据",
    href: "/settings/privacy",
    group: "设置",
    keywords: ["settings", "privacy", "data", "隐私", "数据", "删除账号"],
    icon: Shield,
  }),
  command({
    id: "admin",
    label: "管理页",
    detail: "打开后台管理",
    href: "/admin",
    group: "管理",
    keywords: ["admin", "manage", "后台", "管理", "邀请", "备份"],
    icon: Wrench,
  }),
];

export function CommandPalette() {
  const router = useRouter();
  const pathname = usePathname();
  const headingId = useId();
  const descriptionId = useId();
  const listboxId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [modifierLabel, setModifierLabel] = useState("Ctrl");

  const filteredCommands = useMemo(() => {
    const tokens = normalizeSearchValue(query).split(" ").filter(Boolean);
    if (tokens.length === 0) return COMMANDS;
    return COMMANDS.filter((item) =>
      tokens.every((token) => item.searchText.includes(token)),
    );
  }, [query]);

  const effectiveSelectedIndex =
    filteredCommands.length === 0
      ? 0
      : Math.min(selectedIndex, filteredCommands.length - 1);
  const selectedCommand = filteredCommands[effectiveSelectedIndex];
  const optionId = useCallback(
    (id: string) => `${listboxId}-${id}`,
    [listboxId],
  );

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => {
      setModifierLabel(
        /Mac|iPhone|iPad|iPod/i.test(navigator.platform || "") ? "⌘" : "Ctrl",
      );
    });
    return () => window.cancelAnimationFrame(frame);
  }, []);

  const openPalette = useCallback(() => {
    previousFocusRef.current =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setOpen(true);
  }, []);

  const closePalette = useCallback((restoreFocus = true) => {
    setOpen(false);
    setQuery("");
    setSelectedIndex(0);
    if (restoreFocus) {
      window.setTimeout(() => {
        previousFocusRef.current?.focus({ preventScroll: true });
      }, 0);
    }
  }, []);

  const runCommand = useCallback(
    (item: Command) => {
      closePalette(false);
      router.push(item.href);
    },
    [closePalette, router],
  );

  useEffect(() => {
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      const isCommandK =
        event.key.toLocaleLowerCase() === "k" && (event.metaKey || event.ctrlKey);

      if (!isCommandK) return;
      event.preventDefault();
      if (open) {
        closePalette();
      } else {
        openPalette();
      }
    };

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [closePalette, open, openPalette]);

  useEffect(() => {
    if (!open) return;
    // P3-1：改用 setTimeout 0 让 focus 在 layout/paint 完成后再触发，
    // 避免在 rAF 内 focus 引起的强制同步重排
    const timer = window.setTimeout(() => {
      inputRef.current?.focus({ preventScroll: true });
      inputRef.current?.select();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [open]);

  const handleQueryChange = (value: string) => {
    setQuery(value);
    setSelectedIndex(0);
  };

  const handleKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      closePalette();
      return;
    }

    if (event.nativeEvent.isComposing) return;

    if (event.key === "ArrowDown") {
      event.preventDefault();
      setSelectedIndex((current) =>
        filteredCommands.length === 0 ? 0 : (current + 1) % filteredCommands.length,
      );
      return;
    }

    if (event.key === "ArrowUp") {
      event.preventDefault();
      setSelectedIndex((current) =>
        filteredCommands.length === 0
          ? 0
          : (current - 1 + filteredCommands.length) % filteredCommands.length,
      );
      return;
    }

    if (event.key === "Enter" && event.target instanceof HTMLButtonElement) {
      return;
    }

    if (event.key === "Enter" && selectedCommand) {
      event.preventDefault();
      runCommand(selectedCommand);
    }
  };

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-[95] flex items-start justify-center px-3 pt-[12vh] sm:pt-[16vh]">
      <button
        type="button"
        aria-label="关闭命令面板"
        className="absolute inset-0 cursor-default bg-black/45 backdrop-blur-sm"
        onClick={() => closePalette()}
        tabIndex={-1}
      />

      <section
        role="dialog"
        aria-modal="true"
        aria-labelledby={headingId}
        aria-describedby={descriptionId}
        className={cn(
          "relative w-full max-w-xl overflow-hidden rounded-lg",
          "border border-[var(--border-strong)] bg-[var(--bg-1)]/95",
          "shadow-[var(--shadow-3)] backdrop-blur-xl",
        )}
        onKeyDown={handleKeyDown}
      >
        <h2 id={headingId} className="sr-only">
          命令面板
        </h2>
        <p id={descriptionId} className="sr-only">
          输入关键词过滤命令，使用上下方向键选择，按 Enter 打开，按 Escape 关闭。
        </p>

        <div className="flex min-h-14 items-center gap-3 border-b border-[var(--border)] px-4">
          <Search className="h-5 w-5 shrink-0 text-[var(--fg-2)]" aria-hidden />
          <input
            ref={inputRef}
            role="combobox"
            aria-expanded="true"
            aria-controls={listboxId}
            aria-activedescendant={
              selectedCommand ? optionId(selectedCommand.id) : undefined
            }
            aria-autocomplete="list"
            value={query}
            onChange={(event) => handleQueryChange(event.target.value)}
            placeholder="搜索命令或页面"
            autoComplete="off"
            spellCheck={false}
            className={cn(
              "h-14 min-w-0 flex-1 bg-transparent text-[15px] text-[var(--fg-0)]",
              "placeholder:text-[var(--fg-2)] focus:outline-none",
            )}
          />
          <div className="hidden items-center gap-1 sm:flex" aria-hidden>
            <Kbd>{modifierLabel}</Kbd>
            <Kbd>K</Kbd>
          </div>
          <button
            type="button"
            aria-label="关闭命令面板"
            className={cn(
              "inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md",
              "text-[var(--fg-2)] transition-colors hover:bg-white/8 hover:text-[var(--fg-0)]",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
            )}
            onClick={() => closePalette()}
          >
            <X className="h-4 w-4" aria-hidden />
          </button>
        </div>

        <div
          id={listboxId}
          role="listbox"
          aria-label="命令"
          className="max-h-[min(420px,58vh)] overflow-y-auto p-2"
        >
          {filteredCommands.length > 0 ? (
            filteredCommands.map((item, index) => {
              const Icon = item.icon;
              const selected = index === effectiveSelectedIndex;
              const current =
                item.href === "/"
                  ? pathname === "/"
                  : pathname === item.href || pathname.startsWith(`${item.href}/`);

              return (
                <button
                  key={item.id}
                  id={optionId(item.id)}
                  type="button"
                  role="option"
                  aria-selected={selected}
                  tabIndex={-1}
                  onMouseEnter={() => setSelectedIndex(index)}
                  onFocus={() => setSelectedIndex(index)}
                  onClick={() => runCommand(item)}
                  className={cn(
                    "flex min-h-[58px] w-full items-center gap-3 rounded-md px-3 py-2.5 text-left",
                    "transition-colors focus-visible:outline-none",
                    selected
                      ? "bg-white/10 text-[var(--fg-0)]"
                      : "text-[var(--fg-1)] hover:bg-white/6 hover:text-[var(--fg-0)]",
                  )}
                >
                  <span
                    className={cn(
                      "inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-md",
                      selected ? "bg-[var(--accent-soft)]" : "bg-white/6",
                    )}
                    aria-hidden
                  >
                    <Icon
                      className={cn(
                        "h-[18px] w-[18px]",
                        selected ? "text-[var(--amber-300)]" : "text-[var(--fg-2)]",
                      )}
                    />
                  </span>

                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm font-medium">
                      {item.label}
                    </span>
                    <span className="mt-0.5 block truncate text-xs text-[var(--fg-2)]">
                      {item.detail}
                    </span>
                  </span>

                  <span className="flex shrink-0 items-center gap-2">
                    {current && (
                      <span className="rounded-[4px] border border-[var(--border)] px-1.5 py-0.5 text-[11px] text-[var(--fg-2)]">
                        当前
                      </span>
                    )}
                    <span className="hidden text-[11px] text-[var(--fg-2)] sm:inline">
                      {item.group}
                    </span>
                    <ArrowRight
                      className={cn(
                        "h-4 w-4",
                        selected ? "text-[var(--fg-1)]" : "text-[var(--fg-3)]",
                      )}
                      aria-hidden
                    />
                  </span>
                </button>
              );
            })
          ) : (
            <div className="px-4 py-10 text-center text-sm text-[var(--fg-2)]">
              没有匹配命令
            </div>
          )}
        </div>
      </section>
    </div>
  );
}

export default CommandPalette;
