"use client";

// 桌面端顶部主导航：复用在 DesktopStudio / DesktopStream / DesktopMe。
// 四 Tab 横向导航 + 左侧 Logo + 可配置右侧 slot。
// 路由契约集中在 navigation.ts，桌面顶部与移动底栏共用同一套 IA。

import { motion } from "framer-motion";
import { useQuery } from "@tanstack/react-query";
import { Menu } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useCallback, useMemo, useRef, type KeyboardEvent, type ReactNode } from "react";

import { IconButton } from "@/components/ui/primitives";
import { getMe, getMyWallet, getPricing, type AuthUser } from "@/lib/apiClient";
import { isDesktopRuntime } from "@/lib/desktop/runtime";
import { formatRmb, formatRmbCompact } from "@/lib/money";
import { SPRING } from "@/lib/motion";
import { useUiStore } from "@/store/useUiStore";
import {
  getActiveNavKey,
  getAppNavItems,
  getFirstVisibleNavRoute,
  isSameRoute,
  type AppNavItem,
  type AppNavKey,
} from "./navigation";

export type DesktopNavTab = AppNavKey;

export interface DesktopTopNavProps {
  active: DesktopNavTab;
  right?: ReactNode;
  onToggleSidebar?: () => void;
}

export function DesktopTopNav({ active, right, onToggleSidebar }: DesktopTopNavProps) {
  const pathname = usePathname();
  const router = useRouter();
  const navVisibility = useUiStore((s) => s.navVisibility);
  const navItems = useMemo(
    () => getAppNavItems(navVisibility),
    [navVisibility],
  );
  const homeHref = useMemo(
    () => getFirstVisibleNavRoute(navVisibility),
    [navVisibility],
  );

  const currentActive: DesktopNavTab = useMemo(() => {
    return getActiveNavKey(pathname, navVisibility) ?? active;
  }, [pathname, active, navVisibility]);

  const onTap = useCallback(
    (tab: AppNavItem) => {
      if (isSameRoute(pathname, tab.route)) return;
      router.push(tab.route);
    },
    [pathname, router],
  );

  const tabsRef = useRef<HTMLUListElement | null>(null);
  const onTabsKeyDown = useCallback((e: KeyboardEvent<HTMLUListElement>) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    const root = tabsRef.current;
    if (!root) return;
    const buttons = Array.from(root.querySelectorAll<HTMLButtonElement>("button"));
    if (buttons.length === 0) return;
    const idx = buttons.indexOf(document.activeElement as HTMLButtonElement);
    const base = idx < 0 ? 0 : idx;
    const delta = e.key === "ArrowRight" ? 1 : -1;
    const next = (base + delta + buttons.length) % buttons.length;
    e.preventDefault();
    buttons[next]?.focus();
  }, []);

  return (
    <header
      className={[
        "sticky top-0 grid h-12 w-full items-center gap-2 px-3 md:px-5",
        "grid-cols-[auto_minmax(0,1fr)_auto]",
        "backdrop-blur-xl bg-[var(--bg-0)]/70 border-b border-[var(--border-subtle)]",
      ].join(" ")}
      style={{
        top: "var(--system-banner-height, 0px)",
        zIndex: "var(--z-header, 10)",
      }}
    >
      {/* Left: sidebar toggle + Logo */}
      <div className="flex items-center gap-3 md:gap-4 min-w-0">
        {onToggleSidebar && (
          <IconButton
            size="md"
            aria-label="切换侧栏"
            title="切换侧栏 (⌘/Ctrl+B)"
            tooltip="切换侧栏 (⌘/Ctrl+B)"
            onClick={onToggleSidebar}
            className="rounded-full"
          >
            <Menu className="w-4.5 h-4.5" />
          </IconButton>
        )}
        <Link
          href={homeHref}
          className="inline-flex h-10 shrink-0 items-center gap-2 rounded-full px-1 transition-colors hover:text-[var(--fg-0)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60"
          aria-label="Lumen 首页"
        >
          {/* Lumen 品牌徽标渐变：琥珀→orange，非状态色，token 化无意义 */}
          {/* eslint-disable-next-line no-restricted-syntax */}
          <div className="w-5 h-5 rounded-full bg-gradient-to-tr from-[var(--amber-400)] to-orange-200 shadow-[var(--shadow-amber)]" />
          <span className="hidden sm:inline text-[13px] font-medium tracking-tight text-[var(--fg-0)]">Lumen</span>
        </Link>
      </div>

      {/* Middle: Tabs — 占据中间 1fr，居中显示，可挤压 */}
      <nav aria-label="主导航" className="flex min-w-0 flex-1 justify-center overflow-hidden">
        <ul ref={tabsRef} onKeyDown={onTabsKeyDown} className="flex items-center gap-1">
          {navItems.map((tab) => {
            const isActive = tab.key === currentActive;
            return (
              <li key={tab.key} className="relative">
                {/* 顶部导航 Tab：内嵌 layoutId 动画下划线 + tabsRef 键盘导航需要原生 button */}
                <button
                  type="button"
                  onClick={() => onTap(tab)}
                  aria-current={isActive ? "page" : undefined}
                  className={[
                    "relative inline-flex h-10 cursor-pointer items-center rounded-[var(--radius-control)] px-3 text-[13px] font-medium leading-none transition-colors whitespace-nowrap",
                    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
                    isActive
                      ? "text-[var(--fg-0)]"
                      : "text-[var(--fg-2)] hover:text-[var(--fg-0)]",
                  ].join(" ")}
                >
                  {tab.label}
                </button>
                {isActive && (
                  <motion.span
                    layoutId="desktop-nav-underline"
                    aria-hidden
                    className="absolute inset-x-2 -bottom-1 h-0.5 rounded-full bg-[var(--amber-400)] shadow-[var(--shadow-amber)]"
                    transition={SPRING.snap}
                  />
                )}
              </li>
            );
          })}
        </ul>
      </nav>

      {/* Right: slot */}
      <div className="flex min-w-0 shrink-0 items-center justify-end gap-2 text-sm text-[var(--fg-2)]">
        <WalletBalancePill />
        {right}
      </div>
    </header>
  );
}

function WalletBalancePill() {
  const desktop = isDesktopRuntime();
  const meQuery = useQuery<AuthUser>({
    queryKey: ["me"],
    queryFn: getMe,
    retry: false,
    staleTime: 60_000,
  });
  const enabled = !desktop && meQuery.data?.account_mode === "wallet";
  const walletQuery = useQuery({
    queryKey: ["me", "wallet"],
    queryFn: getMyWallet,
    enabled,
    retry: false,
    staleTime: 30_000,
  });
  const pricingQuery = useQuery({
    queryKey: ["me", "pricing"],
    queryFn: getPricing,
    enabled,
    retry: false,
    staleTime: 60_000,
  });
  const wallet = walletQuery.data;
  if (pricingQuery.data?.billing_enabled === false) return null;
  if (!enabled || wallet?.balance == null) return null;
  const low =
    wallet.low_balance_threshold?.micro != null &&
    wallet.balance.micro < wallet.low_balance_threshold.micro;
  const negative = wallet.balance.micro < 0;
  const balanceText = formatRmb(wallet.balance.rmb);
  const compactBalanceText = formatRmbCompact(wallet.balance.rmb);
  return (
    <Link
      href="/me/wallet"
      aria-label={low ? `钱包余额低 ¥${balanceText}` : `钱包余额 ¥${balanceText}`}
      title={`¥${balanceText}`}
      className={[
        "hidden h-10 max-w-[140px] shrink-0 items-center rounded-full border px-3 text-[12px] font-medium tabular-nums sm:inline-flex",
        "truncate font-mono",
        low || negative
          ? "border-danger-border bg-danger-soft text-[var(--danger-fg)]"
          : "border-[var(--border)] bg-[color-mix(in_srgb,var(--fg-0)_5%,transparent)] text-[var(--fg-1)] hover:text-[var(--fg-0)]",
      ].join(" ")}
    >
      <span className="inline-block min-w-[88px] truncate text-right">¥{compactBalanceText}</span>
    </Link>
  );
}
