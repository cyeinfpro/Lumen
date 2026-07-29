"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { toast } from "@/components/ui/primitives";
import type {
  VideoAction,
  VideoGenerationOut,
} from "@/lib/types";
import {
  nextVideoRequestFence,
} from "@/lib/videoEventSnapshot";
import type { VideoRequestFence } from "@/lib/videoEventSnapshot";

import {
  displayPromptReferenceMentions,
  referenceDisplayToken,
  referenceLimitsForModelOption,
} from "./video-reference-domain";
import {
  canApplyPromptEnhanceCandidate,
  focusVideoWorkbenchElement,
} from "./video-workbench-ui";
import type {
  PromptEnhanceCandidate,
  ReferenceDraft,
} from "./video-workbench-ui";
import {
  billingModelForAction,
  durationOptionsForModel,
  estimateHoldMicro,
  parseSeed,
  resolutionOptionsForModel,
} from "./video-options-model";
import {
  motionSafeScrollBehavior,
} from "./video-page-utils";
import { hasPromptEnhancementPanel } from "./video-page-derived-state";
import {
  effectiveVideoDuration,
  effectiveVideoResolution,
  referenceDraftFromHistory,
  selectedVideoModel,
  videoServiceSummary,
  videoSourceReady,
  videoSubmitDisabledReason,
} from "./video-page-domain";
import {
  useVideoGenerationFeed,
} from "./use-video-generation-feed";
import {
  useVideoDraftMediaController,
} from "./use-video-draft-media-controller";
import {
  useVideoParameterHandlers,
} from "./use-video-parameter-handlers";
import {
  useVideoPageViewActions,
} from "./use-video-page-view-actions";
import {
  useVideoReferenceSummary,
} from "./use-video-reference-summary";
import {
  useVideoPromptEnhancement,
} from "./use-video-prompt-enhancement";
import {
  useVideoTaskMutations,
} from "./use-video-task-mutations";
import {
  VideoPageView,
} from "./video-page-view";
import type { VideoPageViewModel } from "./video-page-view";
import {
  formatDurationLabel,
} from "./video-task-model";

export default function VideoPage() {
  const promptRef = useRef<HTMLTextAreaElement | null>(null);
  const promptEnhanceAbortRef = useRef<AbortController | null>(null);
  const promptEnhanceEpochRef = useRef(0);
  const draftFenceRef = useRef<VideoRequestFence>({
    taskId: "draft:new",
    epoch: 0,
  });
  const retryRequestFenceRef = useRef<VideoRequestFence>({
    taskId: "retry:none",
    epoch: 0,
  });
  const actionRef = useRef<VideoAction>("t2v");
  const promptValueRef = useRef("");

  const [action, setAction] = useState<VideoAction>("t2v");
  const [prompt, setPrompt] = useState("");
  const [model, setModel] = useState("");
  const [durationS, setDurationS] = useState(5);
  const [resolution, setResolution] = useState("720p");
  const [aspectRatio, setAspectRatio] = useState("adaptive");
  const [generateAudio, setGenerateAudio] = useState(true);
  const [seed, setSeed] = useState("");
  const [isEnhancingPrompt, setIsEnhancingPrompt] = useState(false);
  const [promptEnhancePreview, setPromptEnhancePreview] = useState("");
  const [promptEnhanceCandidates, setPromptEnhanceCandidates] = useState<
    PromptEnhanceCandidate[]
  >([]);
  const [
    selectedPromptEnhanceCandidateId,
    setSelectedPromptEnhanceCandidateId,
  ] = useState("");

  const {
    abortGenerationRefresh,
    activeItems,
    disableVideoSettling,
    effectiveItems,
    enableVideoSettling,
    failedHistoryItems,
    filteredHistoryItems,
    historyFilter,
    historyQ,
    invalidateHistory,
    isTaskPanelOpen,
    options,
    optionsQ,
    playbackVideoItem,
    scheduleGenerationRefresh,
    selectedVideoId,
    setHistoryFilter,
    setIsTaskPanelOpen,
    setItems,
    setSelectedVideoId,
    settledHistoryItems,
    succeededHistoryItems,
    syncVideoSettling,
    terminalHistorySyncedRef,
  } = useVideoGenerationFeed();

  const promptEnhancePanelVisible = hasPromptEnhancementPanel(
    isEnhancingPrompt,
    promptEnhancePreview,
    promptEnhanceCandidates,
  );

  useEffect(() => {
    actionRef.current = action;
  }, [action]);

  useEffect(() => {
    promptValueRef.current = prompt;
  }, [prompt]);

  useEffect(
    () => () => {
      retryRequestFenceRef.current = nextVideoRequestFence(
        retryRequestFenceRef.current,
        "retry:disposed",
      );
      promptEnhanceAbortRef.current?.abort();
    },
    [],
  );

  const availableModels = useMemo(
    () => options?.models.filter((item) => item.actions.includes(action)) ?? [],
    [action, options?.models],
  );
  const selectedModel = selectedVideoModel(availableModels, model);
  const selectedModelOption = availableModels.find(
    (item) => item.model === selectedModel,
  );
  const referenceLimits = useMemo(
    () => referenceLimitsForModelOption(selectedModelOption, selectedModel),
    [selectedModel, selectedModelOption],
  );

  const clearPromptEnhanceChoices = useCallback(() => {
    setPromptEnhancePreview("");
    setPromptEnhanceCandidates([]);
    setSelectedPromptEnhanceCandidateId("");
  }, []);

  const clearPromptEnhanceSelection = useCallback(() => {
    setPromptEnhancePreview("");
    setSelectedPromptEnhanceCandidateId("");
  }, []);

  const abortPromptEnhancement = useCallback(() => {
    promptEnhanceEpochRef.current += 1;
    const controller = promptEnhanceAbortRef.current;
    promptEnhanceAbortRef.current = null;
    controller?.abort();
    setIsEnhancingPrompt(false);
  }, []);

  const beforeMediaChange = useCallback(() => {
    abortPromptEnhancement();
    clearPromptEnhanceChoices();
  }, [abortPromptEnhancement, clearPromptEnhanceChoices]);

  const {
    addAssetReference,
    assetReferenceKind,
    assetUrlInput,
    cancelFirstFrameUpload,
    cancelReferenceUpload,
    firstFrameUploadPending,
    handleInputImageIdChange,
    hasActiveUpload,
    inputImageId,
    isVolcanoAssetManagerOpen,
    loadDraftMedia,
    referenceMedia,
    referencePreviewItem,
    referenceUploadPending,
    removeDeletedVolcanoAssets,
    removeReferenceDraft,
    setAssetReferenceKind,
    setAssetUrlInput,
    setIsVolcanoAssetManagerOpen,
    setReferencePreviewItem,
    startFirstFrameUpload,
    startReferenceUpload,
    uploadedLabel,
    useVolcanoAssets,
  } = useVideoDraftMediaController({
    actionRef,
    draftFenceRef,
    promptValueRef,
    referenceLimits,
    beforeMediaChange,
    setPrompt,
  });

  const {
    assetReferenceKindOptions,
    existingVolcanoAssetIds,
    referenceCounts,
    referenceLimitError,
    remainingVolcanoAssetLimits,
    selectedAssetReferenceKind,
  } = useVideoReferenceSummary(
    referenceMedia,
    referenceLimits,
    assetReferenceKind,
  );
  const selectedBillingModel = billingModelForAction(
    options,
    selectedModel,
    action,
  );
  const availableResolutions = useMemo(
    () => resolutionOptionsForModel(options, selectedModel),
    [options, selectedModel],
  );
  const effectiveResolution = effectiveVideoResolution(
    availableResolutions,
    resolution,
  );
  const availableDurations = useMemo(
    () =>
      durationOptionsForModel(
        options,
        selectedModel,
        action,
        effectiveResolution,
      ),
    [action, effectiveResolution, options, selectedModel],
  );
  const effectiveDurationS = effectiveVideoDuration(
    availableDurations,
    durationS,
  );
  const estimate = estimateHoldMicro(options, {
    model: selectedModel,
    billingModel: selectedBillingModel,
    action,
    resolution: effectiveResolution,
    durationS: effectiveDurationS,
    referenceHasVideo: referenceMedia.some((item) => item.kind === "video"),
  });
  const seedIsValid = !seed.trim() || parseSeed(seed) !== null;

  const switchDraftContext = useCallback(
    (taskId: string, nextAction: VideoAction) => {
      draftFenceRef.current = nextVideoRequestFence(
        draftFenceRef.current,
        taskId,
      );
      actionRef.current = nextAction;
      abortPromptEnhancement();
      cancelFirstFrameUpload();
      cancelReferenceUpload();
      clearPromptEnhanceChoices();
      setReferencePreviewItem(null);
      setIsVolcanoAssetManagerOpen(false);
    },
    [
      abortPromptEnhancement,
      cancelFirstFrameUpload,
      cancelReferenceUpload,
      clearPromptEnhanceChoices,
      setIsVolcanoAssetManagerOpen,
      setReferencePreviewItem,
    ],
  );

  const focusPromptTarget = useCallback(
    (target: HTMLTextAreaElement, options?: FocusOptions): boolean =>
      focusVideoWorkbenchElement(
        target,
        options,
        Boolean(promptEnhanceAbortRef.current || hasActiveUpload()),
      ),
    [hasActiveUpload],
  );

  const insertPromptText = useCallback(
    (text: string) => {
      abortPromptEnhancement();
      clearPromptEnhanceSelection();
      const target = promptRef.current;
      if (!target) {
        setPrompt(
          (prev) => `${prev}${prev.endsWith(" ") || !prev ? "" : " "}${text}`,
        );
        return;
      }
      const start = target.selectionStart ?? prompt.length;
      const end = target.selectionEnd ?? prompt.length;
      const before = prompt.slice(0, start);
      const after = prompt.slice(end);
      const spacer = before && !before.endsWith(" ") ? " " : "";
      const next = `${before}${spacer}${text}${after.startsWith(" ") || !after ? "" : " "}${after}`;
      setPrompt(next);
      requestAnimationFrame(() => {
        const position = (before + spacer + text).length;
        if (!focusPromptTarget(target)) return;
        target.setSelectionRange(position, position);
      });
    },
    [
      abortPromptEnhancement,
      clearPromptEnhanceSelection,
      focusPromptTarget,
      prompt,
    ],
  );

  const insertReferenceTag = useCallback(
    (item: ReferenceDraft) => {
      insertPromptText(referenceDisplayToken(item));
    },
    [insertPromptText],
  );

  const {
    cancelMut,
    createMut,
    deleteMut,
    requestVideoRetry,
    retryMut,
  } = useVideoTaskMutations({
    abortGenerationRefresh,
    action,
    aspectRatio,
    disableVideoSettling,
    durationS: effectiveDurationS,
    effectiveItems,
    enableVideoSettling,
    generateAudio,
    inputImageId,
    invalidateHistory,
    model: selectedModel,
    prompt,
    referenceMedia,
    resolution: effectiveResolution,
    retryRequestFenceRef,
    scheduleGenerationRefresh,
    seed,
    setIsTaskPanelOpen,
    setItems,
    setSelectedVideoId,
    syncVideoSettling,
    terminalHistorySyncedRef,
  });

  const loadAsDraft = useCallback(
    (item: VideoGenerationOut) => {
      switchDraftContext(item.id, item.action);
      setAction(item.action);
      setModel(item.model);
      setDurationS(item.duration_s);
      setResolution(item.resolution);
      setAspectRatio(item.aspect_ratio);
      setGenerateAudio(item.generate_audio);
      setSeed(item.seed != null ? String(item.seed) : "");
      const draftReferenceMedia = item.reference_media.map((reference, index) =>
        referenceDraftFromHistory(reference, index, item.reference_media),
      );
      loadDraftMedia(
        item.input_image_id ?? "",
        item.input_image_id ? "已从历史任务载入" : "",
        draftReferenceMedia,
      );
      setPrompt(
        displayPromptReferenceMentions(item.prompt, draftReferenceMedia),
      );
      requestAnimationFrame(() => {
        const target = promptRef.current;
        if (target) focusPromptTarget(target);
      });
      toast.success("已套用参数");
    },
    [focusPromptTarget, loadDraftMedia, switchDraftContext],
  );

  const {
    canEnhancePrompt,
    enhancePromptAction,
  } = useVideoPromptEnhancement({
    action,
    aspectRatio,
    clearPromptEnhanceChoices,
    draftFenceRef,
    durationS: effectiveDurationS,
    generateAudio,
    hasActiveUpload,
    inputImageId,
    isEnhancingPrompt,
    model: selectedModel,
    prompt,
    promptEnhanceAbortRef,
    promptEnhanceEpochRef,
    referenceMedia,
    referenceUploadPending,
    resolution: effectiveResolution,
    setIsEnhancingPrompt,
    setPrompt,
    setPromptEnhanceCandidates,
    setPromptEnhancePreview,
    setSelectedPromptEnhanceCandidateId,
    uploadPending: firstFrameUploadPending,
  });

  const scrollPromptEditorIntoView = useCallback(() => {
    const target = promptRef.current;
    if (!target) return;
    target.scrollIntoView({
      behavior: motionSafeScrollBehavior(),
      block: "center",
    });
    requestAnimationFrame(() => focusPromptTarget(target));
  }, [focusPromptTarget]);

  const applyPromptEnhanceCandidate = useCallback(
    (candidate: PromptEnhanceCandidate) => {
      if (!canApplyPromptEnhanceCandidate(candidate)) return;
      setPrompt(candidate.prompt);
      setSelectedPromptEnhanceCandidateId(candidate.id);
      requestAnimationFrame(() => {
        const target = promptRef.current;
        if (target) focusPromptTarget(target, { preventScroll: true });
      });
    },
    [focusPromptTarget],
  );

  const handlePromptChange = useCallback(
    (value: string) => {
      abortPromptEnhancement();
      clearPromptEnhanceSelection();
      setPrompt(
        action === "reference"
          ? displayPromptReferenceMentions(value, referenceMedia)
          : value,
      );
    },
    [
      abortPromptEnhancement,
      action,
      clearPromptEnhanceSelection,
      referenceMedia,
    ],
  );

  const resizePromptEditor = useCallback(() => {
    const target = promptRef.current;
    if (!target) return;
    target.style.height = "0px";
    target.style.height = `${target.scrollHeight}px`;
  }, []);

  useEffect(() => {
    resizePromptEditor();
  }, [prompt, resizePromptEditor]);

  useEffect(() => {
    window.addEventListener("resize", resizePromptEditor);
    return () => window.removeEventListener("resize", resizePromptEditor);
  }, [resizePromptEditor]);

  const scrollParametersIntoView = useCallback(() => {
    document.getElementById("video-generation-settings")?.scrollIntoView({
      behavior: motionSafeScrollBehavior(),
      block: "start",
    });
  }, []);

  const uploadsPending = firstFrameUploadPending || referenceUploadPending;
  const submitDisabledReason = useMemo(
    () =>
      videoSubmitDisabledReason({
        createPending: createMut.isPending,
        uploadPending: uploadsPending,
        optionsLoading: optionsQ.isLoading,
        options,
        selectedModel,
        availableResolutions,
        resolution: effectiveResolution,
        availableDurations,
        durationS: effectiveDurationS,
        prompt,
        action,
        inputImageId,
        referenceCounts,
        referenceLimitError,
        seedIsValid,
        estimate,
      }),
    [
      action,
      availableDurations,
      availableResolutions,
      createMut.isPending,
      effectiveDurationS,
      effectiveResolution,
      estimate,
      inputImageId,
      options,
      optionsQ.isLoading,
      prompt,
      referenceCounts,
      referenceLimitError,
      seedIsValid,
      selectedModel,
      uploadsPending,
    ],
  );
  const canSubmit = submitDisabledReason === "可以提交";
  const submitVideo = useCallback(() => {
    if (!canSubmit || hasActiveUpload()) return;
    createMut.mutate();
  }, [canSubmit, createMut, hasActiveUpload]);

  const {
    handleActionChange,
    handleAspectRatioChange,
    handleDurationChange,
    handleGenerateAudioChange,
    handleModelChange,
    handleResolutionChange,
  } = useVideoParameterHandlers({
    action,
    options,
    resolution,
    selectedModel,
    beforeParameterChange: beforeMediaChange,
    switchDraftContext,
    setAction,
    setAspectRatio,
    setDurationS,
    setGenerateAudio,
    setModel,
    setResolution,
  });

  const {
    closeReferencePreview,
    copyVideoPrompt,
    deleteTaskVideo,
    insertReferencePreview,
    previewTaskVideo,
    useTaskAsDraft,
  } = useVideoPageViewActions({
    deleteVideoById: deleteMut.mutate,
    insertReferenceTag,
    loadAsDraft,
    referencePreviewItem,
    setIsTaskPanelOpen,
    setReferencePreviewItem,
    setSelectedVideoId,
  });

  const registerPromptEditor = useCallback(
    (element: HTMLTextAreaElement | null) => {
      promptRef.current = element;
    },
    [],
  );

  const serviceEnabled = Boolean(options?.enabled);
  const serviceSummary = videoServiceSummary({
    loading: optionsQ.isLoading,
    enabled: serviceEnabled,
    modelCount: availableModels.length,
    unavailableReason: options?.unavailable_reason,
  });
  const parameterProfile = `${effectiveResolution} · ${formatDurationLabel(effectiveDurationS)}`;
  const sourceReady = videoSourceReady(
    action,
    inputImageId,
    referenceMedia.length,
  );
  const modelOptionValues = availableModels.map((item) => item.model);
  const durationOptionValues = availableDurations.map(String);
  const aspectRatioOptionValues = options?.aspect_ratios ?? [
    "adaptive",
    "16:9",
    "9:16",
    "1:1",
  ];

  const viewModel: VideoPageViewModel = {
    header: {
      action,
      parameterProfile,
      generateAudio,
      serviceEnabled,
      optionsLoading: optionsQ.isLoading,
      activeCount: activeItems.length,
      historyCount: settledHistoryItems.length,
      serviceSummary,
      submitDisabledReason,
      onOpenParameters: scrollParametersIntoView,
      onOpenTasks: () => setIsTaskPanelOpen(true),
    },
    composer: {
      action,
      onActionChange: handleActionChange,
      firstFrame: {
        pending: firstFrameUploadPending,
        inputImageId,
        uploadedLabel,
        onFile: startFirstFrameUpload,
        onInputImageIdChange: handleInputImageIdChange,
      },
      references: {
        pending: referenceUploadPending,
        counts: referenceCounts,
        limits: referenceLimits,
        items: referenceMedia,
        prompt,
        kindOptions: assetReferenceKindOptions,
        selectedKind: selectedAssetReferenceKind,
        assetUrlInput,
        onFile: startReferenceUpload,
        onOpenAssetManager: () => setIsVolcanoAssetManagerOpen(true),
        onInsert: insertReferenceTag,
        onPreview: setReferencePreviewItem,
        onRemove: removeReferenceDraft,
        onKindChange: setAssetReferenceKind,
        onAssetUrlInputChange: setAssetUrlInput,
        onAddAssetReference: () =>
          addAssetReference(selectedAssetReferenceKind),
      },
      prompt: {
        onPromptEditorChange: registerPromptEditor,
        value: prompt,
        enhancing: isEnhancingPrompt,
        canEnhance: canEnhancePrompt,
        uploadsPending,
        panelVisible: promptEnhancePanelVisible,
        preview: promptEnhancePreview,
        candidates: promptEnhanceCandidates,
        selectedCandidateId: selectedPromptEnhanceCandidateId,
        onEnhance: () => void enhancePromptAction(),
        onChange: handlePromptChange,
        onInsertChip: insertPromptText,
        onSelectCandidate: applyPromptEnhanceCandidate,
        onDismissCandidates: clearPromptEnhanceChoices,
        onReturnToEditor: scrollPromptEditorIntoView,
      },
    },
    parameters: {
      selectedModel,
      modelOptions: modelOptionValues,
      durationS: effectiveDurationS,
      durationOptions: durationOptionValues,
      resolution: effectiveResolution,
      resolutionOptions: availableResolutions,
      aspectRatio,
      aspectRatioOptions: aspectRatioOptionValues,
      seed,
      generateAudio,
      estimate,
      canSubmit,
      reason: submitDisabledReason,
      loading: createMut.isPending,
      sourceReady,
      onSubmit: submitVideo,
      onModelChange: handleModelChange,
      onDurationChange: handleDurationChange,
      onResolutionChange: handleResolutionChange,
      onAspectRatioChange: handleAspectRatioChange,
      onSeedChange: setSeed,
      onGenerateAudioChange: handleGenerateAudioChange,
    },
    assetManager: {
      open: isVolcanoAssetManagerOpen,
      model: selectedModel,
      remainingLimits: remainingVolcanoAssetLimits,
      existingAssetIds: existingVolcanoAssetIds,
      onClose: () => setIsVolcanoAssetManagerOpen(false),
      onUse: useVolcanoAssets,
      onDeleted: removeDeletedVolcanoAssets,
    },
    tasks: {
      open: isTaskPanelOpen,
      activeItems,
      historyItems: filteredHistoryItems,
      historyFilter,
      historyCounts: {
        all: settledHistoryItems.length,
        succeeded: succeededHistoryItems.length,
        failed: failedHistoryItems.length,
      },
      historyLoading: historyQ.isLoading,
      historyHasNextPage: Boolean(historyQ.hasNextPage),
      historyFetchingNextPage: historyQ.isFetchingNextPage,
      retryDisabled: retryMut.isPending,
      selectedVideoId,
      onClose: () => setIsTaskPanelOpen(false),
      onHistoryFilterChange: setHistoryFilter,
      onRefresh: () => void historyQ.refetch(),
      onLoadMore: () => void historyQ.fetchNextPage(),
      onCancel: (item) => cancelMut.mutate(item.id),
      onRetry: (item) => requestVideoRetry(item.id),
      onCopy: copyVideoPrompt,
      onUseDraft: useTaskAsDraft,
      onDelete: deleteTaskVideo,
      onPreview: previewTaskVideo,
    },
    playback: {
      item: playbackVideoItem,
      onClose: () => setSelectedVideoId(""),
      onUseDraft: () => {
        if (playbackVideoItem) loadAsDraft(playbackVideoItem);
      },
      onRetry: () => {
        if (playbackVideoItem) requestVideoRetry(playbackVideoItem.id);
      },
      onCopy: () => {
        if (playbackVideoItem) copyVideoPrompt(playbackVideoItem);
      },
      onDelete: () => {
        if (playbackVideoItem) deleteMut.mutate(playbackVideoItem.video.id);
      },
    },
    referencePreview: {
      item: referencePreviewItem,
      onClose: closeReferencePreview,
      onInsert: insertReferencePreview,
    },
  };

  return <VideoPageView model={viewModel} />;
}
