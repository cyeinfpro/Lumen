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
  uploadImage,
  videoPosterUrl,
} from "@/lib/apiClient";
import type {
  VideoAction,
  VideoGenerationOut,
} from "@/lib/types";
import { uuid } from "@/lib/utils";
import {
  isVideoRequestFenceCurrent,
  mergeVideoGenerationLists as mergeById,
  nextVideoRequestFence,
} from "@/lib/videoEventSnapshot";
import type { VideoRequestFence } from "@/lib/videoEventSnapshot";

import {
  appendVolcanoAssetReferences,
  assetIdFromReferenceUrl,
  DEFAULT_REFERENCE_LIMITS,
  displayPromptReferenceMentions,
  nextReferenceIdentity,
  normalizeAssetUrl,
  referenceCountsFor,
  referenceDisplayToken,
  referenceKindNoun,
  referenceLimitMessage,
  referenceLimitViolation,
  referenceLimitsForModelOption,
  referencePayloadForVideoAction,
  removeReferenceAndReindexPrompt,
  removeReferencesAndReindexPrompt,
  referencesForVideoAction,
  REFERENCE_KINDS,
  promptForVideoAction,
} from "./video-reference-domain";
import type {
  ReferenceKind,
  ReferenceLimits,
  VolcanoAssetReferenceCandidate,
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
  isAbortError,
  revokeReferenceObjectUrl,
  revokeUnusedReferenceObjectUrls,
  uploadReferenceVideo,
} from "./video-request-lifecycle";
import type {
  DraftUploadRequest,
  ReferenceUploadRequest,
  ReferenceUploadResult,
} from "./video-request-lifecycle";
import {
  billingModelForAction,
  durationOptionsForModel,
  durationOrPreferred,
  estimateHoldMicro,
  firstModelForAction,
  parseSeed,
  preferredResolution,
  resolutionOptionsForModel,
  toVideoResolution,
} from "./video-options-model";
import {
  cleanReferencePreviewUrl,
  imageReferencePreviewUrl,
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
  selectedReferenceKind,
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
  VideoPageView,
} from "./video-page-view";
import type { VideoPageViewModel } from "./video-page-view";
import {
  formatDurationLabel,
  hasVideo,
} from "./video-task-model";

export default function VideoPage() {
  const promptRef = useRef<HTMLTextAreaElement | null>(null);
  const promptEnhanceAbortRef = useRef<AbortController | null>(null);
  const promptEnhanceEpochRef = useRef(0);
  const firstFrameUploadAbortRef = useRef<AbortController | null>(null);
  const firstFrameUploadEpochRef = useRef(0);
  const referenceUploadAbortRef = useRef<AbortController | null>(null);
  const referenceUploadEpochRef = useRef(0);
  const draftFenceRef = useRef<VideoRequestFence>({
    taskId: "draft:new",
    epoch: 0,
  });
  const retryRequestFenceRef = useRef<VideoRequestFence>({
    taskId: "retry:none",
    epoch: 0,
  });
  const actionRef = useRef<VideoAction>("t2v");
  const referenceLimitsRef = useRef<ReferenceLimits>(DEFAULT_REFERENCE_LIMITS);
  const referenceMediaRef = useRef<ReferenceDraft[]>([]);
  const previousReferenceMediaRef = useRef<ReferenceDraft[]>([]);
  const promptValueRef = useRef("");

  const [action, setAction] = useState<VideoAction>("t2v");
  const [prompt, setPrompt] = useState("");
  const [model, setModel] = useState("");
  const [durationS, setDurationS] = useState(5);
  const [resolution, setResolution] = useState("720p");
  const [aspectRatio, setAspectRatio] = useState("adaptive");
  const [generateAudio, setGenerateAudio] = useState(true);
  const [seed, setSeed] = useState("");
  const [inputImageId, setInputImageId] = useState("");
  const [uploadedLabel, setUploadedLabel] = useState("");
  const [referenceMedia, setReferenceMedia] = useState<ReferenceDraft[]>([]);
  const [referencePreviewItem, setReferencePreviewItem] =
    useState<ReferenceDraft | null>(null);
  const [isVolcanoAssetManagerOpen, setIsVolcanoAssetManagerOpen] =
    useState(false);
  const [assetUrlInput, setAssetUrlInput] = useState("");
  const [assetReferenceKind, setAssetReferenceKind] =
    useState<ReferenceKind>("video");
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

  useEffect(() => {
    referenceMediaRef.current = referenceMedia;
    revokeUnusedReferenceObjectUrls(
      previousReferenceMediaRef.current,
      referenceMedia,
    );
    previousReferenceMediaRef.current = referenceMedia;
  }, [referenceMedia]);

  useEffect(
    () => () => {
      retryRequestFenceRef.current = nextVideoRequestFence(
        retryRequestFenceRef.current,
        "retry:disposed",
      );
      promptEnhanceAbortRef.current?.abort();
      firstFrameUploadAbortRef.current?.abort();
      referenceUploadAbortRef.current?.abort();
      revokeUnusedReferenceObjectUrls(previousReferenceMediaRef.current, []);
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
  useEffect(() => {
    referenceLimitsRef.current = referenceLimits;
  }, [referenceLimits]);

  const assetReferenceKindOptions = useMemo<ReferenceKind[]>(
    () => REFERENCE_KINDS.filter((kind) => referenceLimits[kind] > 0),
    [referenceLimits],
  );
  const selectedAssetReferenceKind = selectedReferenceKind(
    assetReferenceKindOptions,
    assetReferenceKind,
  );
  const referenceCounts = useMemo(
    () => referenceCountsFor(referenceMedia),
    [referenceMedia],
  );
  const existingVolcanoAssetIds = useMemo(
    () =>
      new Set(
        referenceMedia
          .map((item) => assetIdFromReferenceUrl(item.url))
          .filter((assetId): assetId is string => Boolean(assetId)),
      ),
    [referenceMedia],
  );
  const remainingVolcanoAssetLimits = useMemo(
    () => ({
      image: Math.max(0, referenceLimits.image - referenceCounts.image),
      video: Math.max(0, referenceLimits.video - referenceCounts.video),
    }),
    [
      referenceCounts.image,
      referenceCounts.video,
      referenceLimits.image,
      referenceLimits.video,
    ],
  );
  const referenceLimitError = referenceLimitViolation(
    referenceMedia,
    referenceLimits,
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

  const cancelFirstFrameUpload = useCallback(() => {
    firstFrameUploadEpochRef.current += 1;
    const controller = firstFrameUploadAbortRef.current;
    firstFrameUploadAbortRef.current = null;
    controller?.abort();
  }, []);

  const cancelReferenceUpload = useCallback(() => {
    referenceUploadEpochRef.current += 1;
    const controller = referenceUploadAbortRef.current;
    referenceUploadAbortRef.current = null;
    controller?.abort();
  }, []);

  const commitReferenceMedia = useCallback(
    (update: (current: ReferenceDraft[]) => ReferenceDraft[]): boolean => {
      const current = referenceMediaRef.current;
      const next = update(current);
      if (next === current) return false;
      referenceMediaRef.current = next;
      setReferenceMedia(next);
      return true;
    },
    [],
  );

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
    ],
  );

  const isCurrentFirstFrameUpload = useCallback(
    (request: DraftUploadRequest): boolean =>
      firstFrameUploadAbortRef.current === request.controller &&
      firstFrameUploadEpochRef.current === request.epoch &&
      !request.controller.signal.aborted &&
      actionRef.current === request.expectedAction &&
      isVideoRequestFenceCurrent(draftFenceRef.current, request.draftFence),
    [],
  );

  const isCurrentReferenceUpload = useCallback(
    (request: ReferenceUploadRequest): boolean =>
      referenceUploadAbortRef.current === request.controller &&
      referenceUploadEpochRef.current === request.epoch &&
      !request.controller.signal.aborted &&
      actionRef.current === request.expectedAction &&
      isVideoRequestFenceCurrent(draftFenceRef.current, request.draftFence),
    [],
  );

  const focusPromptTarget = useCallback(
    (target: HTMLTextAreaElement, options?: FocusOptions): boolean =>
      focusVideoWorkbenchElement(
        target,
        options,
        Boolean(
          promptEnhanceAbortRef.current ||
          firstFrameUploadAbortRef.current ||
          referenceUploadAbortRef.current,
        ),
      ),
    [],
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

  const uploadMut = useMutation({
    mutationFn: (request: DraftUploadRequest) =>
      uploadImage(request.file, { signal: request.controller.signal }),
    onSuccess: (image, request) => {
      if (!isCurrentFirstFrameUpload(request)) return;
      abortPromptEnhancement();
      clearPromptEnhanceChoices();
      setInputImageId(image.id);
      setUploadedLabel(`${image.width}x${image.height}`);
      toast.success("首帧已上传");
    },
    onError: (error, request) => {
      if (isAbortError(error) || !isCurrentFirstFrameUpload(request)) return;
      toast.error("上传失败", {
        description: error instanceof Error ? error.message : undefined,
      });
    },
    onSettled: (_data, _error, request) => {
      if (firstFrameUploadAbortRef.current === request.controller) {
        firstFrameUploadAbortRef.current = null;
      }
    },
  });

  const referenceUploadMut = useMutation({
    mutationFn: async (
      request: ReferenceUploadRequest,
    ): Promise<ReferenceUploadResult> => {
      if (
        referenceMediaRef.current.filter((item) => item.kind === request.kind)
          .length >= request.limit
      ) {
        throw new Error(referenceLimitMessage(request.kind, request.limit));
      }
      if (request.kind === "image") {
        const image = await uploadImage(request.file, {
          signal: request.controller.signal,
        });
        return {
          kind: "image" as const,
          image_id: image.id,
          display: `${image.width}x${image.height}`,
          previewUrl: imageReferencePreviewUrl(image),
        };
      }
      const video = await uploadReferenceVideo(
        request.file,
        request.controller.signal,
      );
      return {
        kind: "video" as const,
        video_id: video.id,
        display: video.size_bytes
          ? `${Math.round(video.size_bytes / 1024 / 1024)}MB`
          : "视频",
        previewUrl:
          cleanReferencePreviewUrl(video.poster_url) ??
          videoPosterUrl(video.id),
      };
    },
    onSuccess: (reference, request) => {
      if (!isCurrentReferenceUpload(request)) {
        revokeReferenceObjectUrl(reference.previewUrl);
        return;
      }
      abortPromptEnhancement();
      clearPromptEnhanceChoices();
      const limit = referenceLimitsRef.current[reference.kind];
      const accepted = commitReferenceMedia((current) => {
        const currentCount = current.filter(
          (item) => item.kind === reference.kind,
        ).length;
        if (currentCount >= limit) return current;
        const identity = nextReferenceIdentity(reference.kind, current);
        return [
          ...current,
          {
            _key: uuid(),
            kind: reference.kind,
            image_id:
              reference.kind === "image" ? reference.image_id : null,
            video_id:
              reference.kind === "video" ? reference.video_id : null,
            label: identity.label,
            ref_id: identity.refId,
            display: reference.display,
            previewUrl: reference.previewUrl,
          },
        ];
      });
      if (!accepted) {
        revokeReferenceObjectUrl(reference.previewUrl);
        toast.error(referenceLimitMessage(reference.kind, limit));
        return;
      }
      toast.success("参考素材已上传");
    },
    onError: (error, request) => {
      if (isAbortError(error) || !isCurrentReferenceUpload(request)) return;
      toast.error("上传失败", {
        description: error instanceof Error ? error.message : undefined,
      });
    },
    onSettled: (_data, _error, request) => {
      if (referenceUploadAbortRef.current === request.controller) {
        referenceUploadAbortRef.current = null;
      }
    },
  });

  const startFirstFrameUpload = useCallback(
    (file: File) => {
      if (actionRef.current !== "i2v") return;
      abortPromptEnhancement();
      clearPromptEnhanceChoices();
      cancelFirstFrameUpload();
      const controller = new AbortController();
      const request: DraftUploadRequest = {
        controller,
        draftFence: { ...draftFenceRef.current },
        epoch: firstFrameUploadEpochRef.current + 1,
        expectedAction: "i2v",
        file,
      };
      firstFrameUploadEpochRef.current = request.epoch;
      firstFrameUploadAbortRef.current = controller;
      uploadMut.mutate(request);
    },
    [
      abortPromptEnhancement,
      cancelFirstFrameUpload,
      clearPromptEnhanceChoices,
      uploadMut,
    ],
  );

  const startReferenceUpload = useCallback(
    (file: File) => {
      if (actionRef.current !== "reference") return;
      const kind = file.type.startsWith("image/")
        ? "image"
        : file.type.startsWith("video/")
          ? "video"
          : null;
      if (!kind) {
        toast.error("上传失败", { description: "只支持图片或视频" });
        return;
      }
      const limit = referenceLimitsRef.current[kind];
      if (
        referenceMediaRef.current.filter((item) => item.kind === kind).length >=
        limit
      ) {
        toast.error(referenceLimitMessage(kind, limit));
        return;
      }
      abortPromptEnhancement();
      clearPromptEnhanceChoices();
      cancelReferenceUpload();
      const controller = new AbortController();
      const request: ReferenceUploadRequest = {
        controller,
        draftFence: { ...draftFenceRef.current },
        epoch: referenceUploadEpochRef.current + 1,
        expectedAction: "reference",
        file,
        kind,
        limit,
      };
      referenceUploadEpochRef.current = request.epoch;
      referenceUploadAbortRef.current = controller;
      referenceUploadMut.mutate(request);
    },
    [
      abortPromptEnhancement,
      cancelReferenceUpload,
      clearPromptEnhanceChoices,
      referenceUploadMut,
    ],
  );

  const addAssetReference = useCallback(() => {
    if (referenceUploadAbortRef.current) return;
    const url = normalizeAssetUrl(assetUrlInput);
    const kind = selectedAssetReferenceKind;
    if (!url) {
      if (assetUrlInput.trim()) {
        toast.error("请输入 asset-* 或 asset://asset-* 官方素材 ID");
      }
      return;
    }
    const current = referenceMediaRef.current;
    const limit = referenceLimitsRef.current[kind];
    if (current.filter((item) => item.kind === kind).length >= limit) {
      toast.error(referenceLimitMessage(kind, limit));
      return;
    }
    abortPromptEnhancement();
    clearPromptEnhanceChoices();
    commitReferenceMedia((references) => {
      const identity = nextReferenceIdentity(kind, references);
      return [
        ...references,
        {
          _key: uuid(),
          kind,
          url,
          label: identity.label,
          ref_id: identity.refId,
          display: url,
          previewUrl: null,
        },
      ];
    });
    setAssetUrlInput("");
    toast.success(`官方${referenceKindNoun(kind)}已添加`);
  }, [
    abortPromptEnhancement,
    assetUrlInput,
    clearPromptEnhanceChoices,
    commitReferenceMedia,
    selectedAssetReferenceKind,
  ]);

  const useVolcanoAssets = useCallback(
    (assets: VolcanoAssetReferenceCandidate[]) => {
      if (actionRef.current !== "reference") return;
      abortPromptEnhancement();
      clearPromptEnhanceChoices();
      const { references, added } = appendVolcanoAssetReferences(
        referenceMediaRef.current,
        assets,
        referenceLimitsRef.current,
        uuid,
      );
      commitReferenceMedia(() => references);
      setIsVolcanoAssetManagerOpen(false);
      if (added > 0) toast.success(`已添加 ${added} 个火山素材`);
    },
    [abortPromptEnhancement, clearPromptEnhanceChoices, commitReferenceMedia],
  );

  const removeDeletedVolcanoAssets = useCallback(
    (assetIds: string[]) => {
      abortPromptEnhancement();
      clearPromptEnhanceChoices();
      const deletedAssetIds = new Set(assetIds);
      const currentReferences = referenceMediaRef.current;
      const removedKeys = new Set(
        currentReferences
          .filter((item) => {
            const assetId = assetIdFromReferenceUrl(item.url);
            return Boolean(assetId && deletedAssetIds.has(assetId));
          })
          .map((item) => item._key),
      );
      if (removedKeys.size === 0) return;
      const next = removeReferencesAndReindexPrompt(
        promptValueRef.current,
        currentReferences,
        (item) => removedKeys.has(item._key),
      );
      setReferencePreviewItem((current) =>
        current && removedKeys.has(current._key) ? null : current,
      );
      commitReferenceMedia(() => next.references);
      promptValueRef.current = next.prompt;
      setPrompt(next.prompt);
    },
    [
      abortPromptEnhancement,
      clearPromptEnhanceChoices,
      commitReferenceMedia,
    ],
  );

  const removeReferenceDraft = useCallback(
    (target: ReferenceDraft) => {
      abortPromptEnhancement();
      clearPromptEnhanceChoices();
      const next = removeReferenceAndReindexPrompt(
        promptValueRef.current,
        referenceMediaRef.current,
        target,
      );
      setReferencePreviewItem((current) =>
        current?._key === target._key ? null : current,
      );
      commitReferenceMedia(() => next.references);
      promptValueRef.current = next.prompt;
      setPrompt(next.prompt);
    },
    [
      abortPromptEnhancement,
      clearPromptEnhanceChoices,
      commitReferenceMedia,
    ],
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
      setInputImageId(item.input_image_id ?? "");
      setUploadedLabel(item.input_image_id ? "已从历史任务载入" : "");
      const draftReferenceMedia = item.reference_media.map((reference, index) =>
        referenceDraftFromHistory(reference, index, item.reference_media),
      );
      commitReferenceMedia(() => draftReferenceMedia);
      setPrompt(
        displayPromptReferenceMentions(item.prompt, draftReferenceMedia),
      );
      requestAnimationFrame(() => {
        const target = promptRef.current;
        if (target) focusPromptTarget(target);
      });
      toast.success("已套用参数");
    },
    [commitReferenceMedia, focusPromptTarget, switchDraftContext],
  );

  const canEnhancePrompt = canEnhanceVideoPrompt({
    uploadPending: uploadMut.isPending,
    referenceUploadPending: referenceUploadMut.isPending,
    prompt,
    action,
    inputImageId,
    referenceCount: referenceMedia.length,
  });

  const enhancePromptAction = useCallback(async () => {
    if (
      isEnhancingPrompt ||
      !canEnhancePrompt ||
      firstFrameUploadAbortRef.current ||
      referenceUploadAbortRef.current
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

  const uploadsPending = uploadMut.isPending || referenceUploadMut.isPending;
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
    if (
      !canSubmit ||
      firstFrameUploadAbortRef.current ||
      referenceUploadAbortRef.current
    ) {
      return;
    }
    createMut.mutate();
  }, [canSubmit, createMut]);

  const handleActionChange = useCallback(
    (nextAction: VideoAction) => {
      switchDraftContext(`draft:${nextAction}`, nextAction);
      const nextModel = firstModelForAction(options, nextAction);
      const nextResolutions = resolutionOptionsForModel(options, nextModel);
      const nextResolution = nextResolutions.includes(resolution)
        ? resolution
        : preferredResolution(nextResolutions);
      const nextDurations = durationOptionsForModel(
        options,
        nextModel,
        nextAction,
        nextResolution,
      );
      setAction(nextAction);
      setModel(nextModel);
      setDurationS((previous) =>
        durationOrPreferred(previous, nextDurations),
      );
    },
    [options, resolution, switchDraftContext],
  );

  const handleInputImageIdChange = useCallback(
    (value: string) => {
      cancelFirstFrameUpload();
      abortPromptEnhancement();
      clearPromptEnhanceChoices();
      setInputImageId(value);
      setUploadedLabel("");
    },
    [
      abortPromptEnhancement,
      cancelFirstFrameUpload,
      clearPromptEnhanceChoices,
    ],
  );

  const handleModelChange = useCallback(
    (value: string) => {
      abortPromptEnhancement();
      clearPromptEnhanceChoices();
      const nextResolutions = resolutionOptionsForModel(options, value);
      const nextResolution = nextResolutions.includes(resolution)
        ? resolution
        : preferredResolution(nextResolutions);
      const nextDurations = durationOptionsForModel(
        options,
        value,
        action,
        nextResolution,
      );
      setModel(value);
      setResolution(nextResolution);
      setDurationS((previous) =>
        durationOrPreferred(previous, nextDurations),
      );
    },
    [
      abortPromptEnhancement,
      action,
      clearPromptEnhanceChoices,
      options,
      resolution,
    ],
  );

  const handleDurationChange = useCallback(
    (value: string) => {
      abortPromptEnhancement();
      clearPromptEnhanceChoices();
      setDurationS(Number(value));
    },
    [abortPromptEnhancement, clearPromptEnhanceChoices],
  );

  const handleResolutionChange = useCallback(
    (value: string) => {
      abortPromptEnhancement();
      clearPromptEnhanceChoices();
      const nextDurations = durationOptionsForModel(
        options,
        selectedModel,
        action,
        value,
      );
      setResolution(value);
      setDurationS((previous) =>
        durationOrPreferred(previous, nextDurations),
      );
    },
    [
      abortPromptEnhancement,
      action,
      clearPromptEnhanceChoices,
      options,
      selectedModel,
    ],
  );

  const handleAspectRatioChange = useCallback(
    (value: string) => {
      abortPromptEnhancement();
      clearPromptEnhanceChoices();
      setAspectRatio(value);
    },
    [abortPromptEnhancement, clearPromptEnhanceChoices],
  );

  const handleGenerateAudioChange = useCallback(
    (value: boolean) => {
      abortPromptEnhancement();
      clearPromptEnhanceChoices();
      setGenerateAudio(value);
    },
    [abortPromptEnhancement, clearPromptEnhanceChoices],
  );

  const copyVideoPrompt = useCallback((item: VideoGenerationOut) => {
    void navigator.clipboard?.writeText(item.prompt);
    toast.success("描述已复制");
  }, []);

  const useTaskAsDraft = useCallback(
    (item: VideoGenerationOut) => {
      loadAsDraft(item);
      setIsTaskPanelOpen(false);
    },
    [loadAsDraft, setIsTaskPanelOpen],
  );

  const deleteTaskVideo = useCallback(
    (item: VideoGenerationOut) => {
      if (item.video) deleteMut.mutate(item.video.id);
    },
    [deleteMut],
  );

  const previewTaskVideo = useCallback(
    (item: VideoGenerationOut) => {
      if (!hasVideo(item)) return;
      setSelectedVideoId(item.video.id);
      setIsTaskPanelOpen(false);
    },
    [setIsTaskPanelOpen, setSelectedVideoId],
  );

  const closeReferencePreview = useCallback(
    () => setReferencePreviewItem(null),
    [],
  );
  const registerPromptEditor = useCallback(
    (element: HTMLTextAreaElement | null) => {
      promptRef.current = element;
    },
    [],
  );
  const insertReferencePreview = useCallback(() => {
    if (!referencePreviewItem) return;
    insertReferenceTag(referencePreviewItem);
    setReferencePreviewItem(null);
  }, [insertReferenceTag, referencePreviewItem]);

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
        pending: uploadMut.isPending,
        inputImageId,
        uploadedLabel,
        onFile: startFirstFrameUpload,
        onInputImageIdChange: handleInputImageIdChange,
      },
      references: {
        pending: referenceUploadMut.isPending,
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
        onAddAssetReference: addAssetReference,
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
