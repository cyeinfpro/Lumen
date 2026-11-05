import { apiFetch } from "./http";

export function retryVisionSvgArtifact(completionId: string) {
  return apiFetch<{ status: string; artifact_status: string }>(
    `/completions/${encodeURIComponent(completionId)}/artifact/retry`,
    { method: "POST", body: JSON.stringify({}) },
  );
}
