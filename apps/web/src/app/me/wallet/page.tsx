"use client";

import { getMyWallet, type AuthUser } from "@/lib/apiClient";
import {
  userBillingQueryKeys,
  useUserQueryScope,
} from "@/components/QueryProvider";

import { ByokWalletPage, WalletPageView } from "./WalletPageView";
import {
  useWalletPageModel,
  type WalletPageQueryKeys,
} from "./useWalletPageModel";

function isByokWalletAccount(
  wallet: Awaited<ReturnType<typeof getMyWallet>> | undefined,
  accountMode: AuthUser["account_mode"] | undefined,
): boolean {
  return wallet?.mode === "byok" || accountMode === "byok";
}

function walletActivity24h(
  wallet: Awaited<ReturnType<typeof getMyWallet>> | undefined,
) {
  return {
    topup: (wallet?.activity_24h?.topup?.micro ?? 0) / 1_000_000,
    spend: (wallet?.activity_24h?.spend?.micro ?? 0) / 1_000_000,
  };
}

export default function WalletPage() {
  const userScope = useUserQueryScope();
  const queryKeys: WalletPageQueryKeys = {
    all: userBillingQueryKeys.all(userScope.userId),
    wallet: userBillingQueryKeys.wallet(userScope.userId),
    snapshot: userBillingQueryKeys.snapshot(userScope.userId),
    transactions: (kind) =>
      userBillingQueryKeys.walletTransactions(userScope.userId, {
        kind,
        limit: 30,
        pagination: "infinite",
      }),
    redemptions: userBillingQueryKeys.redemptions(userScope.userId, {
      limit: 20,
      pagination: "infinite",
    }),
  };
  const model = useWalletPageModel({ userScope, queryKeys });
  const { wallet } = model;
  const stats24h = walletActivity24h(wallet);

  if (isByokWalletAccount(wallet, model.accountMode)) {
    return <ByokWalletPage />;
  }

  return <WalletPageView model={model} activity24h={stats24h} />;
}
