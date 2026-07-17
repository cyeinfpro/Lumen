"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { type ReactNode, useEffect, useRef, useState } from "react";
import { getMe, getMyWallet, getPricing, type AuthUser } from "@/lib/apiClient";
import { formatRmb, formatRmbCompact } from "@/lib/money";

export interface MobileTopBarProps {
  left?: ReactNode;
  right?: ReactNode;
  below?: ReactNode;
  showWallet?: boolean;
  /** 当页面滚动超过 10px 时才玻璃化。需要挂 sentinel：<div data-topbar-sentinel /> */
  glassOnScroll?: boolean;
  className?: string;
}

export function MobileTopBar({
  left,
  right,
  below,
  showWallet = true,
  glassOnScroll = true,
  className = "",
}: MobileTopBarProps) {
  const [glass, setGlass] = useState(false);
  const ref = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!glassOnScroll) return;
    const root =
      ref.current?.closest<HTMLElement>("[data-app-viewport]") ??
      ref.current?.closest<HTMLElement>("[data-lumen-app-shell]");
    const scroller =
      root?.querySelector<HTMLElement>("[data-app-scroll]") ?? window;
    const onScroll = () =>
      setGlass(
        scroller === window
          ? window.scrollY > 8
          : (scroller as HTMLElement).scrollTop > 8,
      );
    scroller.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
    return () => scroller.removeEventListener("scroll", onScroll);
  }, [glassOnScroll]);

  const showGlass = glassOnScroll && glass;

  return (
    <header
      ref={ref}
      className={[
        "adaptive-material sticky left-0 right-0 top-0 shrink-0 safe-x",
        "transition-[background-color,backdrop-filter,border-color] duration-[var(--dur-normal)]",
        showGlass
          ? "bg-[var(--bg-0)]/72 backdrop-blur-xl mobile-perf-surface border-b border-[var(--border-subtle)]"
          : "bg-transparent border-b border-transparent",
        className,
      ].join(" ")}
      style={{
        zIndex: "var(--z-header, 10)" as unknown as number,
        paddingTop:
          "calc(env(safe-area-inset-top, 0px) + var(--top-banner-stack-height, 0px))",
      }}
    >
      <div className="relative mx-auto flex min-h-[var(--mobile-topbar-h)] max-w-[640px] items-center gap-2 px-3 [@media(max-width:390px)]:gap-1">
        <div className="flex min-w-0 flex-1 items-center gap-1.5 overflow-hidden">
          {left}
        </div>
        <div className="flex shrink-0 items-center justify-end gap-1 [@media(max-width:390px)]:gap-0.5">
          {showWallet ? <MobileWalletPill /> : null}
          {right}
        </div>
      </div>
      {below ? (
        <div className="mx-auto min-w-0 max-w-[640px] px-3 pb-2">{below}</div>
      ) : null}
    </header>
  );
}

function MobileWalletPill() {
  const pathname = usePathname();
  const meQuery = useQuery<AuthUser>({
    queryKey: ["me"],
    queryFn: getMe,
    retry: false,
    staleTime: 60_000,
  });
  const enabled =
    meQuery.data?.account_mode === "wallet" && !pathname.startsWith("/admin");
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
        "inline-flex min-h-11 max-w-[88px] shrink items-center rounded-full border px-2 text-[11px] font-medium tabular-nums",
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
