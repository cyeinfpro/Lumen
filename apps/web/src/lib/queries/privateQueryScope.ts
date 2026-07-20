"use client";

import {
  useQueryClient,
  type QueryKey,
  type UseQueryOptions,
} from "@tanstack/react-query";

import {
  isUserScopedQueryKeyForUser,
  useUserQueryScope,
} from "@/lib/queries/userScope";
import { qk } from "./queryKeys";

export { isUserScopedQueryKeyForUser };

export function privateQueryEnabled<
  TQueryFnData = unknown,
  TError = Error,
  TData = TQueryFnData,
  TQueryKey extends QueryKey = QueryKey,
>(
  identityEnabled: boolean,
  requestedEnabled:
    | UseQueryOptions<TQueryFnData, TError, TData, TQueryKey>["enabled"]
    | undefined,
  ...requirements: boolean[]
): UseQueryOptions<TQueryFnData, TError, TData, TQueryKey>["enabled"] {
  if (!identityEnabled || requirements.some((requirement) => !requirement)) {
    return false;
  }
  return requestedEnabled ?? true;
}

export function useCurrentUserQueryKeys() {
  const userScope = useUserQueryScope();
  return {
    userScope,
    userKeys: qk.user(userScope.userId),
  };
}

export function useCurrentUserQueryClient() {
  const { userScope, userKeys } = useCurrentUserQueryKeys();
  return {
    queryClient: useQueryClient(),
    userScope,
    userKeys,
  };
}
