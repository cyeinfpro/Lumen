"use client";

import { useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Button, toast } from "@/components/ui/primitives";
import { retryCanvasVideoPreparation } from "@/lib/api/canvasHistory";
import { getPrivateIdentitySnapshot, isPrivateIdentitySnapshotCurrent } from "@/lib/auth/privateIdentityEpoch";
import type { CanvasAssetDescriptor, CanvasDocument } from "@/lib/canvas/types";
import { canvasQueryKeys } from "@/lib/queries/canvases";

export function CanvasPreparationStatus({ canvasId, asset }: { canvasId: string; asset?: CanvasAssetDescriptor }) {
  const client = useQueryClient();
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);
  if (!asset || asset.kind !== "video" || asset.preparation_state === "ready") return null;
  const retry = async () => {
    if (inFlight.current) return;
    const identity = getPrivateIdentitySnapshot();
    if (!identity.userId) return;
    inFlight.current = true;
    setBusy(true);
    try {
      const next = await retryCanvasVideoPreparation(asset);
      if (!isPrivateIdentitySnapshotCurrent(identity)) return;
      client.setQueryData<CanvasDocument>(canvasQueryKeys.detail(canvasId), (document) => document ? {
        ...document,
        assets: document.assets?.map((old) => old.asset_id === next.asset_id && old.kind === next.kind &&
          old.source_sha256 === next.source_sha256 && (old.preparation_revision ?? -1) <= (next.preparation_revision ?? -1) ? next : old),
      } : document);
      toast.success("素材准备已重新排队");
    } catch (error) {
      if (isPrivateIdentitySnapshotCurrent(identity)) toast.error(error instanceof Error ? error.message : "素材准备重试失败");
    } finally {
      inFlight.current = false;
      setBusy(false);
      if (isPrivateIdentitySnapshotCurrent(identity)) void client.invalidateQueries({ queryKey: canvasQueryKeys.detail(canvasId) });
    }
  };
  const canRetry = asset.preparation_state === "failed" && /^[a-f0-9]{64}$/.test(asset.source_sha256) &&
    asset.preparation_revision !== null && Number.isSafeInteger(asset.preparation_revision) && asset.preparation_revision >= 0;
  return <div className="mt-2 grid gap-1 type-caption text-[var(--fg-2)]">
    <span>{asset.preparation_state === "failed" ? "素材准备失败" : asset.preparation_state === "unavailable" ? "素材暂不可用" : "素材准备中"}</span>
    {asset.preparation_state === "failed" ? <>
      <Button size="sm" variant="outline" disabled={!canRetry || busy} onClick={() => { void retry(); }}>重试素材准备</Button>
      <span>仅处理已有视频，不发起付费生成。</span>
    </> : null}
  </div>;
}
