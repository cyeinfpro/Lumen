"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { type ReactNode, useEffect, useRef, useState } from "react";
import { getMe, getMyWallet, getPricing, type AuthUser } from "@/lib/apiClient";
import { formatRmb, formatRmbCompact } from "@/lib/money";
import { isDesktopRuntime } from "@/lib/desktop/runtime";

export interface MobileTopBarProps {
  left?: ReactNode;
  right?: ReactNode;
  /** 当页面滚动超过 10px 时才玻璃化。需要挂 sentinel：<div data-topbar-sentinel /> */
  glassOnScroll?: boolean;
  className?: string;
}

export function MobileTopBar({
  left,
  right,
  glassOnScroll = true,
  className = "",
}: MobileTopBarProps) {
  const [glass, setGlass] = useState(false);
  const ref = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!glassOnScroll) return;
    const el = document.querySelector<HTMLElement>("[data-topbar-sentinel]");
    if (!el) {
      // fallback: 监听 window scroll
      const onScroll = () => setGlass(window.scrollY > 8);
      window.addEventListener("scroll", onScroll, { passive: true });
      onScroll();
      return () => window.removeEventListener("scroll", onScroll);
    }
    const io = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          setGlass(!e.isIntersecting);
        }
      },
      { threshold: 0 },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [glassOnScroll]);

  return (
    <header
      ref={ref}
      className={[
        "sticky top-0 left-0 right-0 safe-x",
        "transition-[background-color,backdrop-filter,border-color] duration-200",
        glass
          ? "bg-[var(--bg-0)]/72 backdrop-blur-xl mobile-perf-surface border-b border-[var(--border-subtle)]"
          : "bg-transparent border-b border-transparent",
        className,
      ].join(" ")}
      style={{
        zIndex: "var(--z-header, 10)" as unknown as number,
        paddingTop: "calc(env(safe-area-inset-top, 0px) + var(--system-banner-height, 0px))",
      }}
    >
      <div className="relative flex min-h-11 items-center max-w-[640px] mx-auto px-3 gap-2 [@media(max-width:390px)]:gap-1.5">
        <div className="flex-1 min-w-0 flex items-center gap-1.5 overflow-hidden">
          {left}
        </div>
        <div className="flex min-w-0 shrink-0 items-center justify-end gap-1 [@media(max-width:390px)]:gap-0.5">
          <MobileWalletPill />
          {right}
        </div>
      </div>
    </header>
  );
}

function MobileWalletPill() {
  const pathname = usePathname();
  const desktop = isDesktopRuntime();
  const meQuery = useQuery<AuthUser>({
    queryKey: ["me"],
    queryFn: getMe,
    retry: false,
    staleTime: 60_000,
  });
  const enabled =
    !desktop && meQuery.data?.account_mode === "wallet" && !pathname.startsWith("/admin");
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
        "inline-flex h-7 max-w-[88px] shrink items-center rounded-full border px-2 text-[11px] font-medium tabular-nums",
        "[@media(max-width:390px)]:max-w-[64px] [@media(max-width:360px)]:hidden",
        "truncate font-mono",
        low || negative
          ? "border-danger-border bg-danger-soft text-[var(--danger-fg)]"
          : "border-[var(--border)] bg-[color-mix(in_srgb,var(--fg-0)_5%,transparent)] text-[var(--fg-1)]",
      ].join(" ")}
    >
      <span className="inline-block min-w-0 max-w-full truncate text-right">
        ¥{compactBalanceText}
      </span>
    </Link>
  );
}
