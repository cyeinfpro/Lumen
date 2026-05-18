"use client";

// 桌面端顶部主导航：复用在 DesktopStudio / DesktopStream / DesktopMe。
// 四 Tab 横向导航 + 左侧 Logo + 可配置右侧 slot。
// 与 MobileTabBar 的路由契约保持一致：/ → 创作；/projects → 项目；/stream → 图库；/me、/settings → 我的。
// 模特库 /library 不再是顶级入口，由项目页内入口跳入；停留在 /library 时高亮「项目」。

import { motion } from "framer-motion";
import { useQuery } from "@tanstack/react-query";
import { Menu } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useCallback, useMemo, useRef, type KeyboardEvent, type ReactNode } from "react";

import { IconButton } from "@/components/ui/primitives";
import { getMe, getMyWallet, getPricing, type AuthUser } from "@/lib/apiClient";
import { formatRmb, formatRmbCompact } from "@/lib/money";
import { SPRING } from "@/lib/motion";

export type DesktopNavTab = "studio" | "projects" | "stream" | "me";

interface TabDef {
  key: DesktopNavTab;
  label: string;
  route: string;
}

const TABS: TabDef[] = [
  { key: "studio", label: "创作", route: "/" },
  { key: "projects", label: "项目", route: "/projects" },
  { key: "stream", label: "图库", route: "/stream" },
  { key: "me", label: "我的", route: "/me" },
];

export interface DesktopTopNavProps {
  active: DesktopNavTab;
  right?: ReactNode;
  onToggleSidebar?: () => void;
}

export function DesktopTopNav({ active, right, onToggleSidebar }: DesktopTopNavProps) {
  const pathname = usePathname();
  const router = useRouter();

  const currentActive: DesktopNavTab = useMemo(() => {
    if (pathname === "/" || pathname === "") return "studio";
    if (pathname.startsWith("/library")) return "projects";
    if (pathname.startsWith("/projects")) return "projects";
    if (pathname.startsWith("/stream")) return "stream";
    if (pathname.startsWith("/me") || pathname.startsWith("/settings")) return "me";
    return active;
  }, [pathname, active]);

  const onTap = useCallback(
    (tab: TabDef) => {
      const onExactRoute =
        tab.route === "/" ? pathname === "/" : pathname === tab.route || pathname.startsWith(tab.route + "/");
      if (onExactRoute) return;
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
        "sticky top-0 grid w-full h-11 items-center gap-2 px-3 md:px-5",
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
            size="sm"
            aria-label="切换侧栏"
            title="切换侧栏 (⌘K)"
            tooltip="切换侧栏 (⌘K)"
            onClick={onToggleSidebar}
            className="rounded-full"
          >
            <Menu className="w-4.5 h-4.5" />
          </IconButton>
        )}
        <Link href="/" className="flex items-center gap-2 shrink-0" aria-label="Lumen 首页">
          {/* Lumen 品牌徽标渐变：琥珀→orange，非状态色，token 化无意义 */}
          {/* eslint-disable-next-line no-restricted-syntax */}
          <div className="w-5 h-5 rounded-full bg-gradient-to-tr from-[var(--amber-400)] to-orange-200 shadow-[var(--shadow-amber)]" />
          <span className="hidden sm:inline text-[13px] font-medium tracking-tight text-[var(--fg-0)]">Lumen</span>
        </Link>
      </div>

      {/* Middle: Tabs — 占据中间 1fr，居中显示，可挤压 */}
      <nav aria-label="主导航" className="flex min-w-0 flex-1 justify-center overflow-hidden">
        <ul ref={tabsRef} onKeyDown={onTabsKeyDown} className="flex items-center gap-1">
          {TABS.map((tab) => {
            const isActive = tab.key === currentActive;
            return (
              <li key={tab.key} className="relative">
                {/* 顶部导航 Tab：内嵌 layoutId 动画下划线 + tabsRef 键盘导航需要原生 button */}
                <button
                  type="button"
                  onClick={() => onTap(tab)}
                  aria-current={isActive ? "page" : undefined}
                  className={[
                    "relative px-2.5 py-1.5 rounded-md text-[13px] font-medium transition-colors cursor-pointer whitespace-nowrap",
                    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
                    isActive
                      ? "text-[var(--fg-0)]"
                      : "text-[var(--fg-2)] hover:text-[var(--fg-0)]",
                  ].join(" ")}
                >
                  {tab.label}
                  {isActive && (
                    <motion.span
                      layoutId="desktop-nav-underline"
                      aria-hidden
                      className="absolute left-2 right-2 -bottom-[9px] h-0.5 rounded-full bg-[var(--amber-400)] shadow-[var(--shadow-amber)]"
                      transition={SPRING.snap}
                    />
                  )}
                </button>
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
  const meQuery = useQuery<AuthUser>({
    queryKey: ["me"],
    queryFn: getMe,
    retry: false,
    staleTime: 60_000,
  });
  const enabled = meQuery.data?.account_mode === "wallet";
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
        "hidden max-w-[140px] shrink-0 items-center rounded-full border px-2.5 py-1 text-[12px] font-medium tabular-nums sm:inline-flex",
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
