import {
  useMutation,
  useQuery,
  type UseMutationOptions,
  type UseQueryOptions,
} from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import {
  getWorkflow,
  type WorkflowRun,
} from "../api/workflows";
import {
  approveCopyAnalysis,
  approvePosterMaster,
  createPosterDesignWorkflow,
  createPosterMasters,
  createPosterRenders,
  inpaintPosterRender,
  revisePosterRender,
  type CopyAnalysisApproveIn,
  type PosterDesignWorkflowCreateIn,
  type PosterDesignWorkflowCreateOut,
  type PosterInpaintIn,
  type PosterMasterApproveIn,
  type PosterMastersCreateIn,
  type PosterRendersCreateIn,
  type PosterReviseIn,
} from "../api/posterWorkflows";
import { qk } from "./queryKeys";
import {
  isUserScopedQueryKeyForUser,
  privateQueryEnabled,
  useCurrentUserQueryClient,
  useCurrentUserQueryKeys,
} from "./privateQueryScope";

import {
  batchDeletePosterStyles,
  deletePosterStyle,
  generatePosterStyle,
  getPosterStyle,
  listPosterStyleJobs,
  listPosterStyles,
  patchPosterStyle,
  syncPosterStylePresets,
  triggerPosterStyleAutoTag,
  type PosterStyleAutoTagOut,
  type PosterStyleBatchDeleteOut,
  type PosterStyleGenerateIn,
  type PosterStyleGenerateOut,
  type PosterStyleItem,
  type PosterStyleJobsOpts,
  type PosterStyleJobsOut,
  type PosterStyleListOpts,
  type PosterStyleListOut,
  type PosterStylePatchIn,
  type PosterStyleSyncOut,
} from "../api/posterStyles";

export function usePosterStylesQuery(
  params: PosterStyleListOpts = {},
  options?: Omit<UseQueryOptions<PosterStyleListOut>, "queryKey" | "queryFn">,
) {
  const { userScope, userKeys } = useCurrentUserQueryKeys();
  return useQuery<PosterStyleListOut>({
    queryKey: userKeys.posterStyles(params),
    queryFn: () => listPosterStyles(params),
    // React Query v5 的 previous data 可能来自上一个用户的 observer。
    placeholderData: (previous, previousQuery) =>
      isUserScopedQueryKeyForUser(
        previousQuery?.queryKey ?? [],
        userScope.userId,
      )
        ? previous
        : undefined,
    staleTime: 15_000,
    ...options,
    enabled: privateQueryEnabled(userScope.enabled, options?.enabled),
  });
}

export function usePosterStyleQuery(
  id: string,
  options?: Omit<UseQueryOptions<PosterStyleItem>, "queryKey" | "queryFn">,
) {
  const { userScope, userKeys } = useCurrentUserQueryKeys();
  return useQuery<PosterStyleItem>({
    queryKey: userKeys.posterStyle(id),
    queryFn: () => getPosterStyle(id),
    staleTime: 30_000,
    ...options,
    enabled: privateQueryEnabled(
      userScope.enabled,
      options?.enabled,
      Boolean(id),
    ),
  });
}

function posterStyleJobsHaveTerminalTransition(
  previous: PosterStyleJobsOut | undefined,
  current: PosterStyleJobsOut | undefined,
): boolean {
  if (!previous || !current) return false;
  const currentStatuses = new Map(
    current.items.map((job) => [job.job_id, job.status] as const),
  );
  return previous.items.some((job) => {
    if (job.status !== "queued" && job.status !== "running") return false;
    const currentStatus = currentStatuses.get(job.job_id);
    return (
      currentStatus === "succeeded" ||
      currentStatus === "failed" ||
      currentStatus === "partial"
    );
  });
}

export function usePosterStyleJobsQuery(
  params: PosterStyleJobsOpts = {},
  options?: Omit<UseQueryOptions<PosterStyleJobsOut>, "queryKey" | "queryFn">,
) {
  const {
    queryClient: qc,
    userScope,
    userKeys,
  } = useCurrentUserQueryClient();
  const previousJobsRef = useRef<PosterStyleJobsOut | undefined>(undefined);
  const previousJobsUserIdRef = useRef(userScope.userId);
  const jobsQuery = useQuery<PosterStyleJobsOut>({
    queryKey: userKeys.posterStyleJobs(params),
    queryFn: () => listPosterStyleJobs(params),
    // 智能轮询：jobs 列表里只要有 running/queued 就 5s 刷一次，否则 30s
    refetchInterval: (query) => {
      const data = query.state.data as PosterStyleJobsOut | undefined;
      const hasRunning =
        data?.items?.some(
          (job) => job.status === "queued" || job.status === "running",
        ) ?? false;
      return hasRunning ? 5_000 : 30_000;
    },
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: true,
    staleTime: 2_000,
    ...options,
    enabled: privateQueryEnabled(userScope.enabled, options?.enabled),
  });

  useEffect(() => {
    if (previousJobsUserIdRef.current === userScope.userId) return;
    previousJobsUserIdRef.current = userScope.userId;
    previousJobsRef.current = undefined;
  }, [userScope.userId]);

  useEffect(() => {
    const currentJobs = jobsQuery.data;
    if (!currentJobs) return;
    const previousJobs = previousJobsRef.current;
    previousJobsRef.current = currentJobs;
    if (!posterStyleJobsHaveTerminalTransition(previousJobs, currentJobs)) return;

    // A terminal jobs snapshot is only visible after the worker transaction commits.
    // Updating the ref first makes repeated terminal polls a no-op.
    const scopedKeys = qk.user(userScope.userId);
    void Promise.all([
      qc.invalidateQueries({ queryKey: scopedKeys.posterStyleLists() }),
      qc.invalidateQueries({ queryKey: scopedKeys.posterStyleDetails() }),
    ]);
  }, [jobsQuery.data, qc, userScope.userId]);

  return jobsQuery;
}

export type PosterStyleJobsQueryResult = ReturnType<
  typeof usePosterStyleJobsQuery
>;

export function usePatchPosterStyleMutation(
  options?: Omit<
    UseMutationOptions<
      PosterStyleItem,
      Error,
      { id: string; body: PosterStylePatchIn }
    >,
    "mutationFn"
  >,
) {
  const { queryClient: qc, userKeys } = useCurrentUserQueryClient();
  return useMutation<
    PosterStyleItem,
    Error,
    { id: string; body: PosterStylePatchIn }
  >({
    mutationFn: ({ id, body }) => patchPosterStyle(id, body),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.setQueryData(userKeys.posterStyle(vars.id), data);
      qc.invalidateQueries({ queryKey: userKeys.posterStylesAll() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export function useDeletePosterStyleMutation(
  options?: Omit<UseMutationOptions<{ ok: boolean }, Error, string>, "mutationFn">,
) {
  const { queryClient: qc, userKeys } = useCurrentUserQueryClient();
  return useMutation<{ ok: boolean }, Error, string>({
    mutationFn: deletePosterStyle,
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.invalidateQueries({ queryKey: userKeys.posterStylesAll() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export function useBatchDeletePosterStylesMutation(
  options?: Omit<
    UseMutationOptions<PosterStyleBatchDeleteOut, Error, string[]>,
    "mutationFn"
  >,
) {
  const { queryClient: qc, userKeys } = useCurrentUserQueryClient();
  return useMutation<PosterStyleBatchDeleteOut, Error, string[]>({
    mutationFn: batchDeletePosterStyles,
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.invalidateQueries({ queryKey: userKeys.posterStylesAll() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export function useSyncPosterStylePresetsMutation(
  options?: Omit<UseMutationOptions<PosterStyleSyncOut, Error, void>, "mutationFn">,
) {
  const { queryClient: qc, userKeys } = useCurrentUserQueryClient();
  return useMutation<PosterStyleSyncOut, Error, void>({
    mutationFn: () => syncPosterStylePresets(),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.invalidateQueries({ queryKey: userKeys.posterStylesAll() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export function useGeneratePosterStyleMutation(
  options?: Omit<
    UseMutationOptions<PosterStyleGenerateOut, Error, PosterStyleGenerateIn>,
    "mutationFn"
  >,
) {
  const { queryClient: qc, userKeys } = useCurrentUserQueryClient();
  return useMutation<PosterStyleGenerateOut, Error, PosterStyleGenerateIn>({
    mutationFn: generatePosterStyle,
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.invalidateQueries({ queryKey: userKeys.posterStyleJobs() });
      qc.invalidateQueries({ queryKey: userKeys.posterStyleLists() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export function useTriggerPosterStyleAutoTagMutation(
  itemId: string,
  options?: Omit<UseMutationOptions<PosterStyleAutoTagOut, Error, void>, "mutationFn">,
) {
  const { queryClient: qc, userKeys } = useCurrentUserQueryClient();
  return useMutation<PosterStyleAutoTagOut, Error, void>({
    mutationFn: () => triggerPosterStyleAutoTag(itemId),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.invalidateQueries({ queryKey: userKeys.posterStyle(itemId) });
      qc.invalidateQueries({ queryKey: userKeys.posterStyleLists() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

// ============================================================================
// Poster Design Workflow hooks
// 与 useWorkflowQuery 共用当前用户的 workflow key，只是聚合层用 alias 暴露给海报 detail 页。
// ============================================================================

// 智能轮询：running 5s / needs_review 30s。逻辑等同 useWorkflowQuery。
export function usePosterWorkflowQuery(
  id: string | null | undefined,
  options?: Omit<UseQueryOptions<WorkflowRun>, "queryKey" | "queryFn">,
) {
  const { userScope, userKeys } = useCurrentUserQueryKeys();
  return useQuery<WorkflowRun>({
    queryKey: userKeys.workflow(id ?? ""),
    queryFn: () => getWorkflow(id as string),
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      if (status === "running") return 5_000;
      if (status === "needs_review") return 30_000;
      return false;
    },
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: true,
    ...options,
    enabled: privateQueryEnabled(
      userScope.enabled,
      options?.enabled,
      typeof id === "string" && id.length > 0,
    ),
  });
}

export function useCreatePosterDesignWorkflowMutation(
  options?: Omit<
    UseMutationOptions<
      PosterDesignWorkflowCreateOut,
      Error,
      PosterDesignWorkflowCreateIn
    >,
    "mutationFn"
  >,
) {
  const { queryClient: qc, userKeys } = useCurrentUserQueryClient();
  return useMutation<
    PosterDesignWorkflowCreateOut,
    Error,
    PosterDesignWorkflowCreateIn
  >({
    mutationFn: createPosterDesignWorkflow,
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.invalidateQueries({ queryKey: userKeys.workflowsAll() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export function useApproveCopyAnalysisMutation(
  workflowId: string,
  options?: Omit<
    UseMutationOptions<WorkflowRun, Error, CopyAnalysisApproveIn>,
    "mutationFn"
  >,
) {
  const { queryClient: qc, userKeys } = useCurrentUserQueryClient();
  return useMutation<WorkflowRun, Error, CopyAnalysisApproveIn>({
    mutationFn: (body) => approveCopyAnalysis(workflowId, body),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.setQueryData(userKeys.workflow(workflowId), data);
      qc.invalidateQueries({ queryKey: userKeys.workflowsAll() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export function useCreatePosterMastersMutation(
  workflowId: string,
  options?: Omit<
    UseMutationOptions<WorkflowRun, Error, PosterMastersCreateIn | void>,
    "mutationFn"
  >,
) {
  const { queryClient: qc, userKeys } = useCurrentUserQueryClient();
  return useMutation<WorkflowRun, Error, PosterMastersCreateIn | void>({
    mutationFn: (body) => createPosterMasters(workflowId, body ?? {}),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.setQueryData(userKeys.workflow(workflowId), data);
      qc.invalidateQueries({ queryKey: userKeys.workflowsAll() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export function useApprovePosterMasterMutation(
  workflowId: string,
  options?: Omit<
    UseMutationOptions<
      WorkflowRun,
      Error,
      PosterMasterApproveIn & { master_id: string }
    >,
    "mutationFn"
  >,
) {
  const { queryClient: qc, userKeys } = useCurrentUserQueryClient();
  return useMutation<
    WorkflowRun,
    Error,
    PosterMasterApproveIn & { master_id: string }
  >({
    mutationFn: ({ master_id, ...body }) =>
      approvePosterMaster(workflowId, master_id, body),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.setQueryData(userKeys.workflow(workflowId), data);
      qc.invalidateQueries({ queryKey: userKeys.workflowsAll() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export function useCreatePosterRendersMutation(
  workflowId: string,
  options?: Omit<
    UseMutationOptions<WorkflowRun, Error, PosterRendersCreateIn>,
    "mutationFn"
  >,
) {
  const { queryClient: qc, userKeys } = useCurrentUserQueryClient();
  return useMutation<WorkflowRun, Error, PosterRendersCreateIn>({
    mutationFn: (body) => createPosterRenders(workflowId, body),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.setQueryData(userKeys.workflow(workflowId), data);
      qc.invalidateQueries({ queryKey: userKeys.workflowsAll() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export function useRevisePosterRenderMutation(
  workflowId: string,
  options?: Omit<
    UseMutationOptions<
      WorkflowRun,
      Error,
      PosterReviseIn & { render_id: string }
    >,
    "mutationFn"
  >,
) {
  const { queryClient: qc, userKeys } = useCurrentUserQueryClient();
  return useMutation<
    WorkflowRun,
    Error,
    PosterReviseIn & { render_id: string }
  >({
    mutationFn: ({ render_id, ...body }) =>
      revisePosterRender(workflowId, render_id, body),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.setQueryData(userKeys.workflow(workflowId), data);
      qc.invalidateQueries({ queryKey: userKeys.workflowsAll() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export function useInpaintPosterRenderMutation(
  workflowId: string,
  options?: Omit<
    UseMutationOptions<
      WorkflowRun,
      Error,
      PosterInpaintIn & { render_id: string }
    >,
    "mutationFn"
  >,
) {
  const { queryClient: qc, userKeys } = useCurrentUserQueryClient();
  return useMutation<
    WorkflowRun,
    Error,
    PosterInpaintIn & { render_id: string }
  >({
    mutationFn: ({ render_id, ...body }) =>
      inpaintPosterRender(workflowId, render_id, body),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.setQueryData(userKeys.workflow(workflowId), data);
      qc.invalidateQueries({ queryKey: userKeys.workflowsAll() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}
