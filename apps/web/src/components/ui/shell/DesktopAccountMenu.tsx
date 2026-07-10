"use client";

import { useQuery } from "@tanstack/react-query";
import {
  Brain,
  ChevronRight,
  CircleUserRound,
  CreditCard,
  FileText,
  Settings,
  Shield,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useMemo, useRef, useState } from "react";

import { DesktopPopover } from "@/components/ui/composer/desktop/DesktopPopover";
import {
  getMe,
  getMyWallet,
  getPricing,
  type AuthUser,
} from "@/lib/apiClient";
import { isDesktopRuntime } from "@/lib/desktop/runtime";
import { formatRmb } from "@/lib/money";
import { cn } from "@/lib/utils";

type MenuUser = AuthUser & { role?: "admin" | "member" };

function walletIsEnabled(desktop: boolean, user: MenuUser | undefined): boolean {
  return !desktop && user?.account_mode === "wallet";
}

function walletIsVisible({
  enabled,
  billingEnabled,
  hasBalance,
}: {
  enabled: boolean;
  billingEnabled: boolean | null | undefined;
  hasBalance: boolean;
}): boolean {
  return enabled && billingEnabled !== false && hasBalance;
}

function accountPathIsActive(pathname: string): boolean {
  return ["/me", "/settings", "/admin"].some((prefix) =>
    pathname.startsWith(prefix),
  );
}

function formatWalletText(
  showWallet: boolean,
  balance: { rmb: string } | null | undefined,
): string | null {
  return showWallet && balance ? `¥${formatRmb(balance.rmb)}` : null;
}

function accountMenuItems({
  desktop,
  showWallet,
  isAdmin,
}: {
  desktop: boolean;
  showWallet: boolean;
  isAdmin: boolean;
}) {
  const items = [
    {
      href: "/me",
      label: desktop ? "设置" : "账户",
      icon: desktop ? Settings : CircleUserRound,
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
  if (isAdmin && !desktop) {
    items.push({ href: "/admin", label: "管理后台", icon: Shield });
  }
  return items;
}

export function DesktopAccountMenu() {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLDivElement | null>(null);
  const pathname = usePathname();
  const desktop = isDesktopRuntime();
  const meQuery = useQuery<MenuUser>({
    queryKey: ["me"],
    queryFn: () => getMe() as Promise<MenuUser>,
    retry: false,
    staleTime: 60_000,
  });
  const walletEnabled = walletIsEnabled(desktop, meQuery.data);
  const walletQuery = useQuery({
    queryKey: ["me", "wallet"],
    queryFn: getMyWallet,
    enabled: walletEnabled,
    retry: false,
    staleTime: 30_000,
  });
  const pricingQuery = useQuery({
    queryKey: ["me", "pricing"],
    queryFn: getPricing,
    enabled: walletEnabled,
    retry: false,
    staleTime: 60_000,
  });

  const label = meQuery.data?.name || meQuery.data?.email || "账户";
  const avatar = label.slice(0, 1).toUpperCase();
  const wallet = walletQuery.data;
  const walletBalance = wallet?.balance;
  const showWallet = walletIsVisible({
    enabled: walletEnabled,
    billingEnabled: pricingQuery.data?.billing_enabled,
    hasBalance: walletBalance != null,
  });
  const walletText = formatWalletText(showWallet, walletBalance);
  const active = accountPathIsActive(pathname);
  const items = useMemo(
    () =>
      accountMenuItems({
        desktop,
        showWallet,
        isAdmin: meQuery.data?.role === "admin",
      }),
    [desktop, meQuery.data?.role, showWallet],
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
          "inline-flex h-9 w-9 items-center justify-center rounded-full",
          "border border-[var(--border)] bg-[var(--bg-2)] text-[12px] font-semibold text-[var(--fg-0)]",
          "transition-[background-color,border-color] duration-[var(--dur-quick)]",
          "hover:border-[var(--border-strong)] hover:bg-[var(--bg-3)]",
          "focus-visible:outline-none focus-visible:shadow-[var(--ring)]",
          active && "border-[var(--border-strong)] bg-[var(--surface-selected)]",
        )}
      >
        {avatar}
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
          <p className="truncate text-[13px] font-medium text-[var(--fg-0)]">
            {label}
          </p>
          <div className="mt-1 flex items-center justify-between gap-3 text-[11px] text-[var(--fg-2)]">
            <span className="truncate">
              {meQuery.data?.email || (desktop ? "本机模式" : "Lumen 账户")}
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
                  "text-[13px] text-[var(--fg-1)] transition-colors duration-[var(--dur-quick)]",
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
