import type { CanvasGraph } from "./types";

export type CanvasUploadAssetKind = "image" | "mask" | "video";

export interface CanvasUploadedAsset {
  id: string;
  created: boolean;
}

export interface StaleCanvasUploadCleanupInput {
  graph: CanvasGraph;
  kind: CanvasUploadAssetKind;
  uploadedAsset: CanvasUploadedAsset;
  initialAssetId: unknown;
}

export type DeleteCanvasUpload = (
  kind: "image" | "video",
  assetId: string,
) => Promise<void>;

export function shouldCleanupStaleCanvasUpload({
  graph,
  kind,
  uploadedAsset,
  initialAssetId,
}: StaleCanvasUploadCleanupInput): boolean {
  const assetId = uploadedAsset.id.trim();
  return (
    uploadedAsset.created &&
    assetId.length > 0 &&
    !Object.is(assetId, initialAssetId) &&
    !canvasGraphReferencesUploadedAsset(graph, kind, assetId)
  );
}

export async function executeStaleCanvasUploadCleanup(
  input: StaleCanvasUploadCleanupInput,
  deleteUpload: DeleteCanvasUpload,
): Promise<boolean> {
  if (!shouldCleanupStaleCanvasUpload(input)) return false;
  try {
    await deleteUpload(
      input.kind === "video" ? "video" : "image",
      input.uploadedAsset.id.trim(),
    );
    return true;
  } catch {
    // Cleanup is best effort because the server remains authoritative.
    return false;
  }
}

function canvasGraphReferencesUploadedAsset(
  graph: CanvasGraph,
  kind: CanvasUploadAssetKind,
  assetId: string,
): boolean {
  const field = kind === "video" ? "video_id" : "image_id";
  return graph.nodes.some((node) => node.config[field] === assetId);
}
