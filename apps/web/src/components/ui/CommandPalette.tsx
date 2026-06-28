"use client";

import {
  ArrowRight,
  BarChart3,
  Brain,
  Clapperboard,
  FileText,
  FolderKanban,
  Home,
  Images,
  PanelLeft,
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
  useSyncExternalStore,
  type ComponentType,
  type KeyboardEvent as ReactKeyboardEvent,
  type SVGProps,
} from "react";

import { IconButton, Kbd } from "@/components/ui/primitives";
import { BottomSheet } from "@/components/ui/primitives/mobile/BottomSheet";
import {
  getActiveNavKey,
  getAppNavItems,
  isSameRoute,
  type AppNavKey,
} from "@/components/ui/shell/navigation";
import { useUiStore } from "@/store/useUiStore";
import { cn } from "@/lib/utils";

type CommandIcon = ComponentType<SVGProps<SVGSVGElement>>;
type CommandAction = "toggle-sidebar";

interface Command {
  id: string;
  label: string;
  detail: string;
  href?: string;
  action?: CommandAction;
  navKey?: AppNavKey;
  group: "导航" | "操作" | "设置" | "管理";
  keywords: string[];
  icon: CommandIcon;
  searchText: string;
}

function normalizeSearchValue(value: string) {
  return value.toLocaleLowerCase("zh-CN").replace(/\s+/g, " ").trim();
}

const MODIFIER_SUBSCRIBE_NOOP = (): (() => void) => () => {};
const MODIFIER_SERVER_SNAPSHOT = (): "Ctrl" => "Ctrl";

function detectModifierLabel(): "⌘" | "Ctrl" {
  if (typeof navigator === "undefined") return "Ctrl";
  return /Mac|iPhone|iPad|iPod/i.test(navigator.platform || "") ? "⌘" : "Ctrl";
}

function command(definition: Omit<Command, "searchText">): Command {
  return {
    ...definition,
    searchText: normalizeSearchValue(
      [
        definition.label,
        definition.detail,
        definition.href ?? "",
        definition.action ?? "",
        definition.group,
        ...definition.keywords,
      ].join(" "),
    ),
  };
}

const NAV_ICONS: Record<AppNavKey, CommandIcon> = {
  studio: Home,
  video: Clapperboard,
  projects: FolderKanban,
  assets: Images,
  me: User,
};

const IS_DESKTOP_RUNTIME = process.env.NEXT_PUBLIC_LUMEN_RUNTIME === "desktop";

const SHARED_COMMANDS: Command[] = [
  command({
    id: "toggle-sidebar",
    label: "切换会话侧栏",
    detail: "打开或关闭创作页左侧会话列表",
    action: "toggle-sidebar",
    group: "操作",
    keywords: ["sidebar", "conversation", "侧栏", "会话", "列表", "快捷键", "cmd b", "ctrl b"],
    icon: PanelLeft,
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
    id: "settings-memory",
    label: "记忆设置",
    detail: "管理长期记忆与候选建议",
    href: "/settings/memory",
    group: "设置",
    keywords: ["settings", "memory", "记忆", "偏好", "长期记忆"],
    icon: Brain,
  }),
];

const DOCKER_ONLY_COMMANDS: Command[] = [
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

const STATIC_COMMANDS: Command[] = [
  ...SHARED_COMMANDS,
  ...(IS_DESKTOP_RUNTIME ? [] : DOCKER_ONLY_COMMANDS),
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
  const navVisibility = useUiStore((s) => s.navVisibility);
  // SSR/CSR 一致：getServerSnapshot=Ctrl，getClientSnapshot 探测 navigator。
  // 用 useSyncExternalStore 避免 hydration mismatch + 不踩 react-hooks/set-state-in-effect。
  const modifierLabel = useSyncExternalStore<string>(
    MODIFIER_SUBSCRIBE_NOOP,
    detectModifierLabel,
    MODIFIER_SERVER_SNAPSHOT,
  );
  // SSR safe：首屏视为桌面，挂载后由 matchMedia 修正。
  const [isDesktop, setIsDesktop] = useState(true);

  const navCommands = useMemo(
    () =>
      getAppNavItems(navVisibility).map((item) =>
        command({
          id: `nav-${item.key}`,
          label: item.label,
          detail: item.detail,
          href: item.route,
          navKey: item.key,
          group: "导航",
          keywords: item.keywords,
          icon: NAV_ICONS[item.key],
        }),
      ),
    [navVisibility],
  );
  const commands = useMemo(
    () => [...navCommands, ...STATIC_COMMANDS],
    [navCommands],
  );
  const filteredCommands = useMemo(() => {
    const tokens = normalizeSearchValue(query).split(" ").filter(Boolean);
    if (tokens.length === 0) return commands;
    return commands.filter((item) =>
      tokens.every((token) => item.searchText.includes(token)),
    );
  }, [commands, query]);

  const effectiveSelectedIndex =
    filteredCommands.length === 0
      ? 0
      : Math.min(selectedIndex, filteredCommands.length - 1);
  const selectedCommand = filteredCommands[effectiveSelectedIndex];
  const activeNavKey = getActiveNavKey(pathname, navVisibility);
  const optionId = useCallback(
    (id: string) => `${listboxId}-${id}`,
    [listboxId],
  );

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return;
    const mql = window.matchMedia("(min-width: 768px)");
    const update = () => setIsDesktop(mql.matches);
    update();
    mql.addEventListener("change", update);
    return () => mql.removeEventListener("change", update);
  }, []);

  const openPalette = useCallback(() => {
    previousFocusRef.current =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setOpen(true);
  }, []);

  const resetPalette = useCallback(() => {
    setOpen(false);
    setQuery("");
    setSelectedIndex(0);
  }, []);

  const closePalette = useCallback((restoreFocus = true) => {
    resetPalette();
    if (restoreFocus) {
      window.setTimeout(() => {
        previousFocusRef.current?.focus({ preventScroll: true });
      }, 0);
    }
  }, [resetPalette]);

  const runCommand = useCallback(
    (item: Command) => {
      resetPalette();
      if (item.action === "toggle-sidebar") {
        window.dispatchEvent(new CustomEvent("lumen:sidebar-toggle"));
        return;
      }
      if (item.href) {
        router.push(item.href);
      }
    },
    [resetPalette, router],
  );

  useEffect(() => {
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      const isCommandK =
        event.key.toLocaleLowerCase() === "k" && (event.metaKey || event.ctrlKey);

      if (!isCommandK) return;
      if (event.defaultPrevented) return;
      event.preventDefault();
      if (open) {
        closePalette();
      } else {
        openPalette();
      }
    };

    window.addEventListener("keydown", onKeyDown, { capture: true });
    return () => window.removeEventListener("keydown", onKeyDown, { capture: true });
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

  // 共用：搜索框 + 结果列表，两种容器（移动 BottomSheet / 桌面居中 modal）共享。
  const searchRow = (
    <div
      className={cn(
        "flex min-h-14 shrink-0 items-center gap-3 border-b border-[var(--border)] px-4",
        // 移动端粘顶：BottomSheet 内容长时不让搜索框滚走
        !isDesktop && "sticky top-0 z-10 bg-[var(--bg-1)]",
      )}
    >
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
      {isDesktop && (
        <div className="hidden items-center gap-1 sm:flex" aria-hidden>
          <Kbd>{modifierLabel}</Kbd>
          <Kbd>K</Kbd>
        </div>
      )}
      <IconButton
        variant="ghost"
        size="lg"
        aria-label="关闭命令面板"
        className="rounded-[var(--radius-control)]"
        onClick={() => closePalette()}
      >
        <X className="h-4 w-4" aria-hidden />
      </IconButton>
    </div>
  );

  const list = (
    <div
      id={listboxId}
      role="listbox"
      aria-label="命令"
      className={cn(
        "mobile-dialog-scroll min-h-0 p-2",
        isDesktop ? "!max-h-[55vh]" : "flex-1",
      )}
    >
      {filteredCommands.length > 0 ? (
        filteredCommands.map((item, index) => {
          const Icon = item.icon;
          const selected = index === effectiveSelectedIndex;
          const current = item.navKey
            ? activeNavKey === item.navKey
            : item.href
              ? isSameRoute(pathname, item.href)
              : false;

          return (
            /* @list-item-ok: combobox option, role + aria-selected + 多列布局 */
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
                "flex min-h-[58px] w-full items-center gap-3 rounded-[var(--radius-control)] px-3 py-2.5 text-left",
                "transition-colors focus-visible:outline-none",
                selected
                  ? "bg-[var(--bg-2)] text-[var(--fg-0)]"
                  : "text-[var(--fg-1)] hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)]",
              )}
            >
              <span
                className={cn(
                  "inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-[var(--radius-control)]",
                  selected ? "bg-[var(--accent-soft)]" : "bg-[var(--bg-2)]",
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
  );

  const shortcutHelp = (
    <div
      className="flex shrink-0 flex-wrap items-center gap-x-4 gap-y-2 border-t border-[var(--border)] px-4 py-3 text-[11px] text-[var(--fg-2)]"
      aria-label="快捷键"
    >
      <span className="inline-flex items-center gap-1.5">
        <Kbd>{modifierLabel}</Kbd>
        <Kbd>K</Kbd>
        命令
      </span>
      <span className="inline-flex items-center gap-1.5">
        <Kbd>{modifierLabel}</Kbd>
        <Kbd>B</Kbd>
        侧栏
      </span>
      <span className="inline-flex items-center gap-1.5">
        <Kbd>↑</Kbd>
        <Kbd>↓</Kbd>
        选择
      </span>
      <span className="inline-flex items-center gap-1.5">
        <Kbd>Enter</Kbd>
        打开
      </span>
      <span className="inline-flex items-center gap-1.5">
        <Kbd>Esc</Kbd>
        关闭
      </span>
    </div>
  );

  if (!isDesktop) {
    // BottomSheet 自带 role="dialog" + aria-label="命令面板"，桌面分支的 sr-only 标题/描述无需重复。
    return (
      <BottomSheet
        open={open}
        onClose={() => closePalette()}
        ariaLabel="命令面板"
        snapPoints={["75%"]}
      >
        <div
          className="flex h-full min-h-0 flex-1 flex-col overflow-hidden"
          onKeyDown={handleKeyDown}
        >
          {searchRow}
          {list}
        </div>
      </BottomSheet>
    );
  }

  return (
    <div className="fixed inset-0 z-[95] flex items-start justify-center px-3 pt-[12vh] sm:pt-[16vh]">
      {/* @backdrop-button: dialog backdrop button，需要 click 但不能用 Button primitive 样式 */}
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
          "relative flex w-full max-w-xl flex-col max-h-[70vh] overflow-hidden rounded-[var(--radius-card)]",
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
        {searchRow}
        {list}
        {shortcutHelp}
      </section>
    </div>
  );
}

export default CommandPalette;
