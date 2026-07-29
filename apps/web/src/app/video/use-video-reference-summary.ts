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

  return {
    assetReferenceKindOptions,
    existingVolcanoAssetIds,
    referenceCounts,
    referenceLimitError: referenceLimitViolation(
      referenceMedia,
      referenceLimits,
    ),
    remainingVolcanoAssetLimits,
    selectedAssetReferenceKind,
  };
}
