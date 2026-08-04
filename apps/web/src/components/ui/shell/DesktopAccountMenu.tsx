"use client";

import { useQuery } from "@tanstack/react-query";
import {
  Brain,
  ChevronRight,
  CircleUserRound,
  CreditCard,
  FileText,
  Shield,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useMemo, useRef, useState } from "react";

import { DesktopPopover } from "@/components/ui/composer/desktop/DesktopPopover";
import {
  AUTH_USER_QUERY_KEY,
  userBillingQueryKeys,
  useUserQueryScope,
} from "@/components/QueryProvider";
import { Avatar } from "@/components/ui/primitives";
import {
  getMe,
  getMyWallet,
  getPricing,
  type AuthUser,
} from "@/lib/apiClient";
import { formatRmb } from "@/lib/money";
import { cn } from "@/lib/utils";

type MenuUser = AuthUser & { role?: "admin" | "member" };

function walletIsEnabled(user: MenuUser | undefined): boolean {
  return user?.account_mode === "wallet";
}

/**
 * 新-16：钱包**入口**只取决于「这个账号是钱包模式且计费没被关掉」，
 * 与余额查询成功与否无关。此前把 hasBalance 也算进来，导致 /me/wallet 请求
 * 失败（离线、5xx、限流）时用户明明有钱却找不到入口 —— 余额查不到恰恰是最需要
 * 让用户点进去看的时候。billingEnabled 用 `!== false`：pricing 还在加载/失败时
 * 保持入口可见，不因为一个附属查询把主导航藏起来。
 */
function walletEntryIsVisible({
  enabled,
  billingEnabled,
}: {
  enabled: boolean;
  billingEnabled: boolean | null | undefined;
}): boolean {
  return enabled && billingEnabled !== false;
}

/** 新-15：balance.rmb 缺失（错误态）不能当作"有余额"，否则会渲染出假的 ¥-- */
function hasUsableBalance(balance: { rmb?: string | null } | null | undefined) {
  return balance != null && balance.rmb != null;
}

function accountPathIsActive(pathname: string): boolean {
  return ["/me", "/settings", "/admin"].some((prefix) =>
    pathname.startsWith(prefix),
  );
}

function formatWalletText(
  showWallet: boolean,
  balance: { rmb?: string | null } | null | undefined,
): string | null {
  // 金额文案与入口解耦：入口可见 ≠ 余额可读，读不到就不渲染金额（而不是 ¥--）。
  const rmb = hasUsableBalance(balance) ? balance?.rmb : null;
  return showWallet && rmb != null ? `¥${formatRmb(rmb)}` : null;
}

function accountMenuItems({
  showWallet,
  isAdmin,
}: {
  showWallet: boolean;
  isAdmin: boolean;
}) {
  const items = [
    {
      href: "/me",
      label: "账户",
      icon: CircleUserRound,
    },
    { href: "/settings/memory", label: "记忆", icon: Brain },
    { href: "/settings/prompts", label: "系统提示词", icon: FileText },
  ];
  if (showWallet) {
    items.splice(1, 0, {
      href: "/me/wallet",
      label: "钱包与账单",
      icon: CreditCard,
    });
  }
  if (isAdmin) {
    items.push({ href: "/admin", label: "管理后台", icon: Shield });
  }
  return items;
}

export function DesktopAccountMenu() {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLDivElement | null>(null);
  const pathname = usePathname();
  const userScope = useUserQueryScope();
  const meQuery = useQuery<MenuUser>({
    queryKey: AUTH_USER_QUERY_KEY,
    queryFn: () => getMe() as Promise<MenuUser>,
    retry: false,
    staleTime: 60_000,
  });
  const walletEnabled =
    userScope.enabled &&
    meQuery.data?.id === userScope.userId &&
    walletIsEnabled(meQuery.data);
  const walletQuery = useQuery({
    queryKey: userBillingQueryKeys.wallet(userScope.userId),
    queryFn: getMyWallet,
    enabled: walletEnabled,
    retry: false,
    staleTime: 30_000,
  });
  const pricingQuery = useQuery({
    queryKey: userBillingQueryKeys.pricing(userScope.userId),
    queryFn: getPricing,
    enabled: walletEnabled,
    retry: false,
    staleTime: 60_000,
  });

  const label = meQuery.data?.name || meQuery.data?.email || "账户";
  const avatar = label.slice(0, 1).toUpperCase();
  const wallet = walletQuery.data;
  const walletBalance = wallet?.balance;
  const showWallet = walletEntryIsVisible({
    enabled: walletEnabled,
    billingEnabled: pricingQuery.data?.billing_enabled,
  });
  const walletText = formatWalletText(showWallet, walletBalance);
  const active = accountPathIsActive(pathname);
  const items = useMemo(
    () =>
      accountMenuItems({
        showWallet,
        isAdmin: meQuery.data?.role === "admin",
      }),
    [meQuery.data?.role, showWallet],
  );

  return (
    <div ref={triggerRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-label="打开账户菜单"
        className={cn(
          "group inline-flex h-10 w-10 items-center justify-center rounded-full",
          "focus-visible:outline-none focus-visible:shadow-[var(--ring)]",
        )}
      >
        <Avatar
          size="sm"
          name={label}
          initials={avatar}
          className={cn(
            "transition-[background-color,border-color,color] duration-[var(--dur-quick)] group-hover:border-[var(--border-strong)] group-hover:bg-[var(--bg-3)]",
            active && "border-accent-border bg-accent-soft text-accent",
          )}
        />
      </button>

      <DesktopPopover
        open={open}
        onClose={() => setOpen(false)}
        anchorRef={triggerRef}
        ariaLabel="账户菜单"
        align="right"
        className="w-64 p-1.5"
      >
        <div className="border-b border-[var(--border-subtle)] px-3 py-2.5">
          <p className="truncate type-body-sm font-medium text-[var(--fg-0)]">
            {label}
          </p>
          <div className="mt-1 flex items-center justify-between gap-3 type-caption text-[var(--fg-2)]">
            <span className="truncate">
              {meQuery.data?.email || "Lumen 账户"}
            </span>
            {walletText ? (
              <span className="shrink-0 font-mono text-[var(--fg-1)]">
                {walletText}
              </span>
            ) : null}
          </div>
        </div>
        <nav className="grid gap-0.5 pt-1" aria-label="账户与设置">
          {items.map((item) => {
            const Icon = item.icon;
            return (
              <Link
                key={item.href}
                href={item.href}
                onClick={() => setOpen(false)}
                className={cn(
                  "flex min-h-10 items-center gap-3 rounded-[var(--radius-control)] px-3",
                  "type-body-sm text-[var(--fg-1)] transition-colors duration-[var(--dur-quick)]",
                  "hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)]",
                  "focus-visible:outline-none focus-visible:shadow-[var(--ring)]",
                )}
              >
                <Icon className="h-4 w-4 text-[var(--fg-2)]" aria-hidden />
                <span className="flex-1">{item.label}</span>
                <ChevronRight className="h-3.5 w-3.5 text-[var(--fg-3)]" aria-hidden />
              </Link>
            );
          })}
        </nav>
      </DesktopPopover>
    </div>
  );
}
