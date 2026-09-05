import type { CanvasEmergencyDraft } from "./persistence";
import type { CanvasDocument } from "./types";

export function serializeCanvasRecoveryCopy(
  draft: CanvasEmergencyDraft & Pick<CanvasDocument, "title" | "description">,
): string {
  return JSON.stringify(draft, null, 2);
}

export function downloadCanvasRecoveryCopy(
  draft: CanvasEmergencyDraft & Pick<CanvasDocument, "title" | "description">,
): void {
  const blob = new Blob([serializeCanvasRecoveryCopy(draft)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `lumen-canvas-${draft.canvas_id}-${draft.updated_at}.json`;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1_000);
}
