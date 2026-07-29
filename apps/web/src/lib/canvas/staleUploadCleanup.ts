import { deleteCanvasUploadedAsset } from "@/lib/api/canvases";

import { executeStaleCanvasUploadCleanup } from "./staleUploadCleanupPolicy";
import type { StaleCanvasUploadCleanupInput } from "./staleUploadCleanupPolicy";

export async function cleanupStaleCanvasUpload(
  input: StaleCanvasUploadCleanupInput,
): Promise<boolean> {
  return executeStaleCanvasUploadCleanup(input, deleteCanvasUploadedAsset);
}

export { shouldCleanupStaleCanvasUpload } from "./staleUploadCleanupPolicy";
export type {
  CanvasUploadAssetKind,
  CanvasUploadedAsset,
  StaleCanvasUploadCleanupInput,
} from "./staleUploadCleanupPolicy";
