"use client";

// Authenticated React Query key policy and identity-aware cache lifecycle.

import {
  type QueryClient,
  type QueryKey,
} from "@tanstack/react-query";
import { useChatStore } from "@/store/useChatStore";

export const AUTH_USER_QUERY_KEY = ["me"] as const;
export const USER_QUERY_SCOPE = "user" as const;
export const UNKNOWN_USER_QUERY_SCOPE = "__identity_unknown__" as const;

export function hasKnownUserIdentity(
  userId: string | null | undefined,
): userId is string {
  return typeof userId === "string" && userId.trim().length > 0;
}

export function userScopedQueryKey<TKey extends QueryKey>(
  userId: string | null | undefined,
  queryKey: TKey,
): readonly [typeof USER_QUERY_SCOPE, string, ...TKey] {
  return [
    USER_QUERY_SCOPE,
    hasKnownUserIdentity(userId) ? userId : UNKNOWN_USER_QUERY_SCOPE,
    ...queryKey,
  ];
}

export function isUserScopedQueryKeyForUser(
  queryKey: QueryKey,
  userId: string | null | undefined,
): boolean {
  return (
    hasKnownUserIdentity(userId) &&
    queryKey[0] === USER_QUERY_SCOPE &&
    queryKey[1] === userId
  );
}

type WalletTransactionsQueryKeyParams = Readonly<{
  kind: string;
  limit: number;
  pagination: "infinite" | "list";
}>;

type RedemptionsQueryKeyParams = Readonly<{
  limit: number;
  pagination: "infinite" | "list";
}>;

type TaskStatusFilter = "all" | "active" | "failed";

export const userBillingQueryKeys = {
  all: (userId: string | null | undefined) =>
    userScopedQueryKey(userId, ["billing"] as const),
  wallet: (userId: string | null | undefined) =>
    userScopedQueryKey(userId, ["billing", "wallet", "summary"] as const),
  walletTransactions: (
    userId: string | null | undefined,
    params: WalletTransactionsQueryKeyParams,
  ) =>
    userScopedQueryKey(
      userId,
      ["billing", "wallet", "transactions", params] as const,
    ),
  pricing: (userId: string | null | undefined) =>
    userScopedQueryKey(userId, ["billing", "pricing"] as const),
  snapshot: (userId: string | null | undefined) =>
    userScopedQueryKey(userId, ["billing", "snapshot"] as const),
  redemptions: (
    userId: string | null | undefined,
    params: RedemptionsQueryKeyParams,
  ) =>
    userScopedQueryKey(
      userId,
      ["billing", "redemptions", params] as const,
    ),
  } as const;

export const userMemoryQueryKeys = {
  all: (userId: string | null | undefined) =>
    userScopedQueryKey(userId, ["me", "memory"] as const),
  settings: (userId: string | null | undefined) =>
    userScopedQueryKey(userId, ["me", "memory", "settings"] as const),
  scopes: (userId: string | null | undefined) =>
    userScopedQueryKey(userId, ["me", "memory", "scopes"] as const),
  items: (userId: string | null | undefined, scopeId: string) =>
    userScopedQueryKey(userId, ["me", "memory", "items", scopeId] as const),
  staging: (userId: string | null | undefined) =>
    userScopedQueryKey(userId, ["me", "memory", "staging"] as const),
  timeline: (userId: string | null | undefined) =>
    userScopedQueryKey(userId, ["me", "memory", "timeline"] as const),
} as const;

export const userTaskQueryKeys = {
  all: (userId: string | null | undefined) =>
    userScopedQueryKey(userId, ["tasks"] as const),
  recent: (
    userId: string | null | undefined,
    status: TaskStatusFilter = "all",
  ) => userScopedQueryKey(userId, ["tasks", "recent", status] as const),
  islandActive: (userId: string | null | undefined) =>
    userScopedQueryKey(userId, ["tasks", "island", "active"] as const),
  islandRecent: (userId: string | null | undefined) =>
    userScopedQueryKey(userId, ["tasks", "island", "recent"] as const),
  presence: (userId: string | null | undefined) =>
    userScopedQueryKey(userId, ["tasks", "recent", "presence"] as const),
} as const;

export const userConversationQueryKeys = {
  detail: (userId: string | null | undefined, conversationId: string) =>
    userScopedQueryKey(userId, ["conversation", conversationId] as const),
  usedMemories: (
    userId: string | null | undefined,
    conversationId: string,
  ) =>
    userScopedQueryKey(userId, [
      "conversation",
      conversationId,
      "used-memories",
    ] as const),
} as const;

function isAuthUserQueryKey(queryKey: QueryKey): boolean {
  return queryKey.length === 1 && queryKey[0] === AUTH_USER_QUERY_KEY[0];
}

export function clearPreviousUserQueryCache(
  client: QueryClient,
  previousUserId: string,
) {
  const isPreviousUserQuery = ({ queryKey }: { queryKey: QueryKey }) => {
    if (queryKey[0] === USER_QUERY_SCOPE) {
      return (
        queryKey[1] === previousUserId ||
        queryKey[1] === UNKNOWN_USER_QUERY_SCOPE
      );
    }
    // Clear legacy authenticated keys during the migration to user-scoped
    // keys. Keep only the identity bootstrap and explicitly public auth data.
    return !isAuthUserQueryKey(queryKey) && queryKey[0] !== "auth";
  };
  const queries = client.getQueryCache().findAll({
    predicate: isPreviousUserQuery,
  });

  // A removed Query can still be referenced by a mounted QueryObserver. Reset
  // first so that observer gets an empty result before the cache entry dies.
  for (const query of queries) {
    query.reset();
  }
  client.removeQueries({
    predicate: isPreviousUserQuery,
  });
}

export function prepareUserIdentityRevalidation(
  client: QueryClient,
  previousUserId: string | null | undefined,
) {
  client
    .getQueryCache()
    .find({
      queryKey: AUTH_USER_QUERY_KEY,
      exact: true,
    })
    ?.reset();
  clearPreviousUserQueryCache(
    client,
    hasKnownUserIdentity(previousUserId)
      ? previousUserId
      : UNKNOWN_USER_QUERY_SCOPE,
  );
}

export function useUserQueryScope() {
  const userId = useChatStore((state) => state.currentUserId);
  return {
    userId,
    enabled: hasKnownUserIdentity(userId),
  };
}
