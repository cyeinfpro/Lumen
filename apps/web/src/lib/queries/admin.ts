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
  createInviteLink,
  createMultiShare,
  createShare,
  deleteAdminUser,
  getPublicInvite,
  getAdminUserHistory,
  listAdminRequestEvents,
  listAdminUsers,
  listAllowedEmails,
  listInviteLinks,
  removeAllowedEmail,
  revokeInviteLink,
  setAdminUserPassword,
  getAdminUpdateStatus,
  getAdminUpdateVersion,
  checkAdminUpdate,
  listAdminProxies,
  listAdminReleases,
  restartTelegramBot,
  rollbackAdminRelease,
  rollbackPreviousAdminRelease,
  testAdminProxy,
  testAllAdminProxies,
  triggerAdminUpdate,
  updateAdminProxies,
} from "../api/admin";
import {
  getAdminModels,
  getProviders,
  getProviderStats,
  getSystemSettings,
  getVideoProviders,
  patchProviderEnabled,
  probeProviders,
  updateProviders,
  updateSystemSettings,
  updateVideoProviders,
} from "../api/system";
import {
  deleteMyAccount,
  listMySessions,
  revokeMySession,
} from "../api/account";
import type {
  AdminUserOut,
  AdminUserHistoryOut,
  AdminRequestEventsOut,
  AdminModelsOut,
  AllowedEmailOut,
  InviteLinkOut,
  InviteLinkPublicOut,
  ProviderItemIn,
  ProviderItemOut,
  ProviderProxyIn,
  ProvidersOut,
  ProvidersProbeOut,
  ProxyListOut,
  ProxyTestOut,
  SessionOut,
  ShareOut,
  SystemSettingsOut,
  VideoProvidersOut,
  VideoProvidersUpdateIn,
} from "../types";
import {
  getAdminStorage,
  testAdminStorage,
  putAdminStorage,
  type StorageApplyResponseOut,
  type StorageConfigOut,
  type StorageConfigUpdateIn,
  type StorageTestIn,
  type StorageTestResultOut,
} from "../api/storage";
import { qk } from "./queryKeys";
import {
  privateQueryEnabled,
  useCurrentUserQueryKeys,
} from "./privateQueryScope";

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

export function useAdminUserHistoryQuery(
  userId: string | null,
  options?: Omit<UseQueryOptions<AdminUserHistoryOut>, "queryKey" | "queryFn">,
) {
  return useQuery<AdminUserHistoryOut>({
    queryKey: qk.adminUserHistory(userId ?? ""),
    queryFn: () => getAdminUserHistory(userId ?? "", { limit: 80 }),
    enabled: Boolean(userId) && (options?.enabled ?? true),
    ...options,
  });
}

export function useSetAdminUserPasswordMutation(
  options?: Omit<
    UseMutationOptions<{ ok: boolean }, Error, { userId: string; password: string }>,
    "mutationFn"
  >,
) {
  const qc = useQueryClient();
  return useMutation<{ ok: boolean }, Error, { userId: string; password: string }>({
    mutationFn: ({ userId, password }) => setAdminUserPassword(userId, password),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.invalidateQueries({ queryKey: ["admin", "users"] });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export function useDeleteAdminUserMutation(
  options?: Omit<UseMutationOptions<{ ok: boolean }, Error, string>, "mutationFn">,
) {
  const qc = useQueryClient();
  return useMutation<{ ok: boolean }, Error, string>({
    mutationFn: deleteAdminUser,
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.invalidateQueries({ queryKey: ["admin", "users"] });
      qc.removeQueries({ queryKey: qk.adminUserHistory(vars) });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
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

interface CreateShareVars {
  imageId: string;
  show_prompt?: boolean;
  expires_at?: string;
}

interface CreateMultiShareVars {
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
  const { userKeys } = useCurrentUserQueryKeys();
  const qc = useQueryClient();
  return useMutation<ShareOut, Error, CreateShareVars>({
    mutationFn: (vars) =>
      createShare(vars.imageId, {
        show_prompt: vars.show_prompt,
        expires_at: vars.expires_at,
      }),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.invalidateQueries({ queryKey: userKeys.myShares() });
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
  const { userKeys } = useCurrentUserQueryKeys();
  const qc = useQueryClient();
  return useMutation<ShareOut, Error, CreateMultiShareVars>({
    mutationFn: (vars) =>
      createMultiShare(vars.imageIds, {
        show_prompt: vars.show_prompt,
        expires_at: vars.expires_at,
      }),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.invalidateQueries({ queryKey: userKeys.myShares() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

// ——————————————————————————————————————————————————————————————
// Invite Links / 系统设置 / 会话 / 注销
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

interface CreateInviteLinkVars {
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
      qc.invalidateQueries({ queryKey: ["me"] });
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
export function useVideoProvidersQuery(
  options?: Omit<UseQueryOptions<VideoProvidersOut>, "queryKey" | "queryFn">,
) {
  return useQuery<VideoProvidersOut>({
    queryKey: qk.videoProviders(),
    queryFn: getVideoProviders,
    ...options,
  });
}

export function useUpdateVideoProvidersMutation(
  options?: Omit<
    UseMutationOptions<VideoProvidersOut, Error, VideoProvidersUpdateIn>,
    "mutationFn"
  >,
) {
  const qc = useQueryClient();
  return useMutation<VideoProvidersOut, Error, VideoProvidersUpdateIn>({
    mutationFn: (payload) => updateVideoProviders(payload),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.setQueryData(qk.videoProviders(), data);
      qc.invalidateQueries({ queryKey: qk.videoProviders() });
      qc.invalidateQueries({ queryKey: qk.systemSettings() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export function usePatchProviderEnabledMutation(
  options?: Omit<
    UseMutationOptions<ProviderItemOut, Error, { name: string; enabled: boolean }>,
    "mutationFn"
  >,
) {
  const qc = useQueryClient();
  return useMutation<ProviderItemOut, Error, { name: string; enabled: boolean }>({
    mutationFn: ({ name, enabled }) => patchProviderEnabled(name, enabled),
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
    UseQueryOptions<import("../types").ProviderStatsOut>,
    "queryKey" | "queryFn"
  >,
) {
  return useQuery<import("../types").ProviderStatsOut>({
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
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.invalidateQueries({ queryKey: qk.adminProxies() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
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
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.invalidateQueries({ queryKey: qk.adminProxies() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
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
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      // proxies 改了之后 ProvidersPanel 的 dropdown 需要刷新
      qc.invalidateQueries({ queryKey: qk.adminProxies() });
      qc.invalidateQueries({ queryKey: qk.providers() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
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

export function useAdminUpdateStatusQuery(
  options?: Omit<
    UseQueryOptions<import("../apiClient").AdminUpdateStatusOut>,
    "queryKey" | "queryFn"
  >,
) {
  return useQuery<import("../apiClient").AdminUpdateStatusOut>({
    queryKey: qk.adminUpdateStatus(),
    queryFn: getAdminUpdateStatus,
    refetchInterval: (query) => (query.state.data?.running ? 5000 : false),
    ...options,
  });
}

export function useAdminUpdateVersionQuery(
  options?: Omit<
    UseQueryOptions<import("../apiClient").AdminUpdateVersionOut>,
    "queryKey" | "queryFn"
  >,
) {
  return useQuery<import("../apiClient").AdminUpdateVersionOut>({
    queryKey: qk.adminUpdateVersion(),
    queryFn: getAdminUpdateVersion,
    ...options,
  });
}

export function useAdminCheckUpdateQuery(
  force = false,
  options?: Omit<
    UseQueryOptions<import("../apiClient").AdminUpdateCheckOut>,
    "queryKey" | "queryFn"
  >,
) {
  return useQuery<import("../apiClient").AdminUpdateCheckOut>({
    queryKey: qk.adminUpdateCheck(force),
    queryFn: () => checkAdminUpdate(force),
    ...options,
  });
}

export function useTriggerAdminUpdateMutation(
  options?: Omit<
    UseMutationOptions<
      import("../apiClient").AdminUpdateTriggerOut,
      Error,
      import("../apiClient").AdminUpdateTriggerIn | void
    >,
    "mutationFn"
  >,
) {
  const qc = useQueryClient();
  return useMutation<
    import("../apiClient").AdminUpdateTriggerOut,
    Error,
    import("../apiClient").AdminUpdateTriggerIn | void
  >({
    mutationFn: (body) => triggerAdminUpdate(body ?? {}),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.invalidateQueries({ queryKey: qk.adminUpdateStatus() });
      qc.invalidateQueries({ queryKey: qk.adminUpdateVersion() });
      qc.invalidateQueries({ queryKey: qk.adminReleases() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

// Release 列表（top 10）。和 update status 拆分以便单独刷新。
// 不主动轮询：依赖 trigger / rollback / SSE done 后 invalidate。
export function useAdminReleasesQuery(
  options?: Omit<
    UseQueryOptions<import("../apiClient").ReleaseInfo[]>,
    "queryKey" | "queryFn"
  >,
) {
  return useQuery<import("../apiClient").ReleaseInfo[]>({
    queryKey: qk.adminReleases(),
    queryFn: listAdminReleases,
    ...options,
  });
}

export function useRollbackReleaseMutation(
  options?: Omit<
    UseMutationOptions<import("../apiClient").AdminRollbackOut, Error, string>,
    "mutationFn"
  >,
) {
  const qc = useQueryClient();
  return useMutation<import("../apiClient").AdminRollbackOut, Error, string>({
    mutationFn: (release_id: string) => rollbackAdminRelease(release_id),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      // 回滚也是后台异步任务，立即把 status / releases 标 stale，让 SSE / 下次 fetch 反映
      qc.invalidateQueries({ queryKey: qk.adminUpdateStatus() });
      qc.invalidateQueries({ queryKey: qk.adminReleases() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export function useRollbackPreviousMutation(
  options?: Omit<
    UseMutationOptions<import("../apiClient").AdminRollbackOut, Error, void>,
    "mutationFn"
  >,
) {
  const qc = useQueryClient();
  return useMutation<import("../apiClient").AdminRollbackOut, Error, void>({
    mutationFn: () => rollbackPreviousAdminRelease(),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.invalidateQueries({ queryKey: qk.adminUpdateStatus() });
      qc.invalidateQueries({ queryKey: qk.adminUpdateVersion() });
      qc.invalidateQueries({ queryKey: qk.adminReleases() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

// ——— Me: Sessions ———

export function useMySessionsQuery(
  options?: Omit<
    UseQueryOptions<{ items: SessionOut[] }>,
    "queryKey" | "queryFn"
  >,
) {
  const { userScope, userKeys } = useCurrentUserQueryKeys();
  return useQuery<{ items: SessionOut[] }>({
    queryKey: userKeys.mySessions(),
    queryFn: listMySessions,
    ...options,
    enabled: privateQueryEnabled(userScope.enabled, options?.enabled),
  });
}

export function useRevokeMySessionMutation(
  options?: Omit<UseMutationOptions<void, Error, string>, "mutationFn">,
) {
  const { userKeys } = useCurrentUserQueryKeys();
  const qc = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: (id: string) => revokeMySession(id),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.invalidateQueries({ queryKey: userKeys.mySessions() });
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

// ——— Admin: Storage backend (local / SMB) ———

export function useAdminStorageQuery(
  options?: Omit<UseQueryOptions<StorageConfigOut>, "queryKey" | "queryFn">,
) {
  return useQuery<StorageConfigOut>({
    queryKey: qk.adminStorage(),
    queryFn: getAdminStorage,
    ...options,
  });
}

export function useTestAdminStorageMutation(
  options?: Omit<
    UseMutationOptions<StorageTestResultOut, Error, StorageTestIn>,
    "mutationFn"
  >,
) {
  return useMutation<StorageTestResultOut, Error, StorageTestIn>({
    mutationFn: (body) => testAdminStorage(body),
    ...options,
  });
}

// 注意：PUT 后 lumen-api 会被 docker stop ~10–30s，这段时间任何 fetch 都会失败。
// 所以这里 onSuccess 不主动 invalidate（refetch 一定 throw），由组件层在 polling
// 完成后自行 setQueryData / invalidate。
export function usePutAdminStorageMutation(
  options?: Omit<
    UseMutationOptions<StorageApplyResponseOut, Error, StorageConfigUpdateIn>,
    "mutationFn"
  >,
) {
  return useMutation<StorageApplyResponseOut, Error, StorageConfigUpdateIn>({
    mutationFn: (body) => putAdminStorage(body),
    ...options,
  });
}
