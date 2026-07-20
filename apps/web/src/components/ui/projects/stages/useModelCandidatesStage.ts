import { useState } from "react";

import { toast } from "@/components/ui/primitives/Toast";
import type {
  AccessoryPlan,
  BackendImageMeta,
  WorkflowRun,
  WorkflowStep,
} from "@/lib/apiClient";
import {
  useApproveModelCandidateMutation,
  useCreateAccessoryPreviewsMutation,
  useCreateModelCandidatesMutation,
  useCreateShowcaseImagesMutation,
  useSaveAccessorySelectionMutation,
} from "@/lib/queries";
import {
  accessorySuggestionText,
  defaultLibraryAgeSegment,
  imageById,
  stepOf,
  stringArray,
  stringValue,
} from "../utils";
import {
  buildShowcaseRequest,
  type ShowcaseFormController,
  useShowcaseStageForm,
} from "./showcaseStageForm";

type StepJson = Record<string, unknown> | undefined;
type ModelCandidate = WorkflowRun["model_candidates"][number];

interface ModelCandidateSteps {
  approval?: WorkflowStep;
  candidate?: WorkflowStep;
  modelSettings?: WorkflowStep;
  showcase?: WorkflowStep;
}

interface ModelCandidatesStageData {
  accessoryPlan: AccessoryPlan;
  approvalPreviewRunning: boolean;
  candidateStepRunning: boolean;
  defaultAgeSegment: ReturnType<typeof defaultLibraryAgeSegment>;
  modelStylePrompt: string;
  persistedAccessoryId: string | null;
  showcaseHasTasks: boolean;
  showcaseInput: StepJson;
  showcaseRunning: boolean;
  stageError: string | null;
  suggestedAccessoryPrompt: string;
  avoidItems: string[];
  accessoryImageIds: string[];
}

export function useModelCandidatesStageController(workflow: WorkflowRun) {
  const data = buildModelCandidatesStageData(workflow);
  const mutations = useModelCandidatesStageMutations(workflow.id);
  const form = useShowcaseStageForm(
    data.showcaseInput,
    data.showcaseRunning,
  );
  const state = useModelCandidatesStageState(workflow, data);
  const actions = buildModelCandidatesStageActions({
    data,
    form,
    mutations,
    state,
  });
  const accessoryImages = resolveAccessoryImages(
    workflow,
    data.accessoryImageIds,
  );

  return {
    ...actions,
    accessoryImages,
    accessoryItems: data.accessoryPlan.items,
    accessoryPlan: data.accessoryPlan,
    accessoryPreviewRunning:
      mutations.createAccessoryPreviews.isPending ||
      data.approvalPreviewRunning,
    accessoryPrompt: state.accessoryPrompt,
    adjustments: state.adjustments,
    approve: mutations.approve,
    candidateGenerationRunning:
      mutations.createCandidates.isPending || data.candidateStepRunning,
    candidates: workflow.model_candidates,
    chosenCandidate: state.chosenCandidate,
    confirmRegenerate: state.confirmRegenerate,
    createAccessoryPreviews: mutations.createAccessoryPreviews,
    createCandidates: mutations.createCandidates,
    createShowcase: mutations.createShowcase,
    defaultAgeSegment: data.defaultAgeSegment,
    form,
    isShowcaseRunning: data.showcaseRunning,
    libraryOpen: state.libraryOpen,
    modelStylePrompt: data.modelStylePrompt,
    previewIndex: state.previewIndex,
    previewList: state.previewList,
    saveAccessorySelection: mutations.saveAccessorySelection,
    savingCandidateId: state.savingCandidateId,
    selectedAccessoryImageId: state.selectedAccessoryImageId,
    selectedCandidate: state.selectedCandidate,
    setAccessoryPrompt: state.setAccessoryPrompt,
    setAdjustments: state.setAdjustments,
    setChosenCandidateId: state.setChosenCandidateId,
    setConfirmRegenerate: state.setConfirmRegenerate,
    setLibraryOpen: state.setLibraryOpen,
    setPreviewIndex: state.setPreviewIndex,
    setSavingCandidateId: state.setSavingCandidateId,
    showcaseHasTasks: data.showcaseHasTasks,
    stageError: data.stageError,
  };
}

export type ModelCandidatesStageController = ReturnType<
  typeof useModelCandidatesStageController
>;

function buildModelCandidatesStageData(
  workflow: WorkflowRun,
): ModelCandidatesStageData {
  const steps = modelCandidateSteps(workflow);
  const approvalInput = stepInput(steps.approval);
  const candidateInput = stepInput(steps.candidate);
  const settingsOutput = stepOutput(steps.modelSettings);
  const approvalOutput = stepOutput(steps.approval);
  const candidateOutput = stepOutput(steps.candidate);
  const accessoryPlan = normalizeAccessoryPlan(
    resolveAccessoryPlanInput(
      approvalInput,
      candidateInput,
      settingsOutput,
    ),
    accessorySuggestionText(workflow),
  );

  return {
    accessoryImageIds: stepImageIds(steps.approval),
    accessoryPlan,
    approvalPreviewRunning: stepRunningWithTasks(steps.approval),
    avoidItems: resolveAvoidItems(candidateInput, settingsOutput),
    candidateStepRunning: stepIsRunning(steps.candidate),
    defaultAgeSegment: defaultLibraryAgeSegment(workflow),
    modelStylePrompt: resolveModelStylePrompt(
      workflow,
      approvalInput,
      candidateInput,
      settingsOutput,
    ),
    persistedAccessoryId: resolvePersistedAccessoryId(
      approvalInput,
      approvalOutput,
    ),
    showcaseHasTasks: stepHasTasks(steps.showcase),
    showcaseInput: stepInput(steps.showcase),
    showcaseRunning: stepIsRunning(steps.showcase),
    stageError: resolveStageError(approvalOutput, candidateOutput),
    suggestedAccessoryPrompt: accessoryPlan.items.join("、"),
  };
}

function modelCandidateSteps(workflow: WorkflowRun): ModelCandidateSteps {
  return {
    approval: stepOf(workflow, "model_approval"),
    candidate: stepOf(workflow, "model_candidates"),
    modelSettings: stepOf(workflow, "model_settings"),
    showcase: stepOf(workflow, "showcase_generation"),
  };
}

function useModelCandidatesStageMutations(workflowId: string) {
  const approve = useApproveModelCandidateMutation(workflowId, {
    onError: (err) =>
      toast.error("确认模特失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      }),
  });
  const createCandidates = useCreateModelCandidatesMutation(workflowId, {
    onError: (err) =>
      toast.error("生成模特候选失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      }),
    onSuccess: () => toast.success("已派发 3 套模特候选生成"),
  });
  const saveAccessorySelection =
    useSaveAccessorySelectionMutation(workflowId, {
      onError: (err) =>
        toast.error("保存配饰四宫格选择失败", {
          description: err instanceof Error ? err.message : "请稍后重试",
        }),
    });
  const createShowcase = useCreateShowcaseImagesMutation(workflowId, {
    onError: (err) =>
      toast.error("生成展示图失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      }),
    onSuccess: () => toast.success("展示图任务已派发"),
  });
  const createAccessoryPreviews =
    useCreateAccessoryPreviewsMutation(workflowId, {
      onError: (err) =>
        toast.error("生成配饰四宫格失败", {
          description:
            err instanceof Error
              ? err.message
              : "请先确认模特后再重新生成配饰四宫格",
        }),
      onSuccess: () => toast.success("配饰四宫格任务已派发"),
    });

  return {
    approve,
    createAccessoryPreviews,
    createCandidates,
    createShowcase,
    saveAccessorySelection,
  };
}

function useModelCandidatesStageState(
  workflow: WorkflowRun,
  data: ModelCandidatesStageData,
) {
  const [adjustments, setAdjustments] = useState("");
  const [accessoryPrompt, setAccessoryPrompt] = useSyncedSuggestedPrompt(
    data.suggestedAccessoryPrompt,
  );
  const [previewList, setPreviewList] = useState<BackendImageMeta[]>([]);
  const [previewIndex, setPreviewIndex] = useState(-1);
  const [confirmRegenerate, setConfirmRegenerate] = useState(false);
  const [libraryOpen, setLibraryOpen] = useState(false);
  const [chosenCandidateId, setChosenCandidateId] = useState<string | null>(
    null,
  );
  const [savingCandidateId, setSavingCandidateId] = useState<string | null>(
    null,
  );
  const [selectedAccessoryImageId, setSelectedAccessoryImageId] =
    useSyncedValue(data.persistedAccessoryId);
  const selection = resolveCandidateSelection(
    workflow.model_candidates,
    chosenCandidateId,
  );

  return {
    accessoryPrompt,
    adjustments,
    chosenCandidate: selection.chosen,
    confirmRegenerate,
    libraryOpen,
    previewIndex,
    previewList,
    savingCandidateId,
    selectedAccessoryImageId,
    selectedCandidate: selection.selected,
    setAccessoryPrompt,
    setAdjustments,
    setChosenCandidateId,
    setConfirmRegenerate,
    setLibraryOpen,
    setPreviewIndex,
    setPreviewList,
    setSavingCandidateId,
    setSelectedAccessoryImageId,
  };
}

type ModelCandidatesStageMutations = ReturnType<
  typeof useModelCandidatesStageMutations
>;
type ModelCandidatesStageState = ReturnType<
  typeof useModelCandidatesStageState
>;

function buildModelCandidatesStageActions({
  data,
  form,
  mutations,
  state,
}: {
  data: ModelCandidatesStageData;
  form: ShowcaseFormController;
  mutations: ModelCandidatesStageMutations;
  state: ModelCandidatesStageState;
}) {
  const openPreview = (
    _image: BackendImageMeta,
    list: BackendImageMeta[],
    index: number,
  ) => {
    state.setPreviewList(list);
    state.setPreviewIndex(index);
  };
  const createShowcaseImages = () => {
    mutations.createShowcase.mutate(buildShowcaseRequest(form));
  };
  const requestShowcaseGeneration = () => {
    if (data.showcaseHasTasks) state.setConfirmRegenerate(true);
    else createShowcaseImages();
  };
  const confirmShowcaseGeneration = async () => {
    createShowcaseImages();
    state.setConfirmRegenerate(false);
  };
  const generateAccessoryPreview = () => {
    if (!state.selectedCandidate) return;
    mutations.createAccessoryPreviews.mutate({
      candidate_id: state.selectedCandidate.id,
      accessory_plan: data.accessoryPlan,
      style_prompt:
        state.accessoryPrompt || data.suggestedAccessoryPrompt,
    });
  };
  const approveChosenCandidate = () => {
    if (!state.chosenCandidate) return;
    mutations.approve.mutate({
      candidate_id: state.chosenCandidate.id,
      adjustments: state.adjustments,
      accessory_plan: data.accessoryPlan,
      selected_accessory_image_id: state.selectedAccessoryImageId,
    });
  };
  const regenerateCandidates = () => {
    if (!data.modelStylePrompt.trim()) {
      toast.warning("请先填写模特风格方向");
      return;
    }
    mutations.createCandidates.mutate({
      candidate_count: 3,
      style_prompt: data.modelStylePrompt,
      avoid: data.avoidItems,
      accessory_plan: data.accessoryPlan,
    });
  };
  const selectAccessoryImage = (imageId: string | null) => {
    state.setSelectedAccessoryImageId(imageId);
    mutations.saveAccessorySelection.mutate({
      selected_accessory_image_id: imageId,
    });
  };
  const generateCandidatesFromLibrary = () => {
    state.setLibraryOpen(false);
    regenerateCandidates();
  };

  return {
    approveChosenCandidate,
    confirmShowcaseGeneration,
    generateAccessoryPreview,
    generateCandidatesFromLibrary,
    openPreview,
    regenerateCandidates,
    requestShowcaseGeneration,
    selectAccessoryImage,
  };
}

function useSyncedSuggestedPrompt(
  suggestedPrompt: string,
): [string, (value: string) => void] {
  const [value, setValue] = useState(suggestedPrompt);
  const [trackedPrompt, setTrackedPrompt] = useState(suggestedPrompt);
  if (trackedPrompt !== suggestedPrompt) {
    const previousPrompt = trackedPrompt;
    setTrackedPrompt(suggestedPrompt);
    setValue((current) => {
      if (!current.trim() || current === previousPrompt) return suggestedPrompt;
      return current;
    });
  }
  return [value, setValue];
}

function useSyncedValue<Value>(
  source: Value,
): [Value, (value: Value) => void] {
  const [value, setValue] = useState(source);
  const [tracked, setTracked] = useState(source);
  if (!Object.is(tracked, source)) {
    setTracked(source);
    setValue(source);
  }
  return [value, setValue];
}

function resolveCandidateSelection(
  candidates: ModelCandidate[],
  chosenCandidateId: string | null,
) {
  const selected = candidates.find(
    (candidate) => candidate.status === "selected",
  );
  const chosen =
    selected ??
    candidates.find((candidate) => candidate.id === chosenCandidateId);
  return { chosen, selected };
}

function resolveModelStylePrompt(
  workflow: WorkflowRun,
  approvalInput: StepJson,
  candidateInput: StepJson,
  settingsOutput: StepJson,
): string {
  return (
    stringValue(approvalInput?.style_prompt) ??
    stringValue(candidateInput?.style_prompt) ??
    stringValue(settingsOutput?.style_prompt) ??
    workflow.user_prompt
  );
}

function resolveAvoidItems(
  candidateInput: StepJson,
  settingsOutput: StepJson,
): string[] {
  if (hasNonEmptyStringArray(candidateInput?.avoid)) {
    return stringArray(candidateInput?.avoid);
  }
  return stringArray(settingsOutput?.avoid);
}

function resolveAccessoryPlanInput(
  approvalInput: StepJson,
  candidateInput: StepJson,
  settingsOutput: StepJson,
): unknown {
  return (
    approvalInput?.accessory_plan ??
    candidateInput?.accessory_plan ??
    settingsOutput?.accessory_plan
  );
}

function resolvePersistedAccessoryId(
  approvalInput: StepJson,
  approvalOutput: StepJson,
): string | null {
  return (
    stringValue(approvalInput?.selected_accessory_image_id) ??
    stringValue(approvalOutput?.selected_accessory_image_id)
  );
}

function resolveStageError(
  approvalOutput: StepJson,
  candidateOutput: StepJson,
): string | null {
  return (
    stringValue(approvalOutput?.error_message) ??
    stringValue(candidateOutput?.error_message)
  );
}

function resolveAccessoryImages(
  workflow: WorkflowRun,
  imageIds: string[],
): BackendImageMeta[] {
  return imageIds
    .map((imageId) => imageById(workflow, imageId))
    .filter((image): image is BackendImageMeta => Boolean(image));
}

function normalizeAccessoryPlan(
  value: unknown,
  fallbackText: string,
): AccessoryPlan {
  const fallbackItems = splitPromptList(fallbackText).slice(0, 3);
  if (!value || typeof value !== "object") {
    return {
      enabled: true,
      items: fallbackItems,
      strength: "subtle",
    };
  }
  const raw = value as {
    enabled?: unknown;
    items?: unknown;
    strength?: unknown;
  };
  const strength =
    raw.strength === "medium" || raw.strength === "strong"
      ? raw.strength
      : "subtle";
  const items = stringArray(raw.items);
  return {
    enabled: raw.enabled !== false,
    items: items.length ? items : fallbackItems,
    strength,
  };
}

function splitPromptList(value: string): string[] {
  return value
    .split(/[,，、;\n]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function hasNonEmptyStringArray(value: unknown): boolean {
  return (
    Array.isArray(value) &&
    value.some((item) => typeof item === "string" && item.length > 0)
  );
}

function stepInput(step: WorkflowStep | undefined): StepJson {
  return step?.input_json;
}

function stepOutput(step: WorkflowStep | undefined): StepJson {
  return step?.output_json;
}

function stepImageIds(step: WorkflowStep | undefined): string[] {
  return step?.image_ids ?? [];
}

function stepHasTasks(step: WorkflowStep | undefined): boolean {
  return Boolean(step?.task_ids?.length);
}

function stepIsRunning(step: WorkflowStep | undefined): boolean {
  return step?.status === "running";
}

function stepRunningWithTasks(step: WorkflowStep | undefined): boolean {
  return stepIsRunning(step) && stepHasTasks(step);
}
