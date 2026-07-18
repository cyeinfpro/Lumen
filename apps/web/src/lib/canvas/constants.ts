export const CANVAS_NOTE_MAX_CHARS = 20_000;
export const CANVAS_NODE_TITLE_MAX_CHARS = 80;

export function normalizeCanvasNodeTitle(
  value: string,
  fallback: string,
): string {
  return value.trim().slice(0, CANVAS_NODE_TITLE_MAX_CHARS) || fallback;
}
