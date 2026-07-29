"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";

import { toast } from "@/components/ui/primitives";
import {
  cancelVideoGeneration,
  createVideoGeneration,
  deleteVideo,
  enhanceVideoPrompt,
  retryVideoGeneration,
} from "@/lib/apiClient";
import type {
  VideoAction,
  VideoGenerationOut,
} from "@/lib/types";
import {
  isVideoRequestFenceCurrent,
  mergeVideoGenerationLists as mergeById,
  nextVideoRequestFence,
} from "@/lib/videoEventSnapshot";
import type { VideoRequestFence } from "@/lib/videoEventSnapshot";

import {
  displayPromptReferenceMentions,
  referenceDisplayToken,
  referenceLimitsForModelOption,
  referencePayloadForVideoAction,
  referencesForVideoAction,
  promptForVideoAction,
} from "./video-reference-domain";
import {
  canApplyPromptEnhanceCandidate,
  cleanPromptEnhanceText,
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
  toVideoResolution,
} from "./video-options-model";
import {
  motionSafeScrollBehavior,
} from "./video-page-utils";
import { hasPromptEnhancementPanel } from "./video-page-derived-state";
import {
  applyPromptEnhanceCandidateState,
  buildPromptEnhanceCandidates,
  canEnhanceVideoPrompt,
  effectiveVideoDuration,
  effectiveVideoResolution,
  inputImageForVideoAction,
  interruptedPromptEnhanceDescription,
  notifyCompletedPromptEnhancement,
  referenceDraftFromHistory,
  selectedVideoModel,
  videoServiceSummary,
  videoSourceReady,
  videoSubmitDisabledReason,
  VIDEO_PROMPT_VARIANT_COUNT,
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

  const createMut = useMutation({
    mutationFn: () =>
      createVideoGeneration({
        action,
        model: selectedModel,
        prompt: promptForVideoAction(action, prompt, referenceMedia),
        input_image_id: inputImageForVideoAction(action, inputImageId),
        reference_media: referencePayloadForVideoAction(action, referenceMedia),
        duration_s: effectiveDurationS,
        resolution: toVideoResolution(effectiveResolution),
        aspect_ratio: aspectRatio,
        generate_audio: generateAudio,
        seed: parseSeed(seed),
        watermark: false,
      }),
    onSuccess: (generation) => {
      terminalHistorySyncedRef.current.delete(generation.id);
      enableVideoSettling(generation.id);
      syncVideoSettling(generation);
      setItems((previous) => mergeById(previous, [generation]));
      setIsTaskPanelOpen(true);
      toast.success("任务已提交");
      scheduleGenerationRefresh(generation.id, { delayMs: 800 });
      void invalidateHistory();
    },
    onError: (error) =>
      toast.error("提交失败", {
        description: error instanceof Error ? error.message : undefined,
      }),
  });

  const cancelMut = useMutation({
    mutationFn: cancelVideoGeneration,
    onSuccess: (generation, requestedId) => {
      if (generation.id !== requestedId) return;
      setItems((previous) => mergeById(previous, [generation]));
      const providerCannotCancel =
        generation.provider_kind === "dashscope" ||
        generation.provider_kind === "omni_flash" ||
        generation.provider_kind === "volcano_newapi";
      toast.success("已请求取消", {
        description: providerCannotCancel
          ? "该供应商可能无法中止已提交任务，若上游最终成功仍会按结果计费。"
          : undefined,
      });
      scheduleGenerationRefresh(generation.id, { forceHistorySync: true });
    },
    onError: (error) =>
      toast.error("取消失败", {
        description: error instanceof Error ? error.message : undefined,
      }),
  });

  const retryMut = useMutation({
    mutationFn: (request: VideoRequestFence) =>
      retryVideoGeneration(request.taskId),
    onSuccess: (generation, request) => {
      if (!isVideoRequestFenceCurrent(retryRequestFenceRef.current, request)) {
        return;
      }
      terminalHistorySyncedRef.current.delete(generation.id);
      enableVideoSettling(generation.id);
      syncVideoSettling(generation);
      setItems((previous) => mergeById(previous, [generation]));
      setIsTaskPanelOpen(true);
      const createdNewTask = generation.id !== request.taskId;
      toast.success(createdNewTask ? "已创建新的重试任务" : "已重新生成", {
        description: createdNewTask
          ? `正在跟踪新任务 ${generation.id.slice(0, 8)}`
          : undefined,
      });
      scheduleGenerationRefresh(generation.id, { delayMs: 800 });
      void invalidateHistory();
    },
    onError: (error, request) => {
      if (!isVideoRequestFenceCurrent(retryRequestFenceRef.current, request)) {
        return;
      }
      toast.error("重试失败", {
        description: error instanceof Error ? error.message : undefined,
      });
    },
  });

  const requestVideoRetry = useCallback(
    (generationId: string) => {
      const request = nextVideoRequestFence(
        retryRequestFenceRef.current,
        generationId,
      );
      retryRequestFenceRef.current = request;
      retryMut.mutate(request);
    },
    [retryMut],
  );

  const deleteMut = useMutation({
    mutationFn: deleteVideo,
    onSuccess: async (_data, videoId) => {
      for (const item of effectiveItems) {
        if (item.video?.id === videoId) {
          disableVideoSettling(item.id);
          abortGenerationRefresh(item.id);
        }
      }
      setItems((previous) =>
        previous.map((item) =>
          item.video?.id === videoId ? { ...item, video: null } : item,
        ),
      );
      setSelectedVideoId((current) => (current === videoId ? "" : current));
      toast.success("视频已删除");
      await invalidateHistory();
    },
    onError: (error) =>
      toast.error("删除失败", {
        description: error instanceof Error ? error.message : undefined,
      }),
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

  const canEnhancePrompt = canEnhanceVideoPrompt({
    uploadPending: firstFrameUploadPending,
    referenceUploadPending,
    prompt,
    action,
    inputImageId,
    referenceCount: referenceMedia.length,
  });

  const enhancePromptAction = useCallback(async () => {
    if (
      isEnhancingPrompt ||
      !canEnhancePrompt ||
      hasActiveUpload()
    ) {
      return;
    }
    const original = prompt;
    const activeReferenceMedia = referencesForVideoAction(
      action,
      referenceMedia,
    );
    const current = promptForVideoAction(action, prompt, activeReferenceMedia);
    const controller = new AbortController();
    promptEnhanceAbortRef.current?.abort();
    const requestEpoch = promptEnhanceEpochRef.current + 1;
    const requestDraftFence = { ...draftFenceRef.current };
    promptEnhanceEpochRef.current = requestEpoch;
    promptEnhanceAbortRef.current = controller;
    clearPromptEnhanceChoices();
    setIsEnhancingPrompt(true);
    let accumulated = "";
    const isCurrentRequest = () =>
      !controller.signal.aborted &&
      promptEnhanceAbortRef.current === controller &&
      promptEnhanceEpochRef.current === requestEpoch &&
      isVideoRequestFenceCurrent(draftFenceRef.current, requestDraftFence);
    try {
      await enhanceVideoPrompt(
        {
          text: current,
          action,
          model: selectedModel,
          duration_s: effectiveDurationS,
          resolution: effectiveResolution,
          aspect_ratio: aspectRatio,
          generate_audio: generateAudio,
          input_image_id: inputImageForVideoAction(action, inputImageId),
          variant_count: VIDEO_PROMPT_VARIANT_COUNT,
          reference_media: referencePayloadForVideoAction(
            action,
            referenceMedia,
          ),
        },
        (delta) => {
          if (!isCurrentRequest()) return;
          accumulated += delta;
          setPromptEnhancePreview(
            displayPromptReferenceMentions(accumulated, activeReferenceMedia),
          );
        },
        controller.signal,
      );
      if (!isCurrentRequest()) return;
      const candidates = buildPromptEnhanceCandidates(
        accumulated,
        current,
        activeReferenceMedia,
      );
      const applied = applyPromptEnhanceCandidateState(
        candidates,
        setPrompt,
        setPromptEnhanceCandidates,
        setSelectedPromptEnhanceCandidateId,
      );
      if (applied) {
        setPromptEnhancePreview("");
        notifyCompletedPromptEnhancement(
          applied.recommended,
          applied.autoApply,
          candidates.length,
        );
      } else {
        setPromptEnhancePreview("");
        toast.error("优化失败", { description: "没有收到有效提示词" });
        setPrompt(original);
      }
    } catch (error) {
      if (isCurrentRequest()) {
        const description =
          error instanceof Error ? error.message : undefined;
        if (accumulated.trim()) {
          const candidates = buildPromptEnhanceCandidates(
            accumulated,
            current,
            activeReferenceMedia,
          );
          const applied = applyPromptEnhanceCandidateState(
            candidates,
            setPrompt,
            setPromptEnhanceCandidates,
            setSelectedPromptEnhanceCandidateId,
          );
          if (!applied) {
            setPrompt(
              displayPromptReferenceMentions(
                cleanPromptEnhanceText(accumulated),
                activeReferenceMedia,
              ),
            );
          }
          setPromptEnhancePreview("");
          toast.error("优化中断", {
            description: interruptedPromptEnhanceDescription(description),
          });
        } else {
          toast.error("优化失败", { description });
          setPrompt(original);
        }
      }
    } finally {
      if (
        promptEnhanceAbortRef.current === controller &&
        promptEnhanceEpochRef.current === requestEpoch
      ) {
        promptEnhanceAbortRef.current = null;
        setIsEnhancingPrompt(false);
      }
    }
  }, [
    action,
    aspectRatio,
    canEnhancePrompt,
    clearPromptEnhanceChoices,
    effectiveDurationS,
    effectiveResolution,
    generateAudio,
    hasActiveUpload,
    inputImageId,
    isEnhancingPrompt,
    prompt,
    referenceMedia,
    selectedModel,
  ]);

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
