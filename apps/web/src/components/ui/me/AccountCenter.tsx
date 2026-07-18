"use client";

import { useQuery } from "@tanstack/react-query";

import {
  AUTH_USER_QUERY_KEY,
  userBillingQueryKeys,
  userMemoryQueryKeys,
  useUserQueryScope,
} from "@/components/QueryProvider";
import {
  getMe,
  getMyWallet,
  listMemoryStaging,
  type AuthUser,
} from "@/lib/apiClient";
import { useSystemPromptsQuery } from "@/lib/queries";
import { useChatStore } from "@/store/useChatStore";

import { AccountCenterMenu } from "./AccountCenterMenu";
import {
  countItems,
  deriveAccountIdentity,
  formatWalletBalance,
} from "./accountCenterModel";
import { useAccountLogout } from "./useAccountLogout";

function useAccountCenterState() {
  const userScope = useUserQueryScope();
  const meQuery = useQuery<AuthUser>({
    queryKey: AUTH_USER_QUERY_KEY,
    queryFn: getMe,
    retry: false,
    staleTime: 60_000,
  });
  const identity = deriveAccountIdentity(meQuery.data, userScope);
  const identityReady =
    userScope.enabled && meQuery.data?.id === userScope.userId;
  const walletQuery = useQuery({
    queryKey: userBillingQueryKeys.wallet(userScope.userId),
    queryFn: getMyWallet,
    enabled: identity.walletEnabled,
    retry: false,
    staleTime: 30_000,
  });

  const promptsQuery = useSystemPromptsQuery({
    enabled: identityReady,
  });
  const stagingQuery = useQuery({
    queryKey: userMemoryQueryKeys.staging(userScope.userId),
    queryFn: listMemoryStaging,
    retry: false,
    staleTime: 30_000,
    enabled: identityReady,
  });

  const fast = useChatStore((state) => state.composer?.fast ?? false);
  const onFastChange = useChatStore((state) => state.setFast);

  return {
    accountMode: identity.accountMode,
    isAdmin: identity.isAdmin,
    walletBalance: formatWalletBalance(walletQuery.data?.balance),
    promptCount: countItems(promptsQuery.data?.items),
    stagingCount: countItems(stagingQuery.data?.items),
    fast,
    onFastChange,
  };
}

export function AccountCenter() {
  const state = useAccountCenterState();
  const logout = useAccountLogout();
  return <AccountCenterMenu {...state} logout={logout} />;
}
