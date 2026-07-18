import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationOptions,
  type UseQueryOptions,
} from "@tanstack/react-query";

import {
  createSystemPrompt,
  deleteSystemPrompt,
  listSystemPrompts,
  patchSystemPrompt,
  setDefaultSystemPrompt,
  type CreateSystemPromptIn,
  type PatchSystemPromptIn,
  type SystemPrompt,
  type SystemPromptListResponse,
} from "../apiClient";
import {
  useUserQueryScope,
  userScopedQueryKey,
} from "@/components/QueryProvider";
import { qk } from "./queryKeys";

function systemPromptsQueryKey(userId: string | null | undefined) {
  return userScopedQueryKey(userId, qk.systemPrompts());
}

function guardSystemPromptMutation<TVariables, TData>(
  identityConfirmed: boolean,
  mutationFn: (variables: TVariables) => Promise<TData>,
): (variables: TVariables) => Promise<TData> {
  if (identityConfirmed) return mutationFn;
  return () =>
    Promise.reject(
      new Error("System prompt mutations require a confirmed user identity"),
    );
}

export function useSystemPromptsQuery(
  options?: Omit<
    UseQueryOptions<SystemPromptListResponse>,
    "queryKey" | "queryFn"
  >,
) {
  const userScope = useUserQueryScope();
  return useQuery<SystemPromptListResponse>({
    ...options,
    queryKey: systemPromptsQueryKey(userScope.userId),
    queryFn: listSystemPrompts,
    enabled: userScope.enabled && (options?.enabled ?? true),
  });
}

export function useCreateSystemPromptMutation(
  options?: Omit<
    UseMutationOptions<SystemPrompt, Error, CreateSystemPromptIn>,
    "mutationFn"
  >,
) {
  const userScope = useUserQueryScope();
  const qc = useQueryClient();
  return useMutation<SystemPrompt, Error, CreateSystemPromptIn>({
    mutationFn: guardSystemPromptMutation(
      userScope.enabled,
      createSystemPrompt,
    ),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.invalidateQueries({
        queryKey: systemPromptsQueryKey(userScope.userId),
      });
      qc.invalidateQueries({ queryKey: ["me"] });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

interface PatchSystemPromptVars extends PatchSystemPromptIn {
  id: string;
}

export function usePatchSystemPromptMutation(
  options?: Omit<
    UseMutationOptions<SystemPrompt, Error, PatchSystemPromptVars>,
    "mutationFn"
  >,
) {
  const userScope = useUserQueryScope();
  const qc = useQueryClient();
  return useMutation<SystemPrompt, Error, PatchSystemPromptVars>({
    mutationFn: guardSystemPromptMutation(userScope.enabled, ({ id, ...body }) =>
      patchSystemPrompt(id, body),
    ),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.invalidateQueries({
        queryKey: systemPromptsQueryKey(userScope.userId),
      });
      qc.invalidateQueries({ queryKey: ["me"] });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export function useDeleteSystemPromptMutation(
  options?: Omit<UseMutationOptions<void, Error, string>, "mutationFn">,
) {
  const userScope = useUserQueryScope();
  const qc = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: guardSystemPromptMutation(
      userScope.enabled,
      deleteSystemPrompt,
    ),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.invalidateQueries({
        queryKey: systemPromptsQueryKey(userScope.userId),
      });
      qc.invalidateQueries({
        queryKey: qk.user(userScope.userId).conversationsAll(),
      });
      qc.invalidateQueries({ queryKey: ["me"] });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export function useSetDefaultSystemPromptMutation(
  options?: Omit<UseMutationOptions<SystemPrompt, Error, string>, "mutationFn">,
) {
  const userScope = useUserQueryScope();
  const qc = useQueryClient();
  return useMutation<SystemPrompt, Error, string>({
    mutationFn: guardSystemPromptMutation(
      userScope.enabled,
      setDefaultSystemPrompt,
    ),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.invalidateQueries({
        queryKey: systemPromptsQueryKey(userScope.userId),
      });
      qc.invalidateQueries({ queryKey: ["me"] });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}
