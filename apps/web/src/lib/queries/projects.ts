import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  type InfiniteData,
  type UseMutationOptions,
  type UseQueryOptions,
} from "@tanstack/react-query";
import {
  autoTagApparelModelLibraryItem,
  clearApparelModelLibraryJobs,
  createApparelModelLibraryItem,
  deleteApparelModelLibraryItems,
  deleteApparelModelLibraryJob,
  deleteApparelModelLibraryItem,
  deleteWorkflow,
  generateApparelModelLibrary,
  getApparelModelLibraryJobs,
  getWorkflow,
  listApparelModelLibrary,
  listWorkflows,
  patchWorkflow,
  approveModelCandidate,
  approveProductAnalysis,
  completeWorkflowDelivery,
  createAccessoryPreviews,
  createApparelWorkflow,
  createModelCandidates,
  createShowcaseImages,
  saveAccessorySelection,
  saveApparelModelLibraryJobItem,
  saveModelCandidateToLibrary,
  selectApparelModelLibraryItem,
  reopenModelSelection,
  reviseWorkflowImage,
  syncApparelModelLibraryPresets,
  type ApparelModelLibraryAutoTagOut,
  type ApparelModelLibraryBatchDeleteOut,
  type ApparelModelLibraryGenerateIn,
  type ApparelModelLibraryItem,
  type ApparelModelLibraryItemCreateIn,
  type ApparelModelLibraryJob,
  type ApparelModelLibraryJobsOpts,
  type ApparelModelLibraryJobsList,
  type ApparelModelLibraryListResponse,
  type ApparelModelLibrarySaveJobItemIn,
  type ApparelModelLibrarySelectIn,
  type ApproveModelCandidateIn,
  type AccessoryPreviewIn,
  type AccessorySelectionIn,
  type ModelCandidateSaveToLibraryIn,
  type CreateApparelWorkflowIn,
  type CreateApparelWorkflowOut,
  type CreateShowcaseImagesIn,
  type PatchWorkflowIn,
  type ModelCandidatesIn,
  type ModelLibraryAgeSegment,
  type ModelLibraryAppearance,
  type ModelLibrarySource,
  type ReviseWorkflowImageIn,
  type WorkflowRun,
  type WorkflowRunListResponse,
} from "../api/workflows";
import {
  approveStoryboardAsset,
  approveStoryboardKeyframe,
  approveStoryboardShot,
  assembleStoryboard,
  createStoryboard,
  createStoryboardAsset,
  createStoryboardShot,
  deleteStoryboardAsset,
  deleteStoryboardShot,
  generateAllStoryboardKeyframes,
  generateStoryboardAsset,
  generateStoryboardKeyframe,
  getStoryboard,
  listStoryboards,
  moveStoryboardShot,
  patchStoryboard,
  patchStoryboardShot,
  rebuildStoryboardShots,
  submitAllStoryboardShots,
  submitStoryboardShot,
  type StoryboardAssetCreateIn,
  type StoryboardCreateIn,
  type StoryboardGenerateIn,
  type StoryboardListResponse,
  type StoryboardPatchIn,
  type StoryboardRun,
  type StoryboardShotCreateIn,
  type StoryboardShotPatchIn,
  type StoryboardSubmitShotIn,
} from "../api/storyboards";
import { uploadImage, type UploadedImage } from "../api/images";
import {
  privateQueryEnabled,
  useCurrentUserQueryClient,
  useCurrentUserQueryKeys,
} from "./privateQueryScope";

export function useWorkflowsQuery(
  params: { type?: string; limit?: number } = {},
  options?: Omit<UseQueryOptions<WorkflowRunListResponse>, "queryKey" | "queryFn">,
) {
  const { userScope, userKeys } = useCurrentUserQueryKeys();
  return useQuery<WorkflowRunListResponse>({
    queryKey: userKeys.workflows(params),
    queryFn: () => listWorkflows(params),
    staleTime: 10_000,
    // 列表里有运行中项目时 30s 兜底刷新；否则不轮询（focus 时仍会刷）
    refetchInterval: (query) => {
      const items = query.state.data?.items ?? [];
      return items.some((item) => item.status === "running") ? 30_000 : false;
    },
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: true,
    ...options,
    enabled: privateQueryEnabled(userScope.enabled, options?.enabled),
  });
}

export function useWorkflowQuery(
  id: string | null | undefined,
  options?: Omit<UseQueryOptions<WorkflowRun>, "queryKey" | "queryFn">,
) {
  const { userScope, userKeys } = useCurrentUserQueryKeys();
  return useQuery<WorkflowRun>({
    queryKey: userKeys.workflow(id ?? ""),
    queryFn: () => getWorkflow(id as string),
    // running 5s、needs_review 30s 兜底（避免外部状态翻面后用户感知延迟）；其余不轮询
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

export function useCreateApparelWorkflowMutation(
  options?: Omit<
    UseMutationOptions<CreateApparelWorkflowOut, Error, CreateApparelWorkflowIn>,
    "mutationFn"
  >,
) {
  const { queryClient: qc, userKeys } = useCurrentUserQueryClient();
  return useMutation<CreateApparelWorkflowOut, Error, CreateApparelWorkflowIn>({
    mutationFn: createApparelWorkflow,
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.invalidateQueries({ queryKey: userKeys.workflowsAll() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

interface PatchWorkflowVars extends PatchWorkflowIn {
  id: string;
}

export function usePatchWorkflowMutation(
  options?: Omit<UseMutationOptions<WorkflowRun, Error, PatchWorkflowVars>, "mutationFn">,
) {
  const { queryClient: qc, userKeys } = useCurrentUserQueryClient();
  return useMutation<WorkflowRun, Error, PatchWorkflowVars>({
    mutationFn: ({ id, ...body }) => patchWorkflow(id, body),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.setQueryData(userKeys.workflow(data.id), data);
      qc.invalidateQueries({ queryKey: userKeys.workflowsAll() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export function useDeleteWorkflowMutation(
  options?: Omit<UseMutationOptions<{ ok: boolean }, Error, string>, "mutationFn">,
) {
  const { queryClient: qc, userKeys } = useCurrentUserQueryClient();
  return useMutation<{ ok: boolean }, Error, string>({
    mutationFn: (id) => deleteWorkflow(id),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.removeQueries({ queryKey: userKeys.workflow(vars) });
      qc.invalidateQueries({ queryKey: userKeys.workflowsAll() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export function useStoryboardsQuery(
  params: { cursor?: string | null; limit?: number } = {},
  options?: Omit<UseQueryOptions<StoryboardListResponse>, "queryKey" | "queryFn">,
) {
  const { userScope, userKeys } = useCurrentUserQueryKeys();
  return useQuery<StoryboardListResponse>({
    queryKey: userKeys.storyboards(params),
    queryFn: () => listStoryboards(params),
    staleTime: 10_000,
    refetchInterval: (query) => {
      const items = query.state.data?.items ?? [];
      return items.some((item) => item.status !== "completed") ? 30_000 : false;
    },
    refetchOnWindowFocus: true,
    ...options,
    enabled: privateQueryEnabled(userScope.enabled, options?.enabled),
  });
}

export function useStoryboardQuery(
  id: string | null | undefined,
  options?: Omit<UseQueryOptions<StoryboardRun>, "queryKey" | "queryFn">,
) {
  const { userScope, userKeys } = useCurrentUserQueryKeys();
  return useQuery<StoryboardRun>({
    queryKey: userKeys.storyboard(id ?? ""),
    queryFn: () => getStoryboard(id as string),
    staleTime: 5_000,
    refetchInterval: (query) => {
      const item = query.state.data;
      if (!item) return false;
      const hasActiveAsset = item.assets.some((asset) => asset.status === "generating");
      const hasActiveShot = item.shots.some((shot) =>
        ["keyframe_generating", "generating"].includes(shot.status),
      );
      const hasAssembly = item.assembly?.status === "compositing";
      return hasActiveAsset || hasActiveShot || hasAssembly ? 5_000 : false;
    },
    refetchOnWindowFocus: true,
    ...options,
    enabled: privateQueryEnabled(
      userScope.enabled,
      options?.enabled,
      typeof id === "string" && id.length > 0,
    ),
  });
}

function useStoryboardRunMutation<TVars>(
  storyboardId: string,
  mutationFn: (vars: TVars) => Promise<StoryboardRun>,
  options?: Omit<UseMutationOptions<StoryboardRun, Error, TVars>, "mutationFn">,
) {
  const { queryClient: qc, userKeys } = useCurrentUserQueryClient();
  return useMutation<StoryboardRun, Error, TVars>({
    mutationFn,
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.setQueryData(userKeys.storyboard(data.id), data);
      qc.invalidateQueries({ queryKey: userKeys.storyboardsAll() });
      qc.invalidateQueries({ queryKey: userKeys.workflowsAll() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
    onSettled: (data, error, vars, onMutateResult, ctx) => {
      qc.invalidateQueries({ queryKey: userKeys.storyboard(storyboardId) });
      options?.onSettled?.(data, error, vars, onMutateResult, ctx);
    },
  });
}

export function useCreateStoryboardMutation(
  options?: Omit<UseMutationOptions<StoryboardRun, Error, StoryboardCreateIn>, "mutationFn">,
) {
  const { queryClient: qc, userKeys } = useCurrentUserQueryClient();
  return useMutation<StoryboardRun, Error, StoryboardCreateIn>({
    mutationFn: createStoryboard,
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.setQueryData(userKeys.storyboard(data.id), data);
      qc.invalidateQueries({ queryKey: userKeys.storyboardsAll() });
      qc.invalidateQueries({ queryKey: userKeys.workflowsAll() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export function usePatchStoryboardMutation(
  storyboardId: string,
  options?: Omit<
    UseMutationOptions<StoryboardRun, Error, StoryboardPatchIn>,
    "mutationFn"
  >,
) {
  return useStoryboardRunMutation(
    storyboardId,
    (body) => patchStoryboard(storyboardId, body),
    options,
  );
}

export function useCreateStoryboardAssetMutation(
  storyboardId: string,
  options?: Omit<
    UseMutationOptions<StoryboardRun, Error, StoryboardAssetCreateIn>,
    "mutationFn"
  >,
) {
  return useStoryboardRunMutation(
    storyboardId,
    (body) => createStoryboardAsset(storyboardId, body),
    options,
  );
}

export function useGenerateStoryboardAssetMutation(
  storyboardId: string,
  stepId: string,
  options?: Omit<
    UseMutationOptions<StoryboardRun, Error, StoryboardGenerateIn | void>,
    "mutationFn"
  >,
) {
  return useStoryboardRunMutation(
    storyboardId,
    (body) => generateStoryboardAsset(storyboardId, stepId, body || {}),
    options,
  );
}

export function useApproveStoryboardAssetMutation(
  storyboardId: string,
  stepId: string,
  options?: Omit<UseMutationOptions<StoryboardRun, Error, void>, "mutationFn">,
) {
  return useStoryboardRunMutation(
    storyboardId,
    () => approveStoryboardAsset(storyboardId, stepId),
    options,
  );
}

export function useDeleteStoryboardAssetMutation(
  storyboardId: string,
  stepId: string,
  options?: Omit<UseMutationOptions<StoryboardRun, Error, void>, "mutationFn">,
) {
  return useStoryboardRunMutation(
    storyboardId,
    () => deleteStoryboardAsset(storyboardId, stepId),
    options,
  );
}

export function useRebuildStoryboardShotsMutation(
  storyboardId: string,
  options?: Omit<
    UseMutationOptions<
      StoryboardRun,
      Error,
      { shots?: StoryboardShotCreateIn[] | null; replace?: boolean } | void
    >,
    "mutationFn"
  >,
) {
  return useStoryboardRunMutation(
    storyboardId,
    (body) => rebuildStoryboardShots(storyboardId, body || {}),
    options,
  );
}

export function useCreateStoryboardShotMutation(
  storyboardId: string,
  options?: Omit<
    UseMutationOptions<StoryboardRun, Error, StoryboardShotCreateIn>,
    "mutationFn"
  >,
) {
  return useStoryboardRunMutation(
    storyboardId,
    (body) => createStoryboardShot(storyboardId, body),
    options,
  );
}

export function usePatchStoryboardShotMutation(
  storyboardId: string,
  stepId: string,
  options?: Omit<
    UseMutationOptions<StoryboardRun, Error, StoryboardShotPatchIn>,
    "mutationFn"
  >,
) {
  return useStoryboardRunMutation(
    storyboardId,
    (body) => patchStoryboardShot(storyboardId, stepId, body),
    options,
  );
}

export function useApproveStoryboardShotMutation(
  storyboardId: string,
  stepId: string,
  options?: Omit<UseMutationOptions<StoryboardRun, Error, void>, "mutationFn">,
) {
  return useStoryboardRunMutation(
    storyboardId,
    () => approveStoryboardShot(storyboardId, stepId),
    options,
  );
}

export function useMoveStoryboardShotMutation(
  storyboardId: string,
  stepId: string,
  options?: Omit<UseMutationOptions<StoryboardRun, Error, -1 | 1>, "mutationFn">,
) {
  return useStoryboardRunMutation(
    storyboardId,
    (direction) => moveStoryboardShot(storyboardId, stepId, direction),
    options,
  );
}

export function useDeleteStoryboardShotMutation(
  storyboardId: string,
  stepId: string,
  options?: Omit<UseMutationOptions<StoryboardRun, Error, void>, "mutationFn">,
) {
  return useStoryboardRunMutation(
    storyboardId,
    () => deleteStoryboardShot(storyboardId, stepId),
    options,
  );
}

export function useGenerateStoryboardKeyframeMutation(
  storyboardId: string,
  stepId: string,
  options?: Omit<
    UseMutationOptions<StoryboardRun, Error, StoryboardGenerateIn | void>,
    "mutationFn"
  >,
) {
  return useStoryboardRunMutation(
    storyboardId,
    (body) => generateStoryboardKeyframe(storyboardId, stepId, body || {}),
    options,
  );
}

export function useGenerateAllStoryboardKeyframesMutation(
  storyboardId: string,
  options?: Omit<UseMutationOptions<StoryboardRun, Error, void>, "mutationFn">,
) {
  return useStoryboardRunMutation(
    storyboardId,
    () => generateAllStoryboardKeyframes(storyboardId),
    options,
  );
}

export function useApproveStoryboardKeyframeMutation(
  storyboardId: string,
  stepId: string,
  options?: Omit<UseMutationOptions<StoryboardRun, Error, void>, "mutationFn">,
) {
  return useStoryboardRunMutation(
    storyboardId,
    () => approveStoryboardKeyframe(storyboardId, stepId),
    options,
  );
}

export function useSubmitStoryboardShotMutation(
  storyboardId: string,
  stepId: string,
  options?: Omit<
    UseMutationOptions<StoryboardRun, Error, StoryboardSubmitShotIn | void>,
    "mutationFn"
  >,
) {
  return useStoryboardRunMutation(
    storyboardId,
    (body) => submitStoryboardShot(storyboardId, stepId, body || {}),
    options,
  );
}

export function useSubmitAllStoryboardShotsMutation(
  storyboardId: string,
  options?: Omit<UseMutationOptions<StoryboardRun, Error, void>, "mutationFn">,
) {
  return useStoryboardRunMutation(
    storyboardId,
    () => submitAllStoryboardShots(storyboardId),
    options,
  );
}

export function useAssembleStoryboardMutation(
  storyboardId: string,
  options?: Omit<UseMutationOptions<StoryboardRun, Error, void>, "mutationFn">,
) {
  return useStoryboardRunMutation(
    storyboardId,
    () => assembleStoryboard(storyboardId),
    options,
  );
}

export function useApproveProductAnalysisMutation(
  workflowId: string,
  options?: Omit<
    UseMutationOptions<WorkflowRun, Error, Record<string, unknown>>,
    "mutationFn"
  >,
) {
  const { queryClient: qc, userKeys } = useCurrentUserQueryClient();
  return useMutation<WorkflowRun, Error, Record<string, unknown>>({
    mutationFn: (corrections) => approveProductAnalysis(workflowId, corrections),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.setQueryData(userKeys.workflow(workflowId), data);
      qc.invalidateQueries({ queryKey: userKeys.workflowsAll() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export function useCreateModelCandidatesMutation(
  workflowId: string,
  options?: Omit<
    UseMutationOptions<WorkflowRun, Error, ModelCandidatesIn>,
    "mutationFn"
  >,
) {
  const { queryClient: qc, userKeys } = useCurrentUserQueryClient();
  return useMutation<WorkflowRun, Error, ModelCandidatesIn>({
    mutationFn: (body) => createModelCandidates(workflowId, body),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.setQueryData(userKeys.workflow(workflowId), data);
      qc.invalidateQueries({ queryKey: userKeys.workflowsAll() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export function useApproveModelCandidateMutation(
  workflowId: string,
  options?: Omit<
    UseMutationOptions<
      WorkflowRun,
      Error,
      ApproveModelCandidateIn & { candidate_id: string }
    >,
    "mutationFn"
  >,
) {
  const { queryClient: qc, userKeys } = useCurrentUserQueryClient();
  return useMutation<
    WorkflowRun,
    Error,
    ApproveModelCandidateIn & { candidate_id: string }
  >({
    mutationFn: ({ candidate_id, ...body }) =>
      approveModelCandidate(workflowId, candidate_id, body),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.setQueryData(userKeys.workflow(workflowId), data);
      qc.invalidateQueries({ queryKey: userKeys.workflowsAll() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export function useReopenModelSelectionMutation(
  workflowId: string,
  options?: Omit<UseMutationOptions<WorkflowRun, Error, void>, "mutationFn">,
) {
  const { queryClient: qc, userKeys } = useCurrentUserQueryClient();
  return useMutation<WorkflowRun, Error, void>({
    mutationFn: () => reopenModelSelection(workflowId),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.setQueryData(userKeys.workflow(workflowId), data);
      qc.invalidateQueries({ queryKey: userKeys.workflowsAll() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export function useCreateAccessoryPreviewsMutation(
  workflowId: string,
  options?: Omit<UseMutationOptions<WorkflowRun, Error, AccessoryPreviewIn>, "mutationFn">,
) {
  const { queryClient: qc, userKeys } = useCurrentUserQueryClient();
  return useMutation<WorkflowRun, Error, AccessoryPreviewIn>({
    mutationFn: (body) => createAccessoryPreviews(workflowId, body),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.setQueryData(userKeys.workflow(workflowId), data);
      qc.invalidateQueries({ queryKey: userKeys.workflowsAll() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export function useSaveAccessorySelectionMutation(
  workflowId: string,
  options?: Omit<UseMutationOptions<WorkflowRun, Error, AccessorySelectionIn>, "mutationFn">,
) {
  const { queryClient: qc, userKeys } = useCurrentUserQueryClient();
  return useMutation<WorkflowRun, Error, AccessorySelectionIn>({
    mutationFn: (body) => saveAccessorySelection(workflowId, body),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.setQueryData(userKeys.workflow(workflowId), data);
      qc.invalidateQueries({ queryKey: userKeys.workflowsAll() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export function useCreateShowcaseImagesMutation(
  workflowId: string,
  options?: Omit<
    UseMutationOptions<WorkflowRun, Error, CreateShowcaseImagesIn>,
    "mutationFn"
  >,
) {
  const { queryClient: qc, userKeys } = useCurrentUserQueryClient();
  return useMutation<WorkflowRun, Error, CreateShowcaseImagesIn>({
    mutationFn: (body) => createShowcaseImages(workflowId, body),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.setQueryData(userKeys.workflow(workflowId), data);
      qc.invalidateQueries({ queryKey: userKeys.workflowsAll() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export function useReviseWorkflowImageMutation(
  workflowId: string,
  options?: Omit<
    UseMutationOptions<
      WorkflowRun,
      Error,
      ReviseWorkflowImageIn & { image_id: string }
    >,
    "mutationFn"
  >,
) {
  const { queryClient: qc, userKeys } = useCurrentUserQueryClient();
  return useMutation<
    WorkflowRun,
    Error,
    ReviseWorkflowImageIn & { image_id: string }
  >({
    mutationFn: ({ image_id, ...body }) =>
      reviseWorkflowImage(workflowId, image_id, body),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.setQueryData(userKeys.workflow(workflowId), data);
      qc.invalidateQueries({ queryKey: userKeys.workflowsAll() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export function useCompleteWorkflowDeliveryMutation(
  workflowId: string,
  options?: Omit<UseMutationOptions<WorkflowRun, Error, void>, "mutationFn">,
) {
  const { queryClient: qc, userKeys } = useCurrentUserQueryClient();
  return useMutation<WorkflowRun, Error, void>({
    mutationFn: () => completeWorkflowDelivery(workflowId),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.setQueryData(userKeys.workflow(workflowId), data);
      qc.invalidateQueries({ queryKey: userKeys.workflowsAll() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export function useApparelModelLibraryQuery(
  params: {
    age_segment?: ModelLibraryAgeSegment;
    source?: "all" | ModelLibrarySource;
    appearance?: ModelLibraryAppearance;
    q?: string;
  } = {},
  options?: Omit<
    UseQueryOptions<ApparelModelLibraryListResponse>,
    "queryKey" | "queryFn"
  >,
) {
  const { userScope, userKeys } = useCurrentUserQueryKeys();
  return useQuery<ApparelModelLibraryListResponse>({
    queryKey: userKeys.apparelModelLibrary(params),
    queryFn: () => listApparelModelLibrary(params),
    staleTime: 15_000,
    ...options,
    enabled: privateQueryEnabled(userScope.enabled, options?.enabled),
  });
}

export function useSyncApparelModelLibraryPresetsMutation(
  options?: Omit<
    UseMutationOptions<Awaited<ReturnType<typeof syncApparelModelLibraryPresets>>, Error, void>,
    "mutationFn"
  >,
) {
  const { queryClient: qc, userKeys } = useCurrentUserQueryClient();
  return useMutation<Awaited<ReturnType<typeof syncApparelModelLibraryPresets>>, Error, void>({
    mutationFn: () => syncApparelModelLibraryPresets(),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.invalidateQueries({ queryKey: userKeys.apparelModelLibraryLists() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export function useCreateApparelModelLibraryItemMutation(
  options?: Omit<
    UseMutationOptions<ApparelModelLibraryItem, Error, ApparelModelLibraryItemCreateIn>,
    "mutationFn"
  >,
) {
  const { queryClient: qc, userKeys } = useCurrentUserQueryClient();
  return useMutation<ApparelModelLibraryItem, Error, ApparelModelLibraryItemCreateIn>({
    mutationFn: createApparelModelLibraryItem,
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.invalidateQueries({ queryKey: userKeys.apparelModelLibraryLists() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export function useDeleteApparelModelLibraryItemMutation(
  options?: Omit<UseMutationOptions<{ ok: boolean }, Error, string>, "mutationFn">,
) {
  const { queryClient: qc, userKeys } = useCurrentUserQueryClient();
  return useMutation<{ ok: boolean }, Error, string>({
    mutationFn: deleteApparelModelLibraryItem,
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.invalidateQueries({ queryKey: userKeys.apparelModelLibraryLists() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export function useDeleteApparelModelLibraryItemsMutation(
  options?: Omit<
    UseMutationOptions<ApparelModelLibraryBatchDeleteOut, Error, string[]>,
    "mutationFn"
  >,
) {
  const { queryClient: qc, userKeys } = useCurrentUserQueryClient();
  return useMutation<ApparelModelLibraryBatchDeleteOut, Error, string[]>({
    mutationFn: deleteApparelModelLibraryItems,
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.invalidateQueries({ queryKey: userKeys.apparelModelLibraryLists() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export function useSelectApparelModelLibraryItemMutation(
  workflowId: string,
  options?: Omit<
    UseMutationOptions<WorkflowRun, Error, ApparelModelLibrarySelectIn>,
    "mutationFn"
  >,
) {
  const { queryClient: qc, userKeys } = useCurrentUserQueryClient();
  return useMutation<WorkflowRun, Error, ApparelModelLibrarySelectIn>({
    mutationFn: (body) => selectApparelModelLibraryItem(workflowId, body),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.setQueryData(userKeys.workflow(workflowId), data);
      qc.invalidateQueries({ queryKey: userKeys.workflowsAll() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export function useSaveModelCandidateToLibraryMutation(
  workflowId: string,
  candidateId: string,
  options?: Omit<
    UseMutationOptions<ApparelModelLibraryItem, Error, ModelCandidateSaveToLibraryIn>,
    "mutationFn"
  >,
) {
  const { queryClient: qc, userKeys } = useCurrentUserQueryClient();
  return useMutation<ApparelModelLibraryItem, Error, ModelCandidateSaveToLibraryIn>({
    mutationFn: (body) => saveModelCandidateToLibrary(workflowId, candidateId, body),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.invalidateQueries({ queryKey: userKeys.apparelModelLibraryLists() });
      qc.invalidateQueries({ queryKey: userKeys.workflow(workflowId) });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export function useUploadImageMutation(
  options?: Omit<UseMutationOptions<UploadedImage, Error, File>, "mutationFn">,
) {
  return useMutation<UploadedImage, Error, File>({
    mutationFn: (file) => uploadImage(file),
    ...options,
  });
}

// ——— Apparel model library: standalone generation ———

export function useGenerateApparelModelLibraryMutation(
  options?: Omit<
    UseMutationOptions<ApparelModelLibraryJob, Error, ApparelModelLibraryGenerateIn>,
    "mutationFn"
  >,
) {
  const { queryClient: qc, userKeys } = useCurrentUserQueryClient();
  return useMutation<ApparelModelLibraryJob, Error, ApparelModelLibraryGenerateIn>({
    mutationFn: generateApparelModelLibrary,
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.invalidateQueries({ queryKey: userKeys.apparelModelLibraryJobs() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export function useApparelModelLibraryJobsQuery(
  params?: ApparelModelLibraryJobsOpts,
  options?: Omit<
    UseQueryOptions<ApparelModelLibraryJobsList>,
    "queryKey" | "queryFn"
  >,
) {
  const { userScope, userKeys } = useCurrentUserQueryKeys();
  return useQuery<ApparelModelLibraryJobsList>({
    queryKey: userKeys.apparelModelLibraryJobsList(params),
    queryFn: () => getApparelModelLibraryJobs(params),
    // 5s 轮询只在确实有进行中任务时开启；历史页很重，空跑会放大卡顿。
    refetchInterval: (query) =>
      query.state.data?.items.some(
        (job) => job.status === "queued" || job.status === "running",
      )
        ? 5_000
        : false,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: true,
    staleTime: 2_000,
    ...options,
    enabled: privateQueryEnabled(userScope.enabled, options?.enabled),
  });
}

export function useApparelModelLibraryJobsInfiniteQuery(params?: { limit?: number }) {
  const { userScope, userKeys } = useCurrentUserQueryKeys();
  const limit = params?.limit ?? 30;
  return useInfiniteQuery<
    ApparelModelLibraryJobsList,
    Error,
    InfiniteData<ApparelModelLibraryJobsList, number>,
    readonly [
      "user",
      string,
      "workflows",
      "apparel_model_library",
      "jobs",
      "infinite",
      { limit: number },
    ],
    number
  >({
    queryKey: userKeys.apparelModelLibraryJobsInfinite({ limit }),
    queryFn: ({ pageParam }) =>
      getApparelModelLibraryJobs({ limit, offset: pageParam }),
    initialPageParam: 0,
    getNextPageParam: (last) =>
      last.has_more ? last.offset + last.items.length : undefined,
    refetchInterval: (query) =>
      query.state.data?.pages[0]?.items.some(
        (job) => job.status === "queued" || job.status === "running",
      )
        ? 5_000
        : false,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: true,
    staleTime: 2_000,
    enabled: userScope.enabled,
  });
}

export function useSaveApparelModelLibraryJobItemMutation(
  workflowRunId: string,
  imageId: string,
  options?: Omit<
    UseMutationOptions<ApparelModelLibraryItem, Error, ApparelModelLibrarySaveJobItemIn>,
    "mutationFn"
  >,
) {
  const { queryClient: qc, userKeys } = useCurrentUserQueryClient();
  return useMutation<ApparelModelLibraryItem, Error, ApparelModelLibrarySaveJobItemIn>({
    mutationFn: (body) =>
      saveApparelModelLibraryJobItem(workflowRunId, imageId, body),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.invalidateQueries({ queryKey: userKeys.apparelModelLibraryJobs() });
      qc.invalidateQueries({ queryKey: userKeys.apparelModelLibraryLists() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export function useDeleteApparelModelLibraryJobMutation(
  options?: Omit<UseMutationOptions<{ ok: boolean }, Error, string>, "mutationFn">,
) {
  const { queryClient: qc, userKeys } = useCurrentUserQueryClient();
  return useMutation<{ ok: boolean }, Error, string>({
    mutationFn: deleteApparelModelLibraryJob,
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.invalidateQueries({ queryKey: userKeys.apparelModelLibraryJobs() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export function useClearApparelModelLibraryJobsMutation(
  options?: Omit<
    UseMutationOptions<{ ok: boolean; deleted: number }, Error, void>,
    "mutationFn"
  >,
) {
  const { queryClient: qc, userKeys } = useCurrentUserQueryClient();
  return useMutation<{ ok: boolean; deleted: number }, Error, void>({
    mutationFn: () => clearApparelModelLibraryJobs(),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.invalidateQueries({ queryKey: userKeys.apparelModelLibraryJobs() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

export function useAutoTagApparelModelLibraryItemMutation(
  itemId: string,
  options?: Omit<
    UseMutationOptions<ApparelModelLibraryAutoTagOut, Error, void>,
    "mutationFn"
  >,
) {
  const { queryClient: qc, userKeys } = useCurrentUserQueryClient();
  return useMutation<ApparelModelLibraryAutoTagOut, Error, void>({
    mutationFn: () => autoTagApparelModelLibraryItem(itemId),
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      qc.invalidateQueries({ queryKey: userKeys.apparelModelLibraryLists() });
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}
