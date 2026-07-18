import type { AuthUser } from "@/lib/apiClient";
import { formatRmb } from "@/lib/money";

export type AccountMode = NonNullable<AuthUser["account_mode"]>;

interface UserIdentityScope {
  userId: string | null | undefined;
  enabled: boolean;
}

export function deriveAccountIdentity(
  user: AuthUser | undefined,
  userScope: UserIdentityScope,
) {
  const accountMode = user?.account_mode ?? "wallet";
  return {
    accountMode,
    isAdmin: user?.role === "admin",
    walletEnabled:
      userScope.enabled &&
      user?.id === userScope.userId &&
      accountMode === "wallet",
  };
}

export function formatWalletBalance(
  balance: { rmb: string } | null | undefined,
): string | undefined {
  return balance == null ? undefined : `¥${formatRmb(balance.rmb)}`;
}

export function countItems(
  items: readonly unknown[] | null | undefined,
): number {
  return items?.length ?? 0;
}
