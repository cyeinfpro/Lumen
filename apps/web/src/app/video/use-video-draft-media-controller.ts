"use client";

import {
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import type {
  Dispatch,
  MutableRefObject,
  SetStateAction,
} from "react";
import { useMutation } from "@tanstack/react-query";

import { toast } from "@/components/ui/primitives";
import {
  uploadImage,
  videoPosterUrl,
} from "@/lib/apiClient";
import type { VideoAction } from "@/lib/types";
import { uuid } from "@/lib/utils";
import { isVideoRequestFenceCurrent } from "@/lib/videoEventSnapshot";
import type { VideoRequestFence } from "@/lib/videoEventSnapshot";

import {
  appendVolcanoAssetReferences,
  assetIdFromReferenceUrl,
  nextReferenceIdentity,
  normalizeAssetUrl,
  referenceKindNoun,
  referenceLimitMessage,
  removeReferenceAndReindexPrompt,
  removeReferencesAndReindexPrompt,
} from "./video-reference-domain";
import type {
  ReferenceKind,
  ReferenceLimits,
  VolcanoAssetReferenceCandidate,
} from "./video-reference-domain";
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
  cleanReferencePreviewUrl,
  imageReferencePreviewUrl,
} from "./video-page-utils";
import type { ReferenceDraft } from "./video-workbench-ui";

type UseVideoDraftMediaControllerOptions = {
  actionRef: MutableRefObject<VideoAction>;
  draftFenceRef: MutableRefObject<VideoRequestFence>;
  promptValueRef: MutableRefObject<string>;
  referenceLimits: ReferenceLimits;
  beforeMediaChange: () => void;
  setPrompt: Dispatch<SetStateAction<string>>;
};

export function useVideoDraftMediaController({
  actionRef,
  draftFenceRef,
  promptValueRef,
  referenceLimits,
  beforeMediaChange,
  setPrompt,
}: UseVideoDraftMediaControllerOptions) {
  const firstFrameUploadAbortRef = useRef<AbortController | null>(null);
  const firstFrameUploadEpochRef = useRef(0);
  const referenceUploadAbortRef = useRef<AbortController | null>(null);
  const referenceUploadEpochRef = useRef(0);
  const referenceLimitsRef = useRef(referenceLimits);
  const referenceMediaRef = useRef<ReferenceDraft[]>([]);
  const previousReferenceMediaRef = useRef<ReferenceDraft[]>([]);

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

  useEffect(() => {
    referenceLimitsRef.current = referenceLimits;
  }, [referenceLimits]);

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
      firstFrameUploadAbortRef.current?.abort();
      referenceUploadAbortRef.current?.abort();
      revokeUnusedReferenceObjectUrls(previousReferenceMediaRef.current, []);
    },
    [],
  );

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

  const isCurrentFirstFrameUpload = useCallback(
    (request: DraftUploadRequest): boolean =>
      firstFrameUploadAbortRef.current === request.controller &&
      firstFrameUploadEpochRef.current === request.epoch &&
      !request.controller.signal.aborted &&
      actionRef.current === request.expectedAction &&
      isVideoRequestFenceCurrent(draftFenceRef.current, request.draftFence),
    [actionRef, draftFenceRef],
  );

  const isCurrentReferenceUpload = useCallback(
    (request: ReferenceUploadRequest): boolean =>
      referenceUploadAbortRef.current === request.controller &&
      referenceUploadEpochRef.current === request.epoch &&
      !request.controller.signal.aborted &&
      actionRef.current === request.expectedAction &&
      isVideoRequestFenceCurrent(draftFenceRef.current, request.draftFence),
    [actionRef, draftFenceRef],
  );

  const uploadMut = useMutation({
    mutationFn: (request: DraftUploadRequest) =>
      uploadImage(request.file, { signal: request.controller.signal }),
    onSuccess: (image, request) => {
      if (!isCurrentFirstFrameUpload(request)) return;
      beforeMediaChange();
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
      beforeMediaChange();
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
      beforeMediaChange();
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
      actionRef,
      beforeMediaChange,
      cancelFirstFrameUpload,
      draftFenceRef,
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
      beforeMediaChange();
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
      actionRef,
      beforeMediaChange,
      cancelReferenceUpload,
      draftFenceRef,
      referenceUploadMut,
    ],
  );

  const addAssetReference = useCallback(
    (selectedKind: ReferenceKind) => {
      if (referenceUploadAbortRef.current) return;
      const url = normalizeAssetUrl(assetUrlInput);
      if (!url) {
        if (assetUrlInput.trim()) {
          toast.error("输入 asset-* 或 asset://asset-* 官方素材 ID");
        }
        return;
      }
      const current = referenceMediaRef.current;
      const limit = referenceLimitsRef.current[selectedKind];
      if (
        current.filter((item) => item.kind === selectedKind).length >= limit
      ) {
        toast.error(referenceLimitMessage(selectedKind, limit));
        return;
      }
      beforeMediaChange();
      commitReferenceMedia((references) => {
        const identity = nextReferenceIdentity(selectedKind, references);
        return [
          ...references,
          {
            _key: uuid(),
            kind: selectedKind,
            url,
            label: identity.label,
            ref_id: identity.refId,
            display: url,
            previewUrl: null,
          },
        ];
      });
      setAssetUrlInput("");
      toast.success(`官方${referenceKindNoun(selectedKind)}已添加`);
    },
    [
      assetUrlInput,
      beforeMediaChange,
      commitReferenceMedia,
    ],
  );

  const useVolcanoAssets = useCallback(
    (assets: VolcanoAssetReferenceCandidate[]) => {
      if (actionRef.current !== "reference") return;
      beforeMediaChange();
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
    [actionRef, beforeMediaChange, commitReferenceMedia],
  );

  const removeDeletedVolcanoAssets = useCallback(
    (assetIds: string[]) => {
      beforeMediaChange();
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
      beforeMediaChange,
      commitReferenceMedia,
      promptValueRef,
      setPrompt,
    ],
  );

  const removeReferenceDraft = useCallback(
    (target: ReferenceDraft) => {
      beforeMediaChange();
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
      beforeMediaChange,
      commitReferenceMedia,
      promptValueRef,
      setPrompt,
    ],
  );

  const handleInputImageIdChange = useCallback(
    (value: string) => {
      cancelFirstFrameUpload();
      beforeMediaChange();
      setInputImageId(value);
      setUploadedLabel("");
    },
    [beforeMediaChange, cancelFirstFrameUpload],
  );

  const loadDraftMedia = useCallback(
    (
      nextInputImageId: string,
      nextUploadedLabel: string,
      nextReferenceMedia: ReferenceDraft[],
    ) => {
      setInputImageId(nextInputImageId);
      setUploadedLabel(nextUploadedLabel);
      commitReferenceMedia(() => nextReferenceMedia);
    },
    [commitReferenceMedia],
  );

  const hasActiveUpload = useCallback(
    () =>
      Boolean(
        firstFrameUploadAbortRef.current ||
          referenceUploadAbortRef.current,
      ),
    [],
  );

  return {
    assetReferenceKind,
    assetUrlInput,
    cancelFirstFrameUpload,
    cancelReferenceUpload,
    firstFrameUploadPending: uploadMut.isPending,
    handleInputImageIdChange,
    hasActiveUpload,
    inputImageId,
    isVolcanoAssetManagerOpen,
    loadDraftMedia,
    referenceMedia,
    referencePreviewItem,
    referenceUploadPending: referenceUploadMut.isPending,
    setAssetReferenceKind,
    setAssetUrlInput,
    setIsVolcanoAssetManagerOpen,
    setReferencePreviewItem,
    startFirstFrameUpload,
    startReferenceUpload,
    addAssetReference,
    useVolcanoAssets,
    removeDeletedVolcanoAssets,
    removeReferenceDraft,
    uploadedLabel,
  };
}
