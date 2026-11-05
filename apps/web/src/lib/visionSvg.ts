import type { VisionSvgArtifactState } from "./types";

export function visionSvgArtifactFields(value: unknown): VisionSvgArtifactState {
  if (!value || typeof value !== "object") return {};
  const raw = value as Record<string, unknown>;
  if (raw.artifact_format !== "svg") return {};
  const status = raw.artifact_status;
  return {
    artifact_format: "svg",
    artifact_status: status === "pending" || status === "ready" || status === "invalid" || status === "processing_failed" ? status : undefined,
    artifact_image_id: typeof raw.artifact_image_id === "string" && raw.artifact_image_id.length <= 128 ? raw.artifact_image_id : undefined,
    artifact_error: typeof raw.artifact_error === "string" ? raw.artifact_error : undefined,
    artifact_source_text: typeof raw.artifact_source_text === "string" ? raw.artifact_source_text : undefined,
  };
}

export function applyVisionSvgArtifact(target: VisionSvgArtifactState, value: unknown): boolean {
  const incoming = visionSvgArtifactFields(value);
  if (!incoming.artifact_format) return false;
  if (target.artifact_status === "ready" && incoming.artifact_status !== "ready") return false;
  const changed = Object.entries(incoming).some(([key, item]) => target[key as keyof VisionSvgArtifactState] !== item);
  Object.assign(target, incoming);
  return changed;
}
