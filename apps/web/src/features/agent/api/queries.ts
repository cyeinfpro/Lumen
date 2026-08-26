"use client";

import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
  type InfiniteData,
  type UseQueryOptions,
} from "@tanstack/react-query";
import { useUserQueryScope } from "@/lib/queries/userScope";
import { qk } from "@/lib/queries/queryKeys";
import type {
  AgentMessageList,
  AgentRun,
  AgentSession,
  AgentSessionImageList,
  AgentSessionCreateInput,
  AgentSessionList,
  AgentSessionPatchInput,
  AgentStatus,
} from "../model/contracts";
import {
  cancelAgentRun,
  continueAgentRun,
  createAgentSession,
  deleteAgentSession,
  ejectAgentSessionImage,
  getAgentActiveRun,
  getAgentSession,
  getAgentStatus,
  listAgentMessages,
  listAgentSessionImages,
  listAgentSessions,
  patchAgentSession,
} from "./agentApi";

export function useAgentStatusQuery(
  enabled: boolean,
  options?: Omit<UseQueryOptions<AgentStatus>, "queryKey" | "queryFn">,
) {
  const userScope = useUserQueryScope();
  return useQuery<AgentStatus>({
    queryKey: qk.user(userScope.userId).agentStatus(),
    queryFn: ({ signal }) => getAgentStatus(signal),
    retry: false,
    staleTime: 10_000,
    ...options,
    enabled: userScope.enabled && enabled && (options?.enabled ?? true),
  });
}

export function useAgentSessionsQuery(params: { q?: string; limit?: number } = {}) {
  const userScope = useUserQueryScope();
  const limit = params.limit ?? 30;
  const q = params.q?.trim() || undefined;
  return useInfiniteQuery<
    AgentSessionList,
    Error,
    InfiniteData<AgentSessionList, string | undefined>,
    ReturnType<ReturnType<typeof qk.user>["agentSessions"]>,
    string | undefined
  >({
    queryKey: qk.user(userScope.userId).agentSessions({ limit, ...(q ? { q } : {}) }),
    queryFn: ({ pageParam, signal }) =>
      listAgentSessions({ cursor: pageParam, limit, q, signal }),
    initialPageParam: undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: userScope.enabled,
    staleTime: 10_000,
  });
}

export function useAgentSessionQuery(sessionId: string | null) {
  const userScope = useUserQueryScope();
  return useQuery<AgentSession>({
    queryKey: qk.user(userScope.userId).agentSession(sessionId ?? ""),
    queryFn: ({ signal }) => getAgentSession(sessionId as string, signal),
    enabled: userScope.enabled && Boolean(sessionId),
    staleTime: 5_000,
  });
}

export function useAgentMessagesQuery(sessionId: string | null) {
  const userScope = useUserQueryScope();
  return useInfiniteQuery<
    AgentMessageList,
    Error,
    InfiniteData<AgentMessageList, string | undefined>,
    ReturnType<ReturnType<typeof qk.user>["agentMessages"]>,
    string | undefined
  >({
    queryKey: qk.user(userScope.userId).agentMessages(sessionId ?? ""),
    queryFn: ({ pageParam, signal }) =>
      listAgentMessages(sessionId as string, {
        cursor: pageParam,
        limit: 100,
        includeTasks: true,
        signal,
      }),
    initialPageParam: undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: userScope.enabled && Boolean(sessionId),
    staleTime: 1_000,
    refetchOnWindowFocus: true,
  });
}

export function useAgentActiveRunQuery(
  sessionId: string | null,
  enabled = true,
) {
  const userScope = useUserQueryScope();
  return useQuery<AgentRun | null>({
    queryKey: qk.user(userScope.userId).agentActiveRun(sessionId ?? ""),
    queryFn: ({ signal }) => getAgentActiveRun(sessionId as string, signal),
    enabled: userScope.enabled && enabled && Boolean(sessionId),
    staleTime: 1_000,
  });
}

export function useAgentSessionImagesQuery(sessionId: string | null) {
  const userScope = useUserQueryScope();
  return useQuery<AgentSessionImageList>({
    queryKey: qk.user(userScope.userId).agentSessionImages(sessionId ?? ""),
    queryFn: ({ signal }) => listAgentSessionImages(sessionId as string, signal),
    enabled: userScope.enabled && Boolean(sessionId),
    staleTime: 5_000,
  });
}

function useAgentInvalidation() {
  const userScope = useUserQueryScope();
  const queryClient = useQueryClient();
  const keys = qk.user(userScope.userId);
  return { queryClient, keys };
}

export function useCreateAgentSessionMutation() {
  const { queryClient, keys } = useAgentInvalidation();
  return useMutation<AgentSession, Error, AgentSessionCreateInput | void>({
    mutationFn: (body) => createAgentSession(body ?? {}),
    onSuccess: (session) => {
      queryClient.setQueryData(keys.agentSession(session.id), session);
      void queryClient.invalidateQueries({ queryKey: keys.agentSessionsAll() });
    },
  });
}

export function usePatchAgentSessionMutation() {
  const { queryClient, keys } = useAgentInvalidation();
  return useMutation<
    AgentSession,
    Error,
    { sessionId: string; patch: AgentSessionPatchInput }
  >({
    mutationFn: ({ sessionId, patch }) => patchAgentSession(sessionId, patch),
    onSuccess: (session) => {
      queryClient.setQueryData(keys.agentSession(session.id), session);
      void queryClient.invalidateQueries({ queryKey: keys.agentSessionsAll() });
    },
  });
}

export function useDeleteAgentSessionMutation() {
  const { queryClient, keys } = useAgentInvalidation();
  return useMutation<void, Error, string>({
    mutationFn: async (sessionId) => {
      await deleteAgentSession(sessionId);
    },
    onSuccess: (_data, sessionId) => {
      queryClient.removeQueries({ queryKey: keys.agentSession(sessionId) });
      queryClient.removeQueries({ queryKey: keys.agentMessages(sessionId) });
      void queryClient.invalidateQueries({ queryKey: keys.agentSessionsAll() });
    },
  });
}

export function useCancelAgentRunMutation() {
  const { queryClient, keys } = useAgentInvalidation();
  return useMutation<AgentRun, Error, string>({
    mutationFn: cancelAgentRun,
    onSuccess: (run) => {
      queryClient.setQueryData(keys.agentRun(run.id), run);
      queryClient.setQueryData(keys.agentActiveRun(run.agent_session_id), null);
      void queryClient.invalidateQueries({
        queryKey: keys.agentMessages(run.agent_session_id),
      });
    },
  });
}

export function useContinueAgentRunMutation() {
  const { queryClient, keys } = useAgentInvalidation();
  return useMutation<AgentRun, Error, { runId: string; idempotencyKey: string }>({
    mutationFn: ({ runId, idempotencyKey }) =>
      continueAgentRun(runId, idempotencyKey),
    onSuccess: (run) => {
      queryClient.setQueryData(keys.agentRun(run.id), run);
      queryClient.setQueryData(keys.agentActiveRun(run.agent_session_id), run);
      void queryClient.invalidateQueries({
        queryKey: keys.agentMessages(run.agent_session_id),
      });
    },
  });
}

export function useEjectAgentSessionImageMutation() {
  const { queryClient, keys } = useAgentInvalidation();
  return useMutation<
    AgentSessionImageList,
    Error,
    { sessionId: string; imageId: string }
  >({
    mutationFn: ({ sessionId, imageId }) =>
      ejectAgentSessionImage(sessionId, imageId),
    onSuccess: (catalog, variables) => {
      queryClient.setQueryData(
        keys.agentSessionImages(variables.sessionId),
        catalog,
      );
    },
  });
}
