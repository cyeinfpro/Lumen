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
  addAllowedEmail,
  createConversation,
  createInviteLink,
  createMultiShare,
  createShare,
  createSystemPrompt,
  deleteConversation,
  deleteMyAccount,
  deleteSystemPrompt,
  getMyUsage,
  getPublicInvite,
  getPublicShare,
  getSystemSettings,
  listAdminRequestEvents,
  listAdminUsers,
  listAllowedEmails,
  listConversations,
  listInviteLinks,
  listMessages,
  listMySessions,
  listMyShares,
  listSystemPrompts,
  patchConversation,
  patchSystemPrompt,
  removeAllowedEmail,
  revokeInviteLink,
  revokeMySession,
  revokeShare,
  setDefaultSystemPrompt,
  getAdminModels,
  getProviders,
  getProviderStats,
  getConversationContext,
  listAdminProxies,
  restartTelegramBot,
  testAdminProxy,
  testAllAdminProxies,
  updateAdminProxies,
  updateProviders,
  probeProviders,
  updateSystemSettings,
  type ConversationListResponse,
  type ConversationSummary,
  type ConversationContextStats,
  type CreateConversationIn,
  type ListConversationsOpts,
  type ListMessagesOpts,
  type MessageListResponse,
  type PatchConversationIn,
  type CreateSystemPromptIn,
  type PatchSystemPromptIn,
  type SystemPrompt,
  type SystemPromptListResponse,
} from "./apiClient";
import type {
  AdminUserOut,
  AdminRequestEventsOut,
  AdminModelsOut,
  AllowedEmailOut,
  InviteLinkOut,
  InviteLinkPublicOut,
  ProviderItemIn,
  ProviderProxyIn,
  ProvidersOut,
  ProvidersProbeOut,
  ProxyListOut,
  ProxyTestOut,
  PublicShareOut,
  SessionOut,
  ShareOut,
  SystemSettingsOut,
  UsageOut,
} from "./types";

// ——— Query keys ———
export const qk = {
  meUsage: () => ["me", "usage"] as const,
  allowedEmails: () => ["admin", "allowed_emails"] as const,
  adminUsers: (params?: { limit?: number; cursor?: string }) =>
    ["admin", "users", params ?? {}] as const,
  adminRequestEvents: (params?: {
    limit?: number;
    kind?: "all" | "generation" | "completion";
    status?: string;
    range?: "24h" | "7d" | "30d";
  }) => ["admin", "request_events", params ?? {}] as const,
  myShares: () => ["me", "shares"] as const,
  publicShare: (token: string) => ["share", token] as const,
  inviteLinks: () => ["admin", "invite_links"] as const,
  publicInvite: (token: string) => ["invite", token] as const,
  systemSettings: () => ["admin", "settings"] as const,
  adminModels: () => ["admin", "models"] as const,
  providers: () => ["admin", "providers"] as const,
  providerStats: () => ["admin", "providers", "stats"] as const,
  adminProxies: () => ["admin", "proxies"] as const,
  systemPrompts: () => ["system_prompts"] as const,
  mySessions: () => ["me", "sessions"] as const,
  conversations: (opts?: ListConversationsOpts) =>
    ["conversations", opts ?? {}] as const,
  conversationsInfinite: (params?: { limit?: number; q?: string }) =>
    ["conversations", "infinite", params ?? {}] as const,
  messages: (convId: string, opts?: ListMessagesOpts) =>
    ["messages", convId, opts ?? {}] as const,
  conversationContext: (convId: string) =>
    ["conversations", convId, "context"] as const,
};

// ——— Queries ———

// ——— System Prompts ———
export function useSystemPromptsQuery(
  options?: Omit<
    UseQueryOptions<SystemPromptListResponse>,
    "queryKey" | "queryFn"
  >,
) {
  return useQuery<SystemPromptListResponse>({
    queryKey: qk.systemPrompts(),
    queryFn: listSystemPrompts,
    ...options,
  });
}

export function useCreateSystemPromptMutation(
  options?: Omit<
    UseMutationOptions<SystemPrompt, Error, CreateSystemPromptIn>,
    "mutationFn"
  >,
) {
  const qc = useQueryClient();
  return useMutation<SystemPrompt, Error, CreateSystemPromptIn>({
    mutationFn: createSystemPrompt,
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.invalidateQueries({ queryKey: qk.systemPrompts() });
      qc.invalidateQueries({ queryKey: ["me"] });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export interface PatchSystemPromptVars extends PatchSystemPromptIn {
  id: string;
}

export function usePatchSystemPromptMutation(
  options?: Omit<
    UseMutationOptions<SystemPrompt, Error, PatchSystemPromptVars>,
    "mutationFn"
  >,
) {
  const qc = useQueryClient();
  return useMutation<SystemPrompt, Error, PatchSystemPromptVars>({
    mutationFn: ({ id, ...body }) => patchSystemPrompt(id, body),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.invalidateQueries({ queryKey: qk.systemPrompts() });
      qc.invalidateQueries({ queryKey: ["me"] });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export function useDeleteSystemPromptMutation(
  options?: Omit<UseMutationOptions<void, Error, string>, "mutationFn">,
) {
  const qc = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: deleteSystemPrompt,
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.invalidateQueries({ queryKey: qk.systemPrompts() });
      qc.invalidateQueries({ queryKey: ["conversations"] });
      qc.invalidateQueries({ queryKey: ["me"] });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export function useSetDefaultSystemPromptMutation(
  options?: Omit<UseMutationOptions<SystemPrompt, Error, string>, "mutationFn">,
) {
  const qc = useQueryClient();
  return useMutation<SystemPrompt, Error, string>({
    mutationFn: setDefaultSystemPrompt,
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.invalidateQueries({ queryKey: qk.systemPrompts() });
      qc.invalidateQueries({ queryKey: ["me"] });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}


export function useMyUsageQuery(
  options?: Omit<UseQueryOptions<UsageOut>, "queryKey" | "queryFn">,
) {
  return useQuery<UsageOut>({
    queryKey: qk.meUsage(),
    queryFn: getMyUsage,
    ...options,
  });
}

export function useAllowedEmailsQuery(
  options?: Omit<
    UseQueryOptions<{ items: AllowedEmailOut[] }>,
    "queryKey" | "queryFn"
  >,
) {
  return useQuery<{ items: AllowedEmailOut[] }>({
    queryKey: qk.allowedEmails(),
    queryFn: listAllowedEmails,
    ...options,
  });
}

export function useAdminUsersQuery(
  params?: { limit?: number; cursor?: string },
  options?: Omit<
    UseQueryOptions<{ items: AdminUserOut[]; next_cursor?: string }>,
    "queryKey" | "queryFn"
  >,
) {
  return useQuery<{ items: AdminUserOut[]; next_cursor?: string }>({
    queryKey: qk.adminUsers(params),
    queryFn: () => listAdminUsers(params),
    ...options,
  });
}

// 分页累加版本：让 TanStack 自管累加，避免在 React 19 里 set-state-in-effect。
type AdminUsersPage = { items: AdminUserOut[]; next_cursor?: string };
export function useAdminUsersInfiniteQuery(params?: { limit?: number }) {
  const limit = params?.limit ?? 50;
  return useInfiniteQuery<
    AdminUsersPage,
    Error,
    InfiniteData<AdminUsersPage, string | undefined>,
    readonly ["admin", "users", "infinite", { limit: number }],
    string | undefined
  >({
    queryKey: ["admin", "users", "infinite", { limit }] as const,
    queryFn: ({ pageParam }) => listAdminUsers({ limit, cursor: pageParam }),
    initialPageParam: undefined,
    getNextPageParam: (last) => last.next_cursor,
  });
}

export function useAdminRequestEventsQuery(
  params?: {
    limit?: number;
    kind?: "all" | "generation" | "completion";
    status?: string;
    range?: "24h" | "7d" | "30d";
  },
  options?: Omit<
    UseQueryOptions<AdminRequestEventsOut>,
    "queryKey" | "queryFn"
  >,
) {
  return useQuery<AdminRequestEventsOut>({
    queryKey: qk.adminRequestEvents(params),
    queryFn: () => listAdminRequestEvents(params),
    ...options,
  });
}

export function useMySharesQuery(
  options?: Omit<
    UseQueryOptions<{ items: ShareOut[] }>,
    "queryKey" | "queryFn"
  >,
) {
  return useQuery<{ items: ShareOut[] }>({
    queryKey: qk.myShares(),
    queryFn: listMyShares,
    ...options,
  });
}

export function usePublicShareQuery(
  token: string,
  options?: Omit<UseQueryOptions<PublicShareOut>, "queryKey" | "queryFn">,
) {
  return useQuery<PublicShareOut>({
    queryKey: qk.publicShare(token),
    queryFn: () => getPublicShare(token),
    enabled: Boolean(token),
    ...options,
  });
}

// ——— Mutations ———

export function useAddAllowedEmailMutation(
  options?: Omit<
    UseMutationOptions<AllowedEmailOut, Error, string>,
    "mutationFn"
  >,
) {
  const qc = useQueryClient();
  return useMutation<AllowedEmailOut, Error, string>({
    mutationFn: (email: string) => addAllowedEmail(email),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.invalidateQueries({ queryKey: qk.allowedEmails() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export function useRemoveAllowedEmailMutation(
  options?: Omit<UseMutationOptions<void, Error, string>, "mutationFn">,
) {
  const qc = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: (id: string) => removeAllowedEmail(id),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.invalidateQueries({ queryKey: qk.allowedEmails() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export interface CreateShareVars {
  imageId: string;
  show_prompt?: boolean;
  expires_at?: string;
}

export interface CreateMultiShareVars {
  imageIds: string[];
  show_prompt?: boolean;
  expires_at?: string;
}

export function useCreateShareMutation(
  options?: Omit<
    UseMutationOptions<ShareOut, Error, CreateShareVars>,
    "mutationFn"
  >,
) {
  const qc = useQueryClient();
  return useMutation<ShareOut, Error, CreateShareVars>({
    mutationFn: (vars) =>
      createShare(vars.imageId, {
        show_prompt: vars.show_prompt,
        expires_at: vars.expires_at,
      }),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.invalidateQueries({ queryKey: qk.myShares() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export function useCreateMultiShareMutation(
  options?: Omit<
    UseMutationOptions<ShareOut, Error, CreateMultiShareVars>,
    "mutationFn"
  >,
) {
  const qc = useQueryClient();
  return useMutation<ShareOut, Error, CreateMultiShareVars>({
    mutationFn: (vars) =>
      createMultiShare(vars.imageIds, {
        show_prompt: vars.show_prompt,
        expires_at: vars.expires_at,
      }),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.invalidateQueries({ queryKey: qk.myShares() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export function useRevokeShareMutation(
  options?: Omit<UseMutationOptions<void, Error, string>, "mutationFn">,
) {
  const qc = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: (shareId: string) => revokeShare(shareId),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.invalidateQueries({ queryKey: qk.myShares() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

// ——————————————————————————————————————————————————————————————
// V1.0 朋友内测：Invite Links / 系统设置 / 会话 / 注销
// ——————————————————————————————————————————————————————————————

// ——— Invite Links ———

export function useInviteLinksQuery(
  options?: Omit<
    UseQueryOptions<{ items: InviteLinkOut[] }>,
    "queryKey" | "queryFn"
  >,
) {
  return useQuery<{ items: InviteLinkOut[] }>({
    queryKey: qk.inviteLinks(),
    queryFn: listInviteLinks,
    ...options,
  });
}

export interface CreateInviteLinkVars {
  email?: string | null;
  expires_in_days?: number;
  role?: "admin" | "member";
}

export function useCreateInviteLinkMutation(
  options?: Omit<
    UseMutationOptions<InviteLinkOut, Error, CreateInviteLinkVars>,
    "mutationFn"
  >,
) {
  const qc = useQueryClient();
  return useMutation<InviteLinkOut, Error, CreateInviteLinkVars>({
    mutationFn: (vars) => createInviteLink(vars),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.invalidateQueries({ queryKey: qk.inviteLinks() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export function useRevokeInviteLinkMutation(
  options?: Omit<UseMutationOptions<void, Error, string>, "mutationFn">,
) {
  const qc = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: (id: string) => revokeInviteLink(id),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.invalidateQueries({ queryKey: qk.inviteLinks() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export function usePublicInviteQuery(
  token: string,
  options?: Omit<
    UseQueryOptions<InviteLinkPublicOut>,
    "queryKey" | "queryFn"
  >,
) {
  return useQuery<InviteLinkPublicOut>({
    queryKey: qk.publicInvite(token),
    queryFn: () => getPublicInvite(token),
    enabled: Boolean(token),
    ...options,
  });
}

// ——— System Settings ———

export function useSystemSettingsQuery(
  options?: Omit<UseQueryOptions<SystemSettingsOut>, "queryKey" | "queryFn">,
) {
  return useQuery<SystemSettingsOut>({
    queryKey: qk.systemSettings(),
    queryFn: getSystemSettings,
    ...options,
  });
}

export function useAdminModelsQuery(
  options?: Omit<UseQueryOptions<AdminModelsOut>, "queryKey" | "queryFn">,
) {
  return useQuery<AdminModelsOut>({
    queryKey: qk.adminModels(),
    queryFn: getAdminModels,
    staleTime: 60_000,
    ...options,
  });
}

export function useUpdateSystemSettingsMutation(
  options?: Omit<
    UseMutationOptions<
      SystemSettingsOut,
      Error,
      { key: string; value: string }[]
    >,
    "mutationFn"
  >,
) {
  const qc = useQueryClient();
  return useMutation<
    SystemSettingsOut,
    Error,
    { key: string; value: string }[]
  >({
    mutationFn: (items) => updateSystemSettings(items),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.invalidateQueries({ queryKey: qk.systemSettings() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

// ——— Admin: Providers ———

export function useProvidersQuery(
  options?: Omit<UseQueryOptions<ProvidersOut>, "queryKey" | "queryFn">,
) {
  return useQuery<ProvidersOut>({
    queryKey: qk.providers(),
    queryFn: getProviders,
    ...options,
  });
}

export function useUpdateProvidersMutation(
  options?: Omit<
    UseMutationOptions<
      ProvidersOut,
      Error,
      ProviderItemIn[] | { items: ProviderItemIn[]; proxies?: ProviderProxyIn[] }
    >,
    "mutationFn"
  >,
) {
  const qc = useQueryClient();
  return useMutation<
    ProvidersOut,
    Error,
    ProviderItemIn[] | { items: ProviderItemIn[]; proxies?: ProviderProxyIn[] }
  >({
    mutationFn: (payload) => updateProviders(payload),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.invalidateQueries({ queryKey: qk.providers() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export function useProbeProvidersMutation(
  options?: Omit<
    UseMutationOptions<ProvidersProbeOut, Error, string[] | undefined>,
    "mutationFn"
  >,
) {
  return useMutation<ProvidersProbeOut, Error, string[] | undefined>({
    mutationFn: (names) => probeProviders(names),
    ...options,
  });
}

export function useProviderStatsQuery(
  options?: Omit<
    UseQueryOptions<import("./types").ProviderStatsOut>,
    "queryKey" | "queryFn"
  >,
) {
  return useQuery<import("./types").ProviderStatsOut>({
    queryKey: qk.providerStats(),
    queryFn: getProviderStats,
    refetchInterval: 30_000,
    ...options,
  });
}

// ——— 代理池 ———

export function useAdminProxiesQuery(
  options?: Omit<UseQueryOptions<ProxyListOut>, "queryKey" | "queryFn">,
) {
  return useQuery<ProxyListOut>({
    queryKey: qk.adminProxies(),
    queryFn: listAdminProxies,
    refetchInterval: 30_000,
    ...options,
  });
}

export function useTestProxyMutation(
  options?: Omit<
    UseMutationOptions<ProxyTestOut, Error, { name: string; target?: string }>,
    "mutationFn"
  >,
) {
  const qc = useQueryClient();
  return useMutation<ProxyTestOut, Error, { name: string; target?: string }>({
    mutationFn: ({ name, target }) => testAdminProxy(name, target),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.adminProxies() });
    },
    ...options,
  });
}

export function useTestAllProxiesMutation(
  options?: Omit<
    UseMutationOptions<ProxyTestOut[], Error, string | undefined>,
    "mutationFn"
  >,
) {
  const qc = useQueryClient();
  return useMutation<ProxyTestOut[], Error, string | undefined>({
    mutationFn: (target) => testAllAdminProxies(target),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.adminProxies() });
    },
    ...options,
  });
}

export function useUpdateAdminProxiesMutation(
  options?: Omit<
    UseMutationOptions<ProxyListOut, Error, ProviderProxyIn[]>,
    "mutationFn"
  >,
) {
  const qc = useQueryClient();
  return useMutation<ProxyListOut, Error, ProviderProxyIn[]>({
    mutationFn: (items) => updateAdminProxies(items),
    onSuccess: () => {
      // proxies 改了之后 ProvidersPanel 的 dropdown 需要刷新
      qc.invalidateQueries({ queryKey: qk.adminProxies() });
      qc.invalidateQueries({ queryKey: qk.providers() });
    },
    ...options,
  });
}

export function useRestartTelegramBotMutation(
  options?: Omit<
    UseMutationOptions<{ ok: boolean; receivers: number }, Error, void>,
    "mutationFn"
  >,
) {
  return useMutation<{ ok: boolean; receivers: number }, Error, void>({
    mutationFn: () => restartTelegramBot(),
    ...options,
  });
}

// ——— Me: Sessions ———

export function useMySessionsQuery(
  options?: Omit<
    UseQueryOptions<{ items: SessionOut[] }>,
    "queryKey" | "queryFn"
  >,
) {
  return useQuery<{ items: SessionOut[] }>({
    queryKey: qk.mySessions(),
    queryFn: listMySessions,
    ...options,
  });
}

export function useRevokeMySessionMutation(
  options?: Omit<UseMutationOptions<void, Error, string>, "mutationFn">,
) {
  const qc = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: (id: string) => revokeMySession(id),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.invalidateQueries({ queryKey: qk.mySessions() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

// ——— Me: Delete account ———

export function useDeleteMyAccountMutation(
  options?: Omit<UseMutationOptions<void, Error, void>, "mutationFn">,
) {
  return useMutation<void, Error, void>({
    mutationFn: () => deleteMyAccount(),
    ...options,
  });
}

// ——————————————————————————————————————————————————————————————
// 核心对话流：conversations / messages hooks（主页 + Sidebar 依赖）
// ——————————————————————————————————————————————————————————————

export function useListConversationsQuery(
  opts?: ListConversationsOpts,
  options?: Omit<
    UseQueryOptions<ConversationListResponse>,
    "queryKey" | "queryFn"
  >,
) {
  return useQuery<ConversationListResponse>({
    queryKey: qk.conversations(opts),
    queryFn: () => listConversations(opts),
    ...options,
  });
}

export function useListConversationsInfiniteQuery(params?: {
  limit?: number;
  q?: string;
}) {
  const limit = params?.limit ?? 30;
  const q = params?.q;
  return useInfiniteQuery<
    ConversationListResponse,
    Error,
    InfiniteData<ConversationListResponse, string | undefined>,
    readonly ["conversations", "infinite", { limit: number; q?: string }],
    string | undefined
  >({
    queryKey: [
      "conversations",
      "infinite",
      { limit, ...(q ? { q } : {}) },
    ] as const,
    queryFn: ({ pageParam }) =>
      listConversations({ limit, q, cursor: pageParam }),
    initialPageParam: undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
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
  const qc = useQueryClient();
  return useMutation<ConversationSummary, Error, CreateConversationIn | void>({
    mutationFn: (body) => createConversation(body ?? {}),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.invalidateQueries({ queryKey: ["conversations"] });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export interface PatchConversationVars extends PatchConversationIn {
  id: string;
}

export function usePatchConversationMutation(
  options?: Omit<
    UseMutationOptions<ConversationSummary, Error, PatchConversationVars>,
    "mutationFn"
  >,
) {
  const qc = useQueryClient();
  return useMutation<ConversationSummary, Error, PatchConversationVars>({
    mutationFn: ({ id, ...rest }) => patchConversation(id, rest),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      // 仅 invalidate 列表页（含 infinite）和该单条 detail；避免冲掉 messages / 其它无关 query
      qc.invalidateQueries({ queryKey: ["conversations", "infinite"] });
      qc.invalidateQueries({ queryKey: ["stream", "feed"] });
      qc.invalidateQueries({
        queryKey: ["conversations"],
        exact: false,
        predicate: (q) => {
          const key = q.queryKey;
          // qk.conversations(opts) → ["conversations", opts]
          return Array.isArray(key) && key[0] === "conversations" && key[1] !== "infinite";
        },
      });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export function useDeleteConversationMutation(
  options?: Omit<UseMutationOptions<void, Error, string>, "mutationFn">,
) {
  const qc = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: (id) => deleteConversation(id),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.invalidateQueries({ queryKey: ["conversations"] });
      qc.invalidateQueries({ queryKey: ["stream", "feed"] });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

// ——— 历史消息（翻旧消息/初始装载） ———
export function useListMessagesQuery(
  convId: string | null | undefined,
  opts?: ListMessagesOpts,
  options?: Omit<UseQueryOptions<MessageListResponse>, "queryKey" | "queryFn">,
) {
  return useQuery<MessageListResponse>({
    queryKey: qk.messages(convId ?? "", opts),
    queryFn: () => listMessages(convId as string, opts),
    enabled: typeof convId === "string" && convId.length > 0,
    ...options,
  });
}

export function useConversationContextQuery(
  convId: string | null | undefined,
  options?: Omit<UseQueryOptions<ConversationContextStats>, "queryKey" | "queryFn">,
) {
  return useQuery<ConversationContextStats>({
    queryKey: qk.conversationContext(convId ?? ""),
    queryFn: () => getConversationContext(convId as string),
    enabled: typeof convId === "string" && convId.length > 0,
    staleTime: 10_000,
    ...options,
  });
}
