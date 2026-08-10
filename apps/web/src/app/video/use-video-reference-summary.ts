"use client";

import { useMemo } from "react";

import {
  assetIdFromReferenceUrl,
  referenceCountsFor,
  referenceLimitViolation,
  REFERENCE_KINDS,
} from "./video-reference-domain";
import type {
  ReferenceKind,
  ReferenceLimits,
} from "./video-reference-domain";
import { selectedReferenceKind } from "./video-page-domain";
import type { ReferenceDraft } from "./video-workbench-ui";

export function useVideoReferenceSummary(
  referenceMedia: ReferenceDraft[],
  referenceLimits: ReferenceLimits,
  referenceTotalLimit: number | null,
  assetReferenceKind: ReferenceKind,
) {
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
  const referenceTotal =
    referenceCounts.image + referenceCounts.video + referenceCounts.audio;
  const remainingTotal =
    referenceTotalLimit === null
      ? Number.POSITIVE_INFINITY
      : Math.max(0, referenceTotalLimit - referenceTotal);
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
      image: Math.min(
        remainingTotal,
        Math.max(0, referenceLimits.image - referenceCounts.image),
      ),
      video: Math.min(
        remainingTotal,
        Math.max(0, referenceLimits.video - referenceCounts.video),
      ),
    }),
    [
      referenceCounts.image,
      referenceCounts.video,
      referenceLimits.image,
      referenceLimits.video,
      remainingTotal,
    ],
  );

  return {
    assetReferenceKindOptions,
    existingVolcanoAssetIds,
    referenceCounts,
    referenceLimitError: referenceLimitViolation(
      referenceMedia,
      referenceLimits,
      referenceTotalLimit,
    ),
    referenceTotal,
    remainingVolcanoAssetLimits,
    selectedAssetReferenceKind,
  };
}
