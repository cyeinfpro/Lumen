"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { toast } from "@/components/ui/primitives";
import { supportsOfficialDraft, referenceFinalMedia, referenceFinalResolution, type VideoDraftMode } from "./video-draft-mode";
import { firstModelForAction, videoModelsForAction, resolutionOptionsForModel } from "./video-options-model";
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
  estimateHoldMicro,
  parseSeed,
  videoModelLabel,
} from "./video-options-model";
import {
  deriveVideoOptionSelection,
  deriveVideoParameterSelection,
} from "./video-option-state";
import {
  motionSafeScrollBehavior,
} from "./video-page-utils";
import { hasPromptEnhancementPanel } from "./video-page-derived-state";
import {
  referenceDraftFromHistory,
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
  selectDirectorViewportVideo,
  videoHistoryLoadError,
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
  const [draftMode, setDraftMode] = useState<VideoDraftMode>("direct");
  const [referenceSourceId, setReferenceSourceId] = useState<string | null>(null);
  const [durationS, setDurationS] = useState<number | null>(null);
  const [resolution, setResolution] = useState("");
  const [aspectRatio, setAspectRatio] = useState("");
  const [generateAudio, setGenerateAudio] = useState<boolean | null>(null);
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

  const {
    availableActions,
    effectiveAction,
    availableModels,
    selectedModel,
    referenceLimits,
    referenceTotalLimit,
    allowAudioOnlyReference,
    inputImageConstraints,
    referenceImageConstraints,
  } = deriveVideoOptionSelection(options, action, model);
  const officialDraftSupported = supportsOfficialDraft(availableModels.find((item) => item.model === selectedModel))
    && resolutionOptionsForModel(options, selectedModel, effectiveAction).includes("480p");
  const effectiveDraftMode = draftMode === "official" && !officialDraftSupported ? "direct" : draftMode;
  useEffect(() => {
    actionRef.current = effectiveAction;
  }, [effectiveAction]);

  const clearPromptEnhanceChoices = useCallback(() => {
    setPromptEnhancePreview("");
    setPromptEnhanceCandidates([]);
    setSelectedPromptEnhanceCandidateId("");
  }, [
    setPromptEnhanceCandidates,
    setPromptEnhancePreview,
    setSelectedPromptEnhanceCandidateId,
  ]);

  const clearPromptEnhanceSelection = useCallback(() => {
    setPromptEnhancePreview("");
    setSelectedPromptEnhanceCandidateId("");
  }, [setPromptEnhancePreview, setSelectedPromptEnhanceCandidateId]);

  const abortPromptEnhancement = useCallback(() => {
    promptEnhanceEpochRef.current += 1;
    const controller = promptEnhanceAbortRef.current;
    promptEnhanceAbortRef.current = null;
    controller?.abort();
    setIsEnhancingPrompt(false);
  }, [setIsEnhancingPrompt]);

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
    referenceTotalLimit,
    inputImageConstraints,
    referenceImageConstraints,
    beforeMediaChange,
    setPrompt,
  });

  const {
    assetReferenceKindOptions,
    existingVolcanoAssetIds,
    referenceCounts,
    referenceLimitError,
    referenceTotal,
    remainingVolcanoAssetLimits,
    selectedAssetReferenceKind,
  } = useVideoReferenceSummary(
    referenceMedia,
    referenceLimits,
    referenceTotalLimit,
    assetReferenceKind,
  );
  const {
    selectedBillingModel,
    availableResolutions,
    effectiveResolution,
    availableAspectRatios,
    effectiveAspectRatio,
    availableDurations,
    effectiveDurationS,
    audioCapability,
    effectiveGenerateAudio,
  } = deriveVideoParameterSelection(
    options,
    selectedModel,
    effectiveAction,
    {
      resolution: effectiveDraftMode === "official" ? "480p" : resolution,
      aspectRatio,
      durationS,
      generateAudio,
    },
  );
  const estimate = estimateHoldMicro(options, {
    model: selectedModel,
    billingModel: selectedBillingModel,
    action: effectiveAction,
    resolution: effectiveResolution,
    durationS: effectiveDurationS,
    referenceCounts,
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
      setPrompt,
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
    finalizeMut,
    deleteMut,
    requestVideoRetry,
    retryMut,
  } = useVideoTaskMutations({
    abortGenerationRefresh,
    draft: effectiveDraftMode === "official",
    referenceDraft: effectiveDraftMode === "reference",
    referenceGenerationId: referenceSourceId,
    action: effectiveAction,
    aspectRatio: effectiveAspectRatio,
    disableVideoSettling,
    durationS: effectiveDurationS,
    effectiveItems,
    enableVideoSettling,
    generateAudio: effectiveGenerateAudio,
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
      setDraftMode(item.draft ? "official" : item.reference_draft ? "reference" : "direct");
      setReferenceSourceId(null);
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
    [
      focusPromptTarget,
      loadDraftMedia,
      setDraftMode,
      setReferenceSourceId,
      setAction,
      setAspectRatio,
      setDurationS,
      setGenerateAudio,
      setModel,
      setPrompt,
      setResolution,
      setSeed,
      switchDraftContext,
    ],
  );

  const prepareReferenceFinal = useCallback((item: VideoGenerationOut) => {
    try {
      const refs = referenceFinalMedia(item);
      const counts = { image: 0, video: 0, audio: 0 };
      for (const ref of refs) counts[ref.kind] += 1;
      const models = videoModelsForAction(options, "reference", counts);
      if (!models.length) throw new Error("原素材与参考草稿超过可用成片模型上限");
      const targetModel = firstModelForAction(options, "reference", counts);
      switchDraftContext(`reference-final:${item.id}`, "reference");
      setAction("reference");
      setDraftMode("direct");
      setReferenceSourceId(item.id);
      setModel(targetModel);
      const finalResolution = referenceFinalResolution(resolutionOptionsForModel(options, targetModel, "reference"));
      setResolution(finalResolution);
      setDurationS(-1);
      setAspectRatio("adaptive");
      setGenerateAudio(item.generate_audio);
      setSeed(item.seed != null ? String(item.seed) : "");
      const media = refs.map((ref, index) => referenceDraftFromHistory(ref, index, refs));
      loadDraftMedia("", "", media);
      setPrompt(displayPromptReferenceMentions(item.prompt, media));
      setSelectedVideoId("");
      setIsTaskPanelOpen(false);
      requestAnimationFrame(() => promptRef.current?.scrollIntoView({ block: "center" }));
      toast.success("已载入参考草稿", { description: `${finalResolution ? "已预选1080p。" : "当前模型不支持1080p，使用模型默认分辨率。"}可修改原提示词与成片模型，确认后手动生成；另行计费，画面不保证一致。` });
    } catch (error) {
      toast.error("参考草稿不可用", { description: error instanceof Error ? error.message : undefined });
    }
  }, [options, switchDraftContext, loadDraftMedia, setSelectedVideoId, setIsTaskPanelOpen,
    setAction, setDraftMode, setReferenceSourceId, setModel, setResolution,
    setDurationS, setAspectRatio, setGenerateAudio, setSeed, setPrompt]);

  const {
    canEnhancePrompt,
    enhancePromptAction,
  } = useVideoPromptEnhancement({
    action: effectiveAction,
    aspectRatio: effectiveAspectRatio,
    clearPromptEnhanceChoices,
    draftFenceRef,
    durationS: effectiveDurationS,
    generateAudio: effectiveGenerateAudio,
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
    [
      focusPromptTarget,
      setPrompt,
      setSelectedPromptEnhanceCandidateId,
    ],
  );

  const handlePromptChange = (value: string) => {
    abortPromptEnhancement();
    clearPromptEnhanceSelection();
    setPrompt(
      effectiveAction === "reference"
        ? displayPromptReferenceMentions(value, referenceMedia)
        : value,
    );
  };

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
  const submitDisabledReason = videoSubmitDisabledReason({
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
    action: effectiveAction,
    inputImageId,
    referenceCounts,
    referenceLimitError,
    allowAudioOnlyReference,
    seedIsValid,
    estimate,
  });
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
    action: effectiveAction,
    options,
    aspectRatio: effectiveAspectRatio,
    resolution: effectiveResolution,
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
  const parameterProfile =
    effectiveResolution && availableDurations.length > 0
      ? `${effectiveResolution} · ${formatDurationLabel(effectiveDurationS)}`
      : "参数未配置";
  const sourceReady = videoSourceReady(
    effectiveAction,
    inputImageId,
    referenceMedia.length,
  );
  const directorViewportItem = selectDirectorViewportVideo(effectiveItems);
  const modelOptionValues = availableModels.map((item) => item.model);
  const modelOptionLabels = Object.fromEntries(
    availableModels.map((item) => [item.model, videoModelLabel(item)]),
  );
  const durationOptionValues = availableDurations.map(String);

  const viewModel: VideoPageViewModel = {
    viewport: {
      item: directorViewportItem,
      loading: historyQ.isLoading,
      error: videoHistoryLoadError(historyQ.error, historyQ.fetchStatus === "paused"),
      onRetry: () => void historyQ.refetch(),
      onPreview: previewTaskVideo,
    },
    header: {
      action: effectiveAction,
      parameterProfile,
      generateAudio: effectiveGenerateAudio,
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
      action: effectiveAction,
      actionOptions: availableActions,
      onActionChange: (value) => { setReferenceSourceId(null); handleActionChange(value); },
      firstFrame: {
        pending: firstFrameUploadPending,
        inputImageId,
        uploadedLabel,
        imageConstraints: inputImageConstraints,
        onFile: startFirstFrameUpload,
        onInputImageIdChange: handleInputImageIdChange,
      },
      references: {
        pending: referenceUploadPending,
        counts: referenceCounts,
        limits: referenceLimits,
        total: referenceTotal,
        totalLimit: referenceTotalLimit,
        imageConstraints: referenceImageConstraints,
        items: referenceMedia,
        prompt,
        kindOptions: assetReferenceKindOptions,
        selectedKind: selectedAssetReferenceKind,
        assetUrlInput,
        onFiles: startReferenceUpload,
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
      draftMode: effectiveDraftMode,
      officialDraftSupported,
      referenceSourceId,
      onDraftModeChange: (value) => { beforeMediaChange(); setDraftMode(value); setReferenceSourceId(null); },
      onClearReferenceSource: () => setReferenceSourceId(null),
      selectedModel,
      modelOptions: modelOptionValues,
      modelOptionLabels,
      durationS: effectiveDurationS,
      durationOptions: durationOptionValues,
      resolution: effectiveResolution,
      resolutionOptions: effectiveDraftMode === "official" ? availableResolutions.filter((value) => value === "480p") : availableResolutions,
      aspectRatio: effectiveAspectRatio,
      aspectRatioOptions: availableAspectRatios,
      seed,
      generateAudio: effectiveGenerateAudio,
      audioSupported: audioCapability.supported,
      estimate,
      canSubmit,
      reason: submitDisabledReason,
      loading: createMut.isPending,
      sourceReady,
      onSubmit: submitVideo,
      onModelChange: (value) => {
        if (draftMode === "official" && !supportsOfficialDraft(availableModels.find((item) => item.model === value))) {
          setDraftMode("direct");
          toast.success("已关闭草稿模式", { description: "所选模型不支持官方草稿，可使用参考草稿模式。" });
        }
        handleModelChange(value);
        if (referenceSourceId) {
          const finalResolution = referenceFinalResolution(resolutionOptionsForModel(options, value, effectiveAction));
          setResolution(finalResolution);
          if (!finalResolution) toast.success("使用模型默认分辨率", { description: "此成片模型不支持1080p，可选择其他模型。" });
        }
      },
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
      historyError: videoHistoryLoadError(historyQ.error, historyQ.fetchStatus === "paused"),
      cancelPendingId: cancelMut.isPending ? cancelMut.variables : undefined,
      historyHasNextPage: Boolean(historyQ.hasNextPage),
      historyFetchingNextPage: historyQ.isFetchingNextPage,
      retryDisabled: retryMut.isPending,
      selectedVideoId,
      onClose: () => setIsTaskPanelOpen(false),
      onHistoryFilterChange: setHistoryFilter,
      onRefresh: () => void historyQ.refetch(),
      onLoadMore: () => void historyQ.fetchNextPage(),
      onCancel: (item) => {
        if (!cancelMut.isPending && !item.cancel_requested_at) cancelMut.mutate(item.id);
      },
      onRetry: (item) => requestVideoRetry(item.id),
      onCopy: copyVideoPrompt,
      onUseDraft: useTaskAsDraft,
      onDelete: deleteTaskVideo,
      onPreview: previewTaskVideo,
    },
    playback: {
      item: playbackVideoItem,
      relatedItems: playbackVideoItem ? effectiveItems.filter((item) => item.draft_generation_id === playbackVideoItem.id || item.reference_generation_id === playbackVideoItem.id) : [],
      finalizing: finalizeMut.isPending,
      onFinalize: () => { if (playbackVideoItem && !finalizeMut.isPending) finalizeMut.mutate(playbackVideoItem); },
      onReferenceFinal: () => { if (playbackVideoItem) prepareReferenceFinal(playbackVideoItem); },
      onRelatedPreview: previewTaskVideo,
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
