// Lumen V1 TanStack Query hooks（预留给 Agent D 的页面层使用）。
// 约定：
//  - queryKey 采用 ['domain', ...params] 形式，domain 单词不复数
//  - mutation 在 onSuccess 里 invalidate 相关的只读 queryKey，保证页面自动刷新
//  - 所有网络错误由 apiFetch 抛 ApiError；这里不做 catch，交给上层 ErrorBoundary / UI
//
// 注意：这里只暴露 hooks，不在模块顶层读 queryClient——客户端组件里用。

import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
  type InfiniteData,
  type UseMutationOptions,
  type UseQueryOptions,
} from "@tanstack/react-query";
import {
  createConversation,
  deleteConversation,
  listConversations,
  patchConversation,
  getConversationContext,
  type ConversationListResponse,
  type ConversationSummary,
  type ConversationContextStats,
  type CreateConversationIn,
  type ListConversationsOpts,
  type PatchConversationIn,
} from "../api/conversations";
import {
  privateQueryEnabled,
  useCurrentUserQueryKeys,
} from "./privateQueryScope";

export function useListConversationsQuery(
  opts?: ListConversationsOpts,
  options?: Omit<
    UseQueryOptions<ConversationListResponse>,
    "queryKey" | "queryFn"
  >,
) {
  const { userScope, userKeys } = useCurrentUserQueryKeys();
  return useQuery<ConversationListResponse>({
    queryKey: userKeys.conversations(opts),
    queryFn: () => listConversations(opts),
    ...options,
    enabled: privateQueryEnabled(userScope.enabled, options?.enabled),
  });
}

export function useListConversationsInfiniteQuery(params?: {
  limit?: number;
  q?: string;
}) {
  const { userScope, userKeys } = useCurrentUserQueryKeys();
  const limit = params?.limit ?? 30;
  const q = params?.q;
  return useInfiniteQuery<
    ConversationListResponse,
    Error,
    InfiniteData<ConversationListResponse, string | undefined>,
    readonly [
      "user",
      string,
      "conversations",
      "infinite",
      { limit: number; q?: string },
    ],
    string | undefined
  >({
    queryKey: userKeys.conversationsInfinite({
      limit,
      ...(q ? { q } : {}),
    }),
    queryFn: ({ pageParam }) =>
      listConversations({ limit, q, cursor: pageParam }),
    initialPageParam: undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: userScope.enabled,
  });
}

export function useCreateConversationMutation(
  options?: Omit<
    UseMutationOptions<
      ConversationSummary,
      Error,
      CreateConversationIn | void
    >,
    "mutationFn"
  >,
) {
  const { userKeys } = useCurrentUserQueryKeys();
  const qc = useQueryClient();
  return useMutation<ConversationSummary, Error, CreateConversationIn | void>({
    mutationFn: (body) => createConversation(body ?? {}),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.invalidateQueries({ queryKey: userKeys.conversationsAll() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

interface PatchConversationVars extends PatchConversationIn {
  id: string;
}

export function usePatchConversationMutation(
  options?: Omit<
    UseMutationOptions<ConversationSummary, Error, PatchConversationVars>,
    "mutationFn"
  >,
) {
  const { userKeys } = useCurrentUserQueryKeys();
  const qc = useQueryClient();
  return useMutation<ConversationSummary, Error, PatchConversationVars>({
    mutationFn: ({ id, ...rest }) => patchConversation(id, rest),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      // 仅 invalidate 列表页（含 infinite）和该单条 detail；避免冲掉 messages / 其它无关 query
      qc.invalidateQueries({
        queryKey: userKeys.conversationsInfiniteAll(),
      });
      qc.invalidateQueries({
        queryKey: userKeys.conversationDetail(vars.id),
      });
      qc.invalidateQueries({
        queryKey: userKeys.conversationContext(vars.id),
      });
      qc.invalidateQueries({ queryKey: ["stream", "feed"] });
      qc.invalidateQueries({
        queryKey: userKeys.conversationsAll(),
        exact: false,
        predicate: (q) => {
          const key = q.queryKey;
          // userKeys.conversations(opts) → ["user", userId, "conversations", opts]
          return (
            Array.isArray(key) &&
            key[0] === "user" &&
            key[1] === userKeys.conversationsAll()[1] &&
            key[2] === "conversations" &&
            typeof key[3] === "object"
          );
        },
      });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export function useDeleteConversationMutation(
  options?: Omit<UseMutationOptions<void, Error, string>, "mutationFn">,
) {
  const { userKeys } = useCurrentUserQueryKeys();
  const qc = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: (id) => deleteConversation(id),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.invalidateQueries({ queryKey: userKeys.conversationsAll() });
      qc.invalidateQueries({ queryKey: ["stream", "feed"] });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export function useConversationContextQuery(
  convId: string | null | undefined,
  options?: Omit<UseQueryOptions<ConversationContextStats>, "queryKey" | "queryFn">,
) {
  const { userScope, userKeys } = useCurrentUserQueryKeys();
  return useQuery<ConversationContextStats>({
    queryKey: userKeys.conversationContext(convId ?? ""),
    queryFn: () => getConversationContext(convId as string),
    staleTime: 10_000,
    ...options,
    enabled: privateQueryEnabled(
      userScope.enabled,
      options?.enabled,
      typeof convId === "string" && convId.length > 0,
    ),
  });
}
